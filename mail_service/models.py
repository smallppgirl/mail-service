from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from mail_service.db import now


def uid():
    return str(uuid4())


J = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    graph_user_id: Mapped[str] = mapped_column(Text, unique=True)
    address: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(J)
    mailbox_ids: Mapped[list] = mapped_column(J)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("mailbox_id", "graph_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    graph_id: Mapped[str] = mapped_column(Text)
    parent_graph_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    removed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class Checkpoint(Base):
    __tablename__ = "sync_checkpoints"
    folder_id: Mapped[str] = mapped_column(ForeignKey("folders.id"), primary_key=True)
    delta_url: Mapped[str | None] = mapped_column(Text)
    next_url: Mapped[str | None] = mapped_column(Text)
    query_version: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    baseline: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    last_success_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict | None] = mapped_column(J)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "graph_id"),
        Index("messages_received_idx", "mailbox_id", "received_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    graph_id: Mapped[str] = mapped_column(Text)
    internet_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    conversation_id: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[dict | None] = mapped_column(J)
    recipients: Mapped[dict | None] = mapped_column(J)
    received_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    modified_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    change_key: Mapped[str | None] = mapped_column(Text)
    is_read: Mapped[bool | None] = mapped_column(Boolean)
    is_draft: Mapped[bool | None] = mapped_column(Boolean)
    has_attachments: Mapped[bool | None] = mapped_column(Boolean)
    categories: Mapped[list | None] = mapped_column(J)
    availability: Mapped[str] = mapped_column(String(30), default="present")
    current_folder_graph_id: Mapped[str | None] = mapped_column(Text)
    body_type: Mapped[str | None] = mapped_column(String(20))
    body_content: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    last_observed_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class Membership(Base):
    __tablename__ = "message_folder_state"
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey("folders.id"), primary_key=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)


class MessageVersion(Base):
    __tablename__ = "message_versions"
    __table_args__ = (UniqueConstraint("message_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(J)
    captured_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class Blob(Base):
    __tablename__ = "blobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    app_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"))
    bucket: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text, unique=True)
    version_id: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text, default="application/octet-stream")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("message_id", "graph_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    graph_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)
    content_id: Mapped[str | None] = mapped_column(Text)
    source: Mapped[dict] = mapped_column(J)
    removed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class AttachmentVersion(Base):
    __tablename__ = "attachment_versions"
    __table_args__ = (UniqueConstraint("attachment_id", "source_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"), index=True)
    source_version: Mapped[str] = mapped_column(Text)
    blob_id: Mapped[str | None] = mapped_column(ForeignKey("blobs.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[dict | None] = mapped_column(J)
    captured_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class SendRequest(Base):
    __tablename__ = "send_requests"
    __table_args__ = (UniqueConstraint("app_id", "mailbox_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(J)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    draft_id: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    accepted_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    sent_observed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    unknown_since: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(J)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class SendAttachment(Base):
    __tablename__ = "send_attachments"
    send_id: Mapped[str] = mapped_column(ForeignKey("send_requests.id"), primary_key=True)
    blob_id: Mapped[str] = mapped_column(ForeignKey("blobs.id"), primary_key=True)
    graph_id: Mapped[str | None] = mapped_column(Text)
    upload_state: Mapped[dict | None] = mapped_column(J)
    status: Mapped[str] = mapped_column(String(30), default="pending")


class SendAttempt(Base):
    __tablename__ = "send_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    send_id: Mapped[str] = mapped_column(ForeignKey("send_requests.id"), index=True)
    phase: Mapped[str] = mapped_column(Text)
    started_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    graph_request_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, default="in_flight")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict] = mapped_column(J)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    run_after: Mapped[object] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_published_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(J)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("events_mailbox_cursor_idx", "mailbox_id", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(J)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class ProcessingState(Base):
    __tablename__ = "app_message_state"
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(J)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class SyncIssue(Base):
    __tablename__ = "sync_issues"
    __table_args__ = (UniqueConstraint("folder_id", "item_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    folder_id: Mapped[str] = mapped_column(ForeignKey("folders.id"), index=True)
    item_hash: Mapped[str] = mapped_column(String(64))
    item: Mapped[dict] = mapped_column(J)
    error: Mapped[dict] = mapped_column(J)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
