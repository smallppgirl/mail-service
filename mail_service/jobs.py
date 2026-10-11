from datetime import timedelta
from sqlalchemy import select
from mail_service.db import now, aware
from mail_service.models import Job


def ensure_job(db, mailbox_id, kind, key, payload, run_after=None, rearm=False):
    job = db.scalar(select(Job).where(Job.dedupe_key == key))
    if job is None:
        job = Job(
            mailbox_id=mailbox_id, kind=kind, dedupe_key=key, payload=payload, run_after=run_after or now()
        )
        db.add(job)
        db.flush()
    elif rearm and job.status in {"done", "failed"}:
        job.status = "pending"
        job.payload = payload
        job.run_after = run_after or now()
        job.attempts = 0
        job.error = None
    return job


def due_for_publication(job, at=None):
    at = at or now()
    if aware(job.run_after) > at:
        return False
    if job.status in {"pending", "retry_wait"}:
        return True
    return job.status == "queued" and (
        job.last_published_at is None or aware(job.last_published_at) < at - timedelta(seconds=60)
    )
