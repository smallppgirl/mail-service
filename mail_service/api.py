import base64
import hashlib
import json
import tempfile
from datetime import datetime
from typing import Annotated, Literal
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from mail_service.config import settings
from mail_service.db import MailboxBusy, aware, mailbox_lock, now, session
from mail_service.jobs import ensure_job
from mail_service.models import (
    Application,
    Attachment,
    AttachmentVersion,
    Blob,
    Checkpoint,
    Event,
    Folder,
    Job,
    Mailbox,
    Message,
    MessageVersion,
    ProcessingState,
    SendAttachment,
    SendAttempt,
    SendRequest,
    SyncIssue,
    uid,
)
from mail_service.storage import open_blob, store_file
from mail_service.sync import digest

app = FastAPI(title="Hybrid Outlook Mail Service", version="0.1.0")


def database():
    with session() as db:
        yield db


DB = Annotated[object, Depends(database)]


def caller(db: DB, x_api_key: Annotated[str | None, Header()] = None):
    if not x_api_key:
        raise HTTPException(401, "X-API-Key required")
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    application = db.scalar(
        select(Application).where(Application.key_hash == key_hash, Application.enabled.is_(True))
    )
    if not application:
        raise HTTPException(401, "Invalid application credentials")
    return application


Caller = Annotated[Application, Depends(caller)]


def authorize(application, mailbox_id, scope):
    if "admin" in application.scopes:
        return
    if scope not in application.scopes or mailbox_id not in application.mailbox_ids:
        raise HTTPException(403, "Application lacks scope or mailbox access")


def mailbox(db, application, mailbox_id, scope="mail:read"):
    authorize(application, mailbox_id, scope)
    result = db.get(Mailbox, mailbox_id)
    if not result:
        raise HTTPException(404, "Mailbox not found")
    return result


def message(db, application, message_id):
    result = db.get(Message, message_id)
    if not result:
        raise HTTPException(404, "Message not found")
    authorize(application, result.mailbox_id, "mail:read")
    return result


def request_owned(db, application, send_id):
    result = db.get(SendRequest, send_id)
    if not result:
        raise HTTPException(404, "Send request not found")
    authorize(application, result.mailbox_id, "mail:send")
    if result.app_id != application.id and "admin" not in application.scopes:
        raise HTTPException(403, "Send request belongs to another application")
    return result


def fields(row, names):
    return {name: getattr(row, name) for name in names.split()}


def message_data(row, include_body=False):
    names = (
        "id mailbox_id graph_id internet_message_id conversation_id subject sender recipients "
        "received_at sent_at is_read is_draft availability current_folder_graph_id first_seen_at "
        "last_observed_at has_attachments categories"
    )
    if include_body:
        names += " body_type body_content"
    return fields(row, names)


def send_data(row):
    return {
        **fields(row, "id mailbox_id status not_before accepted_at sent_observed_at error created_at"),
        "delivery_status": "unknown",
        "status_url": f"/v1/send-requests/{row.id}",
    }


class Recipient(BaseModel):
    address: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    name: str | None = Field(default=None, max_length=200)


class Body(BaseModel):
    type: Literal["text", "html"] = "text"
    content: str = Field(max_length=2_000_000)


class SendInput(BaseModel):
    to: list[Recipient] = Field(min_length=1, max_length=100)
    cc: list[Recipient] = Field(default_factory=list, max_length=100)
    bcc: list[Recipient] = Field(default_factory=list, max_length=100)
    subject: str = Field(max_length=998)
    body: Body
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)
    not_before: datetime | None = None

    @field_validator("not_before")
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError("not_before must include a timezone")
        return value

    @field_validator("attachment_ids")
    @classmethod
    def unique_attachments(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("attachment_ids must be unique")
        return value


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: DB):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready"}


@app.get("/v1/mailboxes")
def mailboxes(db: DB, application: Caller):
    query = select(Mailbox)
    if "admin" not in application.scopes:
        query = query.where(Mailbox.id.in_(application.mailbox_ids))
    return [fields(row, "id address enabled created_at") for row in db.scalars(query)]


@app.get("/v1/mailboxes/{mailbox_id}/folders")
def folders(mailbox_id: str, db: DB, application: Caller):
    mailbox(db, application, mailbox_id)
    return [
        fields(row, "id graph_id parent_graph_id name tracked removed_at")
        for row in db.scalars(select(Folder).where(Folder.mailbox_id == mailbox_id))
    ]


