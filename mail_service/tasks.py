import logging
import random
from datetime import timedelta
from celery import Celery
from sqlalchemy import select
from mail_service.config import settings
from mail_service.db import MailboxBusy, aware, mailbox_lock, now, session
from mail_service.graph import Graph, GraphError
from mail_service.jobs import due_for_publication, ensure_job
from mail_service.models import Checkpoint, Folder, Job, Mailbox, Message, SendRequest, SyncIssue
from mail_service.sending import run_send
from mail_service.storage import capture_attachments
from mail_service.sync import (
    apply_message,
    discover_folders,
    resolve_message,
    start_reconciliation,
    sync_folder,
)

log = logging.getLogger(__name__)
celery = Celery("mail_service", broker=settings().redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 1200},
    task_soft_time_limit=850,
    task_time_limit=900,
    timezone="UTC",
    beat_schedule={
        "schedule": {"task": "mail.schedule", "schedule": 15.0},
        "dispatch": {"task": "mail.dispatch", "schedule": 5.0},
    },
)


@celery.task(name="mail.schedule")
def schedule():
    with session() as db:
        for mailbox in db.scalars(select(Mailbox).where(Mailbox.enabled.is_(True))):
            ensure_job(db, mailbox.id, "folders", f"folders:{mailbox.id}", {}, rearm=False)
            for folder in db.scalars(
                select(Folder).where(
                    Folder.mailbox_id == mailbox.id, Folder.tracked.is_(True), Folder.removed_at.is_(None)
                )
            ):
                ensure_job(db, mailbox.id, "sync", f"sync:{folder.id}", {"folder_id": folder.id})
                ensure_job(
                    db,
                    mailbox.id,
                    "reconcile",
                    f"reconcile:{folder.id}",
                    {"folder_id": folder.id},
                    run_after=now() + timedelta(seconds=settings().reconcile_interval_seconds),
                )
            for request in db.scalars(
                select(SendRequest).where(
                    SendRequest.mailbox_id == mailbox.id,
                    SendRequest.status.in_(
                        [
                            "accepted",
                            "submission_unknown",
                            "preparation_unknown",
                            "draft_creating",
                            "submitting",
                        ]
                    ),
                )
            ):
                send_job = ensure_job(
                    db,
                    mailbox.id,
                    "send",
                    f"send:{request.id}",
                    {"send_id": request.id},
                    run_after=now() + timedelta(seconds=30),
                    rearm=False,
                )
                if send_job.status == "done" and aware(send_job.run_after) <= now():
                    send_job.status = "pending"
                    send_job.attempts = 0
        # Recurring completed jobs become eligible again at their persisted next run time.
        for job in db.scalars(
            select(Job).where(
                Job.status == "done",
                Job.run_after <= now(),
                Job.kind.in_(["folders", "sync", "reconcile"]),
            )
        ):
            job.status = "pending"
            job.attempts = 0
        db.commit()


@celery.task(name="mail.dispatch")
def dispatch():
    with session() as db:
        jobs = db.scalars(
            select(Job)
            .where(Job.run_after <= now(), Job.status.in_(["pending", "retry_wait", "queued", "running"]))
            .order_by(Job.run_after)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            if job.status == "running" and job.lease_until and aware(job.lease_until) < now():
                try:
                    # Never reclaim while an older worker still owns the mailbox lock.
                    with mailbox_lock(job.mailbox_id):
                        job.status = "pending"
                except MailboxBusy:
                    continue
            if not due_for_publication(job):
                continue
            execute.apply_async(args=[job.id], queue="sender" if job.kind == "send" else "mail")
            job.status = "queued"
            job.last_published_at = now()
        # Publish-before-commit can duplicate delivery; job claims make replay safe.
        db.commit()


@celery.task(name="mail.execute")
def execute(job_id):
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        mailbox_id = job.mailbox_id
    try:
        with mailbox_lock(mailbox_id) as guard:
            with session() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job.status not in {"pending", "queued", "retry_wait"} or aware(job.run_after) > now():
                    return
                mailbox = db.get(Mailbox, mailbox_id)
                if not mailbox.enabled:
                    job.status = "failed"
                    job.error = {"code": "mailbox_disabled"}
                    db.commit()
                    return
                job.status = "running"
                job.attempts += 1
                job.lease_until = now() + timedelta(seconds=1000)
                db.commit()
                graph = Graph(mailbox.graph_user_id, guard=guard)
                try:
                    completed = True
                    if job.kind == "folders":
                        discover_folders(db, mailbox, graph)
                    elif job.kind == "sync":
                        folder = db.get(Folder, job.payload["folder_id"])
                        if folder.tracked and folder.removed_at is None:
                            completed = sync_folder(db, folder, graph)
                    elif job.kind == "reconcile":
                        folder = db.get(Folder, job.payload["folder_id"])
                        if folder.tracked and folder.removed_at is None:
                            start_reconciliation(db, folder)
                            ensure_job(
                                db,
                                mailbox.id,
                                "sync",
                                f"sync:{folder.id}",
                                {"folder_id": folder.id},
                                rearm=True,
                            )
                    elif job.kind == "attachments":
                        capture_attachments(
                            db, job.payload["message_id"], job.payload["source_version"], graph
                        )
                    elif job.kind == "resolve":
                        resolve_message(db, db.get(Message, job.payload["message_id"]), graph)
                    elif job.kind == "sync_issue":
                        issue = db.get(SyncIssue, job.payload["issue_id"])
                        folder = db.get(Folder, issue.folder_id)
                        apply_message(db, folder, issue.item, db.get(Checkpoint, folder.id).generation)
                        issue.resolved = True
                        db.commit()
                    elif job.kind == "send":
                        run_send(db, db.get(SendRequest, job.payload["send_id"]), graph)
                    intervals = {
                        "folders": settings().folder_interval_seconds,
                        "sync": settings().sync_interval_seconds,
                        "reconcile": settings().reconcile_interval_seconds,
                        "send": 30,
                    }
                    job.status = "done" if completed else "pending"
                    job.run_after = now() + timedelta(seconds=intervals.get(job.kind, 0) if completed else 1)
                    job.lease_until = None
                    job.error = None
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    job = db.get(Job, job_id)
                    job.error = exc.safe() if isinstance(exc, GraphError) else {"code": type(exc).__name__}
                    if job.kind == "sync":
                        cp = db.get(Checkpoint, job.payload["folder_id"])
                        cp.last_error = job.error
                        cp.status = (
                            "failed" if isinstance(exc, GraphError) and exc.status == 403 else "retry_wait"
                        )
                    retryable = (
                        not isinstance(exc, GraphError) or exc.status in {401, 408, 429} or exc.status >= 500
                    )
                    job.status = (
                        "retry_wait" if retryable and job.attempts < settings().retry_limit else "failed"
                    )
                    delay = (
                        exc.retry_after
                        if isinstance(exc, GraphError) and exc.retry_after
                        else min(600, 2**job.attempts * 5) + random.randint(0, 5)
                    )
                    job.run_after = now() + timedelta(seconds=delay)
                    job.lease_until = None
                    if job.status == "failed" and job.kind == "send":
                        request = db.get(SendRequest, job.payload["send_id"])
                        if request.status not in {
                            "accepted",
                            "submission_unknown",
                            "submitting",
                            "draft_creating",
                            "preparation_unknown",
                        }:
                            request.status = "failed"
                            request.error = job.error
                    db.commit()
                    log.warning("Job %s ended with %s (%s)", job.id, job.status, job.error["code"])
                finally:
                    graph.close()
    except MailboxBusy:
        # No task retry storm; the durable dispatcher republishes unclaimed jobs.
        return
