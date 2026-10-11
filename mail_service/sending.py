import base64
import hashlib
import tempfile
from datetime import timedelta
from sqlalchemy import select
from mail_service.config import settings
from mail_service.db import now, aware
from mail_service.graph import GraphError, segment
from mail_service.models import Blob, SendAttachment, SendAttempt
from mail_service.storage import open_blob
from mail_service.sync import event, parse_date


MARKER = "x-mail-service-request-id"


def begin_attempt(db, request, phase):
    attempt = SendAttempt(send_id=request.id, phase=phase)
    db.add(attempt)
    db.commit()
    return attempt


def finish_attempt(db, attempt, graph, outcome):
    attempt.finished_at = now()
    attempt.outcome = outcome
    response = getattr(graph, "last_response", None)
    if response is not None:
        attempt.status_code = response.status_code
        attempt.graph_request_id = response.headers.get("request-id")
    db.commit()


def marker_matches(item, request_id):
    return any(
        h.get("name", "").lower() == MARKER and h.get("value") == request_id
        for h in item.get("internetMessageHeaders", [])
    )


def find_correlated_message(graph, request):
    cutoff = (aware(request.created_at) - timedelta(minutes=5)).isoformat()
    matches = []
    for folder in ["drafts", "sentitems"]:
        path = (
            graph.root + f"/mailFolders/{folder}/messages"
            f"?$filter=createdDateTime ge {cutoff}"
            "&$select=id,isDraft,sentDateTime,internetMessageHeaders"
        )
        for page in graph.pages(path):
            for item in page["value"]:
                if marker_matches(item, request.id):
                    matches.append(item)
    if len(matches) > 1:
        request.status = "needs_review"
        request.error = {"code": "multiple_correlated_messages", "duplicate_possible": True}
        return None
    return matches[0] if matches else None


def reconcile_send(db, request, graph):
    previous = request.status
    expired = request.unknown_since is not None and now() - aware(request.unknown_since) > timedelta(
        seconds=settings().unknown_reconcile_seconds
    )
    item = None
    if request.draft_id:
        try:
            item = graph.get(
                graph.root + f"/messages/{segment(request.draft_id)}?$select=id,isDraft,sentDateTime"
            )
        except GraphError as exc:
            if exc.status != 404:
                raise
    if item is None:
        item = find_correlated_message(graph, request)
    if item:
        request.draft_id = item["id"]
        if item.get("isDraft") is False and item.get("sentDateTime"):
            request.status = "sent_observed"
            request.sent_observed_at = now()
            event(db, request.mailbox_id, "send.sent_observed", payload={"send_request_id": request.id})
        elif request.status in {"preparation_unknown", "draft_creating"}:
            # Draft creation ambiguity can be resolved by recovering the exact draft.
            request.status = "needs_review" if expired and previous == "preparation_unknown" else "preparing"
            if request.status == "needs_review":
                request.error = {"code": "unresolved_attachment_preparation", "duplicate_possible": True}
            # Preserve the ambiguity window while attachment recovery is in progress.
    if request.status in {"submission_unknown", "preparation_unknown", "draft_creating", "submitting"}:
        since = aware(request.unknown_since or request.created_at)
        if now() - since > timedelta(seconds=settings().unknown_reconcile_seconds):
            request.status = "needs_review"
            request.error = {"code": "unresolved_external_operation", "duplicate_possible": True}
    db.commit()


def existing_attachment(graph, root, blob):
    """Recover an attachment POST/last PUT whose successful response was lost."""
    for page in graph.pages(root):
        for item in page["value"]:
            if item.get("name") != blob.name or item.get("size") != blob.size:
                continue
            with tempfile.TemporaryFile() as file:
                graph.download(root + f"/{segment(item['id'])}/$value", file, settings().max_attachment_bytes)
                sha = hashlib.file_digest(file, "sha256").hexdigest()
            if sha == blob.sha256:
                return item["id"]
    return None