@app.get("/v1/mailboxes/{mailbox_id}/sync-status")
def sync_status(mailbox_id: str, db: DB, application: Caller):
    mailbox(db, application, mailbox_id)
    result = []
    for folder, checkpoint in db.execute(
        select(Folder, Checkpoint)
        .join(Checkpoint, Checkpoint.folder_id == Folder.id)
        .where(Folder.mailbox_id == mailbox_id)
    ):
        result.append(
            {
                "folder_id": folder.id,
                "name": folder.name,
                "tracked": folder.tracked,
                **fields(checkpoint, "status baseline last_success_at last_error"),
            }
        )
    return {
        "folders": result,
        "quarantined_items": db.scalar(
            select(func.count())
            .select_from(SyncIssue)
            .join(Folder)
            .where(Folder.mailbox_id == mailbox_id, SyncIssue.resolved.is_(False))
        ),
        "unfinished_jobs": db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.mailbox_id == mailbox_id, Job.status.in_(["pending", "queued", "running", "retry_wait"])
            )
        ),
    }


@app.get("/v1/mailboxes/{mailbox_id}/messages")
def messages(
    mailbox_id: str,
    db: DB,
    application: Caller,
    q: str | None = None,
    availability: Literal["present", "unknown", "not_found"] | None = None,
    folder_id: str | None = None,
    received_after: datetime | None = None,
    is_read: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
):
    mailbox(db, application, mailbox_id)
    order_date = func.coalesce(Message.received_at, Message.first_seen_at)
    query = select(Message).where(Message.mailbox_id == mailbox_id)
    if q:
        if db.bind.dialect.name == "postgresql":
            vector = func.to_tsvector(
                "simple", func.coalesce(Message.subject, "") + " " + func.coalesce(Message.body_content, "")
            )
            query = query.where(vector.op("@@")(func.plainto_tsquery("simple", q)))
        else:
            query = query.where(
                or_(
                    Message.subject.icontains(q, autoescape=True),
                    Message.body_content.icontains(q, autoescape=True),
                )
            )
    if availability:
        query = query.where(Message.availability == availability)
    if folder_id:
        folder = db.get(Folder, folder_id)
        if not folder or folder.mailbox_id != mailbox_id:
            raise HTTPException(404, "Folder not found")
        query = query.where(Message.current_folder_graph_id == folder.graph_id)
    if received_after:
        if received_after.tzinfo is None:
            raise HTTPException(422, "received_after must include a timezone")
        query = query.where(Message.received_at >= received_after)
    if is_read is not None:
        query = query.where(Message.is_read == is_read)
    if cursor:
        try:
            timestamp, last_id = json.loads(base64.urlsafe_b64decode(cursor))
            last_date = datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid cursor") from None
        query = query.where(or_(order_date < last_date, (order_date == last_date) & (Message.id < last_id)))
    rows = db.scalars(query.order_by(order_date.desc(), Message.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                [
                    aware(last.received_at or last.first_seen_at).isoformat(),
                    last.id,
                ]
            ).encode()
        ).decode()
    checkpoints = db.scalars(
        select(Checkpoint)
        .join(Folder)
        .where(Folder.mailbox_id == mailbox_id, Folder.tracked.is_(True), Folder.removed_at.is_(None))
    ).all()
    dates = [aware(cp.last_success_at) for cp in checkpoints if cp.last_success_at]
    return {
        "items": [message_data(row) for row in rows[:limit]],
        "next_cursor": next_cursor,
        "freshness": {
            "oldest_folder_sync_at": min(dates) if dates and len(dates) == len(checkpoints) else None,
            "rebuilding": any(cp.baseline for cp in checkpoints),
            "sync_in_progress": any(cp.status == "syncing" for cp in checkpoints),
        },
    }


@app.get("/v1/messages/{message_id}")
def get_message(message_id: str, db: DB, application: Caller):
    return message_data(message(db, application, message_id), include_body=True)


@app.get("/v1/messages/{message_id}/versions")
def versions(message_id: str, db: DB, application: Caller, limit: int = Query(50, ge=1, le=200)):
    message(db, application, message_id)
    return [
        fields(row, "id content_hash content captured_at")
        for row in db.scalars(
            select(MessageVersion)
            .where(MessageVersion.message_id == message_id)
            .order_by(MessageVersion.captured_at.desc())
            .limit(limit)
        )
    ]


