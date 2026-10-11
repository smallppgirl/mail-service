import hashlib
import json
from datetime import datetime
from sqlalchemy import select
from mail_service.db import now
from mail_service.graph import GraphError, segment
from mail_service.jobs import ensure_job
from mail_service.models import (
    Checkpoint,
    Event,
    Folder,
    Membership,
    Message,
    MessageVersion,
    SyncIssue,
)

SELECT = (
    "id,parentFolderId,internetMessageId,conversationId,subject,sender,from,toRecipients,"
    "ccRecipients,bccRecipients,receivedDateTime,sentDateTime,lastModifiedDateTime,changeKey,"
    "isRead,isDraft,hasAttachments,categories,body,bodyPreview"
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def event(db, mailbox_id, kind, message_id=None, payload=None):
    db.add(Event(mailbox_id=mailbox_id, message_id=message_id, kind=kind, payload=payload or {}))


def apply_message(db, folder, item, generation):
    graph_id = item["id"]
    message = db.scalar(
        select(Message).where(Message.mailbox_id == folder.mailbox_id, Message.graph_id == graph_id)
    )
    if "@removed" in item:
        if message:
            member = db.get(Membership, (message.id, folder.id))
            if member and member.present:
                member.present = False
                if message.current_folder_graph_id == folder.graph_id:
                    message.availability = "unknown"
                event(
                    db,
                    folder.mailbox_id,
                    "message.live_state_changed",
                    message.id,
                    {"removed_from_folder": folder.id},
                )
                ensure_job(
                    db,
                    folder.mailbox_id,
                    "resolve",
                    f"resolve:{message.id}",
                    {"message_id": message.id},
                    rearm=True,
                )
        return message

    first = message is None
    if first:
        message = Message(mailbox_id=folder.mailbox_id, graph_id=graph_id)
        db.add(message)
        db.flush()
    old_state = (
        message.is_read,
        message.is_draft,
        message.current_folder_graph_id,
        message.availability,
        message.categories,
    )
    old_change_key = message.change_key
    mapping = {
        "internetMessageId": "internet_message_id",
        "conversationId": "conversation_id",
        "subject": "subject",
        "sender": "sender",
        "changeKey": "change_key",
        "isRead": "is_read",
        "isDraft": "is_draft",
        "hasAttachments": "has_attachments",
        "categories": "categories",
    }
    for remote, local in mapping.items():
        if remote in item:
            setattr(message, local, item[remote])
    for remote, local in [
        ("receivedDateTime", "received_at"),
        ("sentDateTime", "sent_at"),
        ("lastModifiedDateTime", "modified_at"),
    ]:
        if remote in item:
            setattr(message, local, parse_date(item[remote]))
    recipients = dict(message.recipients or {})
    for name in ["from", "toRecipients", "ccRecipients", "bccRecipients"]:
        if name in item:
            recipients[name] = item[name]
    message.recipients = recipients
    message.current_folder_graph_id = item.get(
        "parentFolderId", message.current_folder_graph_id or folder.graph_id
    )
    message.availability = "present"
    message.last_observed_at = now()
    member = db.get(Membership, (message.id, folder.id))
    if member is None:
        member = Membership(message_id=message.id, folder_id=folder.id)
        db.add(member)
    member.present = True
    member.generation = generation

    if "body" in item:
        body = item["body"] or {}
        message.body_type = body.get("contentType")
        message.body_content = body.get("content")
    if message.body_type is not None:
        body = {"contentType": message.body_type, "content": message.body_content}
        content = {
            "subject": message.subject,
            "sender": message.sender,
            "recipients": recipients,
            "body": body,
            "internetMessageId": message.internet_message_id,
        }
        content_hash = digest(content)
        if not db.scalar(
            select(MessageVersion.id).where(
                MessageVersion.message_id == message.id, MessageVersion.content_hash == content_hash
            )
        ):
            db.add(MessageVersion(message_id=message.id, content_hash=content_hash, content=content))
            if not first:
                event(db, folder.mailbox_id, "message.content_changed", message.id)
    new_state = (
        message.is_read,
        message.is_draft,
        message.current_folder_graph_id,
        message.availability,
        message.categories,
    )
    if first:
        event(db, folder.mailbox_id, "message.first_captured", message.id)
    elif old_state != new_state:
        event(db, folder.mailbox_id, "message.live_state_changed", message.id)
    if message.has_attachments or "cid:" in (message.body_content or "").lower():
        if first or old_change_key != message.change_key:
            version = message.change_key or digest(item)
            ensure_job(
                db,
                folder.mailbox_id,
                "attachments",
                f"attachments:{message.id}:{version}",
                {"message_id": message.id, "source_version": version},
            )
    return message


def discover_folders(db, mailbox, graph):
    seen = set()
    paths = [graph.root + "/mailFolders?includeHiddenFolders=true"]
    while paths:
        path = paths.pop()
        for page in graph.pages(path):
            for item in page["value"]:
                seen.add(item["id"])
                folder = db.scalar(
                    select(Folder).where(Folder.mailbox_id == mailbox.id, Folder.graph_id == item["id"])
                )
                if folder is None:
                    folder = Folder(mailbox_id=mailbox.id, graph_id=item["id"], name=item["displayName"])
                    db.add(folder)
                    db.flush()
                    db.add(Checkpoint(folder_id=folder.id))
                folder.name = item["displayName"]
                folder.parent_graph_id = item.get("parentFolderId")
                folder.removed_at = None
                if item.get("childFolderCount", 0):
                    paths.append(
                        graph.root + f"/mailFolders/{segment(item['id'])}/childFolders"
                        "?includeHiddenFolders=true"
                    )
        # Discovery can be repeated after interruption; removals happen only after a complete walk.
    for folder in db.scalars(select(Folder).where(Folder.mailbox_id == mailbox.id)):
        if folder.graph_id not in seen:
            folder.removed_at = now()
            for member in db.scalars(
                select(Membership).where(Membership.folder_id == folder.id, Membership.present.is_(True))
            ):
                member.present = False
                message = db.get(Message, member.message_id)
                message.availability = "unknown"
                ensure_job(
                    db, mailbox.id, "resolve", f"resolve:{message.id}", {"message_id": message.id}, rearm=True
                )
    db.commit()


def sync_folder(db, folder, graph, max_pages=20):
    checkpoint = db.get(Checkpoint, folder.id)
    initial = graph.root + f"/mailFolders/{segment(folder.graph_id)}/messages/delta?$select={SELECT}"
    for _ in range(max_pages):
        url = checkpoint.next_url or checkpoint.delta_url or initial
        try:
            page = graph.get(url)
        except GraphError as exc:
            if exc.status == 410 or exc.code in {"syncStateNotFound", "InvalidDeltaToken"}:
                checkpoint.next_url = checkpoint.delta_url = None
                checkpoint.generation += 1
                checkpoint.baseline = True
                checkpoint.status = "rebuilding"
                checkpoint.last_error = exc.safe()
                db.commit()
                return False
            raise
        # All page mutations, events, jobs and cursor advance are one commit.
        for item in page["value"]:
            try:
                with db.begin_nested():
                    apply_message(db, folder, item, checkpoint.generation)
            except (ValueError, KeyError, TypeError) as exc:
                # A malformed item cannot block the folder indefinitely. Preserve
                # the raw item durably before committing the page checkpoint.
                item_hash = digest(item)
                issue = db.scalar(
                    select(SyncIssue).where(
                        SyncIssue.folder_id == folder.id, SyncIssue.item_hash == item_hash
                    )
                )
                if issue is None:
                    db.add(
                        SyncIssue(
                            folder_id=folder.id,
                            item_hash=item_hash,
                            item=item,
                            error={"code": type(exc).__name__},
                        )
                    )
                    event(db, folder.mailbox_id, "sync.item_quarantined", payload={"folder_id": folder.id})
        checkpoint.next_url = page.get("@odata.nextLink")
        if not checkpoint.next_url:
            checkpoint.delta_url = page["@odata.deltaLink"]
            if checkpoint.baseline:
                for member in db.scalars(
                    select(Membership).where(
                        Membership.folder_id == folder.id,
                        Membership.present.is_(True),
                        Membership.generation != checkpoint.generation,
                    )
                ):
                    member.present = False
                    message = db.get(Message, member.message_id)
                    message.availability = "unknown"
                    ensure_job(
                        db,
                        folder.mailbox_id,
                        "resolve",
                        f"resolve:{message.id}",
                        {"message_id": message.id},
                        rearm=True,
                    )
            checkpoint.baseline = False
            checkpoint.status = "ready"
            checkpoint.last_success_at = now()
            checkpoint.last_error = None
        else:
            checkpoint.status = "rebuilding" if checkpoint.baseline else "syncing"
        db.commit()
        if not checkpoint.next_url:
            return True
    return False


def resolve_message(db, message, graph):
    try:
        item = graph.get(graph.root + f"/messages/{segment(message.graph_id)}?$select={SELECT}")
    except GraphError as exc:
        if exc.status != 404:
            raise
        if message.availability != "not_found":
            message.availability = "not_found"
            event(db, message.mailbox_id, "message.unavailable", message.id)
        db.commit()
        return
    folder = db.scalar(
        select(Folder).where(
            Folder.mailbox_id == message.mailbox_id, Folder.graph_id == item.get("parentFolderId")
        )
    )
    if folder:
        cp = db.get(Checkpoint, folder.id)
        apply_message(db, folder, item, cp.generation)
        for member in db.scalars(select(Membership).where(Membership.message_id == message.id)):
            member.present = member.folder_id == folder.id
    else:
        message.availability = "present"
        message.current_folder_graph_id = item.get("parentFolderId")
        message.last_observed_at = now()
    db.commit()


def start_reconciliation(db, folder):
    # Rebaseline one folder without deleting retained data. Scheduled staggered by the dispatcher.
    cp = db.get(Checkpoint, folder.id)
    if cp.baseline or cp.next_url:
        return
    cp.generation += 1
    cp.baseline = True
    cp.next_url = cp.delta_url = None
    cp.status = "rebuilding"
    db.commit()