def attach_blob(db, request, link, blob, graph):
    root = graph.root + f"/messages/{segment(request.draft_id)}/attachments"
    if link.status == "ready":
        return
    if link.status == "uploading":
        found = existing_attachment(graph, root, blob)
        if found:
            link.graph_id = found
            link.status = "ready"
            db.commit()
            return
        if blob.size < 3 * 1024 * 1024:
            # A previous POST may still be in flight. Do not blindly POST another copy.
            raise RuntimeError("attachment_post_unresolved")
    link.status = "uploading"
    db.commit()
    if blob.size < 3 * 1024 * 1024:
        body = open_blob(blob)
        try:
            data = body.read(settings().max_attachment_bytes + 1)
        finally:
            body.close()
        if len(data) != blob.size or hashlib.sha256(data).hexdigest() != blob.sha256:
            raise IOError("Outgoing attachment checksum mismatch")
        item = graph.post(
            root,
            json={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": blob.name,
                "contentType": blob.content_type,
                "contentBytes": base64.b64encode(data).decode(),
            },
        )
        link.graph_id = item["id"]
    else:
        state = link.upload_state
        if not state or parse_date(state["expirationDateTime"]) <= now():
            state = graph.post(
                root + "/createUploadSession",
                json={
                    "AttachmentItem": {
                        "attachmentType": "file",
                        "name": blob.name,
                        "size": blob.size,
                    }
                },
            )
            link.upload_state = state
            db.commit()
        # The session is the progress authority after a lost chunk response.
        response = graph.upload_request("GET", state["uploadUrl"])
        remote = response.json()
        offset = int(remote.get("nextExpectedRanges", ["0-"])[0].split("-")[0])
        source = open_blob(blob)
        try:
            with tempfile.TemporaryFile() as file:
                sha = hashlib.sha256()
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings().max_attachment_bytes:
                        raise ValueError("Attachment exceeds configured limit")
                    sha.update(chunk)
                    file.write(chunk)
                if size != blob.size or sha.hexdigest() != blob.sha256:
                    raise IOError("Outgoing attachment checksum mismatch")
                file.seek(offset)
                while offset < blob.size:
                    chunk = file.read(3 * 1024 * 1024)
                    response = graph.upload_request(
                        "PUT",
                        state["uploadUrl"],
                        content=chunk,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{blob.size}",
                        },
                    )
                    offset += len(chunk)
                    link.upload_state = {**state, "nextExpectedRanges": [f"{offset}-"]}
                    db.commit()
        finally:
            source.close()
        link.graph_id = existing_attachment(graph, root, blob)
        if not link.graph_id:
            raise RuntimeError("Completed attachment not visible yet")
    link.status = "ready"
    db.commit()


def run_send(db, request, graph):
    if request.status in {"cancelled", "sent_observed", "failed", "needs_review"}:
        return
    if request.status in {
        "accepted",
        "submission_unknown",
        "submitting",
        "preparation_unknown",
        "draft_creating",
    }:
        reconcile_send(db, request, graph)
        if request.status != "preparing":
            return
    try:
        if not request.draft_id:
            request.status = "draft_creating"
            request.unknown_since = now()
            db.commit()
            attempt = begin_attempt(db, request, "create_draft")
            payload = request.payload
            item = graph.post(
                graph.root + "/messages",
                json={
                    "subject": payload["subject"],
                    "body": {"contentType": payload["body"]["type"], "content": payload["body"]["content"]},
                    "toRecipients": [{"emailAddress": r} for r in payload["to"]],
                    "ccRecipients": [{"emailAddress": r} for r in payload.get("cc", [])],
                    "bccRecipients": [{"emailAddress": r} for r in payload.get("bcc", [])],
                    "internetMessageHeaders": [{"name": MARKER, "value": request.id}],
                },
            )
            request.draft_id = item["id"]
            request.status = "preparing"
            request.unknown_since = None
            db.commit()
            finish_attempt(db, attempt, graph, "created")
        for link in db.scalars(select(SendAttachment).where(SendAttachment.send_id == request.id)):
            blob = db.get(Blob, link.blob_id)
            if blob.status != "ready":
                raise ValueError("Outgoing attachment is not ready")
            attach_blob(db, request, link, blob, graph)
        request.status = "draft_ready"
        db.commit()
        attempt = begin_attempt(db, request, "submit")
        request.status = "submitting"
        request.unknown_since = now()
        db.commit()
        graph.post(graph.root + f"/messages/{segment(request.draft_id)}/send")
        request.status = "accepted"
        request.accepted_at = now()
        request.unknown_since = None
        event(db, request.mailbox_id, "send.accepted", payload={"send_request_id": request.id})
        db.commit()
        finish_attempt(db, attempt, graph, "accepted")
    except GraphError as exc:
        request.error = exc.safe()
        if exc.status == 429 or exc.status == 401:
            # Explicit rejection is retryable; reset small attachment POST ownership.
            if request.status == "submitting":
                request.status = "draft_ready"
            elif request.status == "draft_creating":
                request.status = "queued"
            for link in db.scalars(select(SendAttachment).where(SendAttachment.send_id == request.id)):
                if link.status == "uploading" and not link.upload_state:
                    link.status = "pending"
            db.commit()
            raise
        if exc.status >= 500:
            request.status = "submission_unknown" if request.status == "submitting" else "preparation_unknown"
            request.unknown_since = request.unknown_since or now()
        else:
            request.status = "failed"
            event(db, request.mailbox_id, "send.failed", payload={"send_request_id": request.id})
        db.commit()
        if exc.status >= 500:
            return
    except Exception as exc:
        previous = request.status
        request.status = "submission_unknown" if previous == "submitting" else "preparation_unknown"
        request.unknown_since = request.unknown_since or now()
        request.error = {"code": type(exc).__name__}
        db.commit()
        # No automatic send retry across uncertain external-operation boundaries.