@app.get("/v1/messages/{message_id}/attachments")
def attachments(message_id: str, db: DB, application: Caller):
    message(db, application, message_id)
    result = []
    for attachment in db.scalars(select(Attachment).where(Attachment.message_id == message_id)):
        result.append(
            {
                **fields(attachment, "id name kind content_type size is_inline content_id removed_at source"),
                "versions": [
                    fields(row, "id blob_id source_version status captured_at error")
                    for row in db.scalars(
                        select(AttachmentVersion).where(AttachmentVersion.attachment_id == attachment.id)
                    )
                ],
            }
        )
    return result


def stream_blob(blob):
    body = open_blob(blob)

    def chunks():
        try:
            while chunk := body.read(1024 * 1024):
                yield chunk
        finally:
            body.close()

    # Browser clients download raw content; do not serve untrusted HTML inline.
    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(blob.size),
        },
    )


@app.get("/v1/attachments/{attachment_id}/download")
def download_attachment(attachment_id: str, db: DB, application: Caller, version_id: str | None = None):
    attachment = db.get(Attachment, attachment_id)
    if not attachment:
        raise HTTPException(404, "Attachment not found")
    message(db, application, attachment.message_id)
    query = select(AttachmentVersion).where(
        AttachmentVersion.attachment_id == attachment_id, AttachmentVersion.status == "ready"
    )
    if version_id:
        query = query.where(AttachmentVersion.id == version_id)
    version = db.scalar(query.order_by(AttachmentVersion.captured_at.desc()))
    if not version:
        raise HTTPException(409, "Attachment content is not captured")
    return stream_blob(db.get(Blob, version.blob_id))


@app.get("/v1/mailboxes/{mailbox_id}/events")
def events(
    mailbox_id: str,
    db: DB,
    application: Caller,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    mailbox(db, application, mailbox_id)
    rows = db.scalars(
        select(Event).where(Event.mailbox_id == mailbox_id, Event.id > after).order_by(Event.id).limit(limit)
    ).all()
    return {
        "items": [fields(row, "id kind message_id payload created_at") for row in rows],
        "next_cursor": rows[-1].id if rows else after,
    }


class StateInput(BaseModel):
    status: str = Field(max_length=100)
    metadata: dict = Field(default_factory=dict)


@app.put("/v1/messages/{message_id}/processing-state")
def processing_state(message_id: str, body: StateInput, db: DB, application: Caller):
    row = message(db, application, message_id)
    authorize(application, row.mailbox_id, "mail:process")
    state = db.get(ProcessingState, (application.id, message_id))
    if state is None:
        state = ProcessingState(app_id=application.id, message_id=message_id)
        db.add(state)
    state.status = body.status
    state.data = body.metadata
    state.updated_at = now()
    db.commit()
    return {"status": state.status, "metadata": state.data}


@app.get("/v1/messages/{message_id}/processing-state")
def get_processing_state(message_id: str, db: DB, application: Caller):
    message(db, application, message_id)
    state = db.get(ProcessingState, (application.id, message_id))
    return {"status": state.status, "metadata": state.data} if state else None


@app.post("/v1/mailboxes/{mailbox_id}/uploads", status_code=201)
def upload(mailbox_id: str, db: DB, application: Caller, file: UploadFile = File()):
    mailbox(db, application, mailbox_id, "mail:send")
    blob_id = uid()
    blob = Blob(
        id=blob_id,
        mailbox_id=mailbox_id,
        app_id=application.id,
        bucket=settings().s3_bucket,
        key=f"mailboxes/{mailbox_id}/blobs/{blob_id}",
        name=(file.filename or "attachment")[:255],
        content_type=file.content_type or "application/octet-stream",
    )
    db.add(blob)
    db.commit()
    try:
        with tempfile.TemporaryFile() as bounded:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings().max_attachment_bytes:
                    raise ValueError("Attachment exceeds configured size limit")
                bounded.write(chunk)
            store_file(blob, bounded)
        db.commit()
    except ValueError:
        blob.status = "failed"
        db.commit()
        raise HTTPException(413, "Attachment exceeds configured size limit") from None
    return fields(blob, "id name content_type size sha256 status")


@app.post("/v1/mailboxes/{mailbox_id}/send-requests", status_code=202)
def send(
    mailbox_id: str,
    body: SendInput,
    db: DB,
    application: Caller,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    box = mailbox(db, application, mailbox_id, "mail:send")
    if not box.enabled:
        raise HTTPException(409, "Mailbox disabled")
    payload = body.model_dump(mode="json", exclude_none=True)
    payload_hash = digest(payload)
    previous = db.scalar(
        select(SendRequest).where(
            SendRequest.app_id == application.id,
            SendRequest.mailbox_id == mailbox_id,
            SendRequest.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if previous.payload_hash != payload_hash:
            raise HTTPException(409, "Idempotency key was used with a different payload")
        return send_data(previous)
    total_size = len(body.body.content.encode("utf-8"))
    for blob_id in body.attachment_ids:
        blob = db.get(Blob, blob_id)
        if not blob or blob.mailbox_id != mailbox_id or blob.app_id != application.id:
            raise HTTPException(403, "Attachment upload is not owned by this application/mailbox")
        if blob.status != "ready":
            raise HTTPException(409, "Attachment upload is not ready")
        # Conservative MIME/base64 allowance, in addition to the actual mailbox limit.
        total_size += ((blob.size + 2) // 3) * 4 + 2048
    if total_size > settings().max_send_total_bytes:
        raise HTTPException(413, "Message exceeds configured total size limit")
    request = SendRequest(
        mailbox_id=mailbox_id,
        app_id=application.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        payload=payload,
        not_before=body.not_before or now(),
    )
    try:
        db.add(request)
        db.flush()
        for blob_id in body.attachment_ids:
            db.add(SendAttachment(send_id=request.id, blob_id=blob_id))
        ensure_job(
            db,
            mailbox_id,
            "send",
            f"send:{request.id}",
            {"send_id": request.id},
            run_after=request.not_before,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.scalar(
            select(SendRequest).where(
                SendRequest.app_id == application.id,
                SendRequest.mailbox_id == mailbox_id,
                SendRequest.idempotency_key == idempotency_key,
            )
        )
        if not previous or previous.payload_hash != payload_hash:
            raise HTTPException(409, "Concurrent idempotency conflict") from None
        return send_data(previous)
    return send_data(request)


@app.get("/v1/send-requests/{send_id}")
def get_send(send_id: str, db: DB, application: Caller):
    request = request_owned(db, application, send_id)
    return {
        **send_data(request),
        "attempts": [
            fields(row, "phase started_at finished_at outcome status_code")
            for row in db.scalars(
                select(SendAttempt).where(SendAttempt.send_id == send_id).order_by(SendAttempt.started_at)
            )
        ],
    }


@app.post("/v1/send-requests/{send_id}/cancel")
def cancel_send(send_id: str, db: DB, application: Caller):
    request = request_owned(db, application, send_id)
    try:
        with mailbox_lock(request.mailbox_id):
            db.refresh(request)
            if request.status not in {"queued", "draft_ready", "preparing"}:
                raise HTTPException(409, "Request cannot be cancelled in its current state")
            request.status = "cancelled"
            db.commit()
    except MailboxBusy:
        raise HTTPException(409, "Mailbox operation in progress; retry cancellation") from None
    return send_data(request)


class RetryInput(BaseModel):
    acknowledge_duplicate_risk: bool = False


@app.post("/v1/send-requests/{send_id}/retry", status_code=202)
def retry_send(send_id: str, body: RetryInput, db: DB, application: Caller):
    previous = request_owned(db, application, send_id)
    try:
        with mailbox_lock(previous.mailbox_id):
            db.refresh(previous)
            if previous.status not in {"failed", "needs_review"}:
                raise HTTPException(409, "Only failed or needs_review requests can be retried")
            if previous.status == "needs_review" and not body.acknowledge_duplicate_risk:
                raise HTTPException(409, "Retry can duplicate mail; acknowledge_duplicate_risk is required")
            # New correlation identity; original uncertain operation remains auditable.
            request = SendRequest(
                mailbox_id=previous.mailbox_id,
                app_id=application.id,
                idempotency_key=f"retry:{previous.id}",
                payload_hash=previous.payload_hash,
                payload=previous.payload,
                not_before=now(),
            )
            existing = db.scalar(
                select(SendRequest).where(
                    SendRequest.app_id == application.id,
                    SendRequest.mailbox_id == previous.mailbox_id,
                    SendRequest.idempotency_key == request.idempotency_key,
                )
            )
            if existing:
                return send_data(existing)
            db.add(request)
            db.flush()
            for link in db.scalars(select(SendAttachment).where(SendAttachment.send_id == previous.id)):
                db.add(SendAttachment(send_id=request.id, blob_id=link.blob_id))
            ensure_job(db, request.mailbox_id, "send", f"send:{request.id}", {"send_id": request.id})
            db.commit()
            return send_data(request)
    except MailboxBusy:
        raise HTTPException(409, "Mailbox operation in progress") from None


@app.post("/v1/mailboxes/{mailbox_id}/sync-requests", status_code=202)
def request_sync(mailbox_id: str, db: DB, application: Caller):
    mailbox(db, application, mailbox_id, "mail:sync")
    try:
        with mailbox_lock(mailbox_id):
            ensure_job(db, mailbox_id, "folders", f"folders:{mailbox_id}", {}, rearm=True)
            for folder in db.scalars(
                select(Folder).where(
                    Folder.mailbox_id == mailbox_id, Folder.tracked.is_(True), Folder.removed_at.is_(None)
                )
            ):
                job = ensure_job(
                    db, mailbox_id, "sync", f"sync:{folder.id}", {"folder_id": folder.id}, rearm=True
                )
                if job.status in {"pending", "retry_wait"}:
                    job.run_after = now()
            db.commit()
    except MailboxBusy:
        return {"status": "already_in_progress"}
    return {"status": "queued"}


@app.get("/v1/mailboxes/{mailbox_id}/sync-issues")
def sync_issues(mailbox_id: str, db: DB, application: Caller, limit: int = Query(100, ge=1, le=500)):
    mailbox(db, application, mailbox_id, "mail:sync")
    return [
        fields(row, "id folder_id error resolved captured_at")
        for row in db.scalars(
            select(SyncIssue)
            .join(Folder)
            .where(Folder.mailbox_id == mailbox_id, SyncIssue.resolved.is_(False))
            .order_by(SyncIssue.captured_at)
            .limit(limit)
        )
    ]


@app.post("/v1/sync-issues/{issue_id}/retry", status_code=202)
def retry_issue(issue_id: str, db: DB, application: Caller):
    issue = db.get(SyncIssue, issue_id)
    if not issue:
        raise HTTPException(404, "Sync issue not found")
    folder = db.get(Folder, issue.folder_id)
    authorize(application, folder.mailbox_id, "mail:sync")
    ensure_job(db, folder.mailbox_id, "sync_issue", f"issue:{issue.id}", {"issue_id": issue.id}, rearm=True)
    db.commit()
    return {"status": "queued"}


class FolderInput(BaseModel):
    tracked: bool


@app.patch("/v1/folders/{folder_id}")
def configure_folder(folder_id: str, body: FolderInput, db: DB, application: Caller):
    folder = db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    authorize(application, folder.mailbox_id, "mail:sync")
    try:
        with mailbox_lock(folder.mailbox_id):
            folder.tracked = body.tracked
            if body.tracked:
                ensure_job(
                    db, folder.mailbox_id, "sync", f"sync:{folder.id}", {"folder_id": folder.id}, rearm=True
                )
            db.commit()
    except MailboxBusy:
        raise HTTPException(409, "Mailbox operation in progress") from None
    return fields(folder, "id tracked")


@app.get("/v1/mailboxes/{mailbox_id}/jobs")
def list_jobs(
    mailbox_id: str,
    db: DB,
    application: Caller,
    status: str = "failed",
    limit: int = Query(100, ge=1, le=500),
):
    if "admin" not in application.scopes:
        raise HTTPException(403, "Administrator scope required")
    mailbox(db, application, mailbox_id)
    return [
        fields(row, "id kind status attempts run_after error")
        for row in db.scalars(
            select(Job)
            .where(Job.mailbox_id == mailbox_id, Job.status == status)
            .order_by(Job.run_after)
            .limit(limit)
        )
    ]


@app.post("/v1/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, db: DB, application: Caller):
    if "admin" not in application.scopes:
        raise HTTPException(403, "Administrator scope required")
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.kind == "send":
        raise HTTPException(409, "Use the send-request retry API to preserve duplicate-risk handling")
    try:
        with mailbox_lock(job.mailbox_id):
            db.refresh(job)
            if job.status != "failed":
                raise HTTPException(409, "Job is not failed")
            job.status = "pending"
            job.run_after = now()
            job.attempts = 0
            db.commit()
    except MailboxBusy:
        raise HTTPException(409, "Mailbox operation in progress") from None
    return {"status": "queued"}
