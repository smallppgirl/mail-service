import hashlib
import tempfile
from functools import lru_cache
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import select
from mail_service.config import settings
from mail_service.db import now
from mail_service.graph import GraphError, segment
from mail_service.models import Attachment, AttachmentVersion, Blob, Message, uid
from mail_service.sync import event


@lru_cache
def s3():
    s = settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3},
            connect_timeout=10,
            read_timeout=30,
            s3={"addressing_style": "path"},
        ),
    )


def initialize_bucket():
    try:
        s3().head_bucket(Bucket=settings().s3_bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3().create_bucket(Bucket=settings().s3_bucket)
    s3().put_bucket_versioning(Bucket=settings().s3_bucket, VersioningConfiguration={"Status": "Enabled"})


def store_file(blob, file):
    """Stable object key + verified checksum make upload-before-commit replay safe."""
    digest = hashlib.sha256()
    size = 0
    file.seek(0)
    while chunk := file.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    if size > settings().max_attachment_bytes:
        raise ValueError("Attachment exceeds configured size limit")
    sha = digest.hexdigest()
    try:
        old = s3().head_object(Bucket=blob.bucket, Key=blob.key)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        old = None
    if old is None or old.get("Metadata", {}).get("sha256") != sha or old["ContentLength"] != size:
        file.seek(0)
        s3().upload_fileobj(
            file,
            blob.bucket,
            blob.key,
            ExtraArgs={
                "ContentType": blob.content_type,
                "Metadata": {"sha256": sha},
            },
        )
    head = s3().head_object(Bucket=blob.bucket, Key=blob.key)
    if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha:
        raise IOError("Stored attachment verification failed")
    blob.sha256 = sha
    blob.size = size
    blob.version_id = head.get("VersionId")
    blob.status = "ready"


def object_args(blob):
    args = {"Bucket": blob.bucket, "Key": blob.key}
    if blob.version_id:
        args["VersionId"] = blob.version_id
    return args


def open_blob(blob):
    return s3().get_object(**object_args(blob))["Body"]


def capture_attachments(db, message_id, source_version, graph):
    message = db.get(Message, message_id)
    root = graph.root + f"/messages/{segment(message.graph_id)}/attachments"
    seen = set()
    try:
        for page in graph.pages(root):
            for item in page["value"]:
                seen.add(item["id"])
                attachment = db.scalar(
                    select(Attachment).where(
                        Attachment.message_id == message.id, Attachment.graph_id == item["id"]
                    )
                )
                if attachment is None:
                    attachment = Attachment(message_id=message.id, graph_id=item["id"])
                    db.add(attachment)
                attachment.kind = item["@odata.type"].split(".")[-1]
                attachment.name = item.get("name") or "attachment"
                attachment.content_type = item.get("contentType") or "application/octet-stream"
                attachment.size = item.get("size", 0)
                attachment.is_inline = item.get("isInline", False)
                attachment.content_id = item.get("contentId")
                attachment.source = {k: v for k, v in item.items() if k != "contentBytes"}
                attachment.removed_at = None
                db.flush()
                version = db.scalar(
                    select(AttachmentVersion).where(
                        AttachmentVersion.attachment_id == attachment.id,
                        AttachmentVersion.source_version == source_version,
                    )
                )
                if version is None:
                    version = AttachmentVersion(attachment_id=attachment.id, source_version=source_version)
                    db.add(version)
                    db.flush()
                if version.status in {"ready", "reference", "unavailable", "too_large"}:
                    continue
                if attachment.kind == "referenceAttachment":
                    version.status = "reference"
                    db.commit()
                    continue
                if attachment.size > settings().max_attachment_bytes:
                    version.status = "too_large"
                    db.commit()
                    continue
                blob = db.get(Blob, version.blob_id) if version.blob_id else None
                if blob is None:
                    blob_id = uid()
                    blob = Blob(
                        id=blob_id,
                        mailbox_id=message.mailbox_id,
                        bucket=settings().s3_bucket,
                        key=f"mailboxes/{message.mailbox_id}/blobs/{blob_id}",
                        name=attachment.name,
                        content_type=attachment.content_type,
                    )
                    db.add(blob)
                    db.flush()
                    version.blob_id = blob.id
                db.commit()  # Reserve the object key before upload.
                try:
                    with tempfile.TemporaryFile() as file:
                        graph.download(
                            root + f"/{segment(attachment.graph_id)}/$value",
                            file,
                            settings().max_attachment_bytes,
                        )
                        store_file(blob, file)
                    version.status = "ready"
                    version.captured_at = now()
                    event(
                        db,
                        message.mailbox_id,
                        "attachment.ready",
                        message.id,
                        {"attachment_id": attachment.id, "version_id": version.id},
                    )
                    db.commit()
                except GraphError as exc:
                    if exc.status != 404:
                        raise
                    version.status = "unavailable"
                    version.error = exc.safe()
                    db.commit()
                except ValueError:
                    version.status = "too_large"
                    db.commit()
    except GraphError as exc:
        if exc.status != 404:
            raise
        message.availability = "unknown"
        event(
            db,
            message.mailbox_id,
            "attachment.capture_unavailable",
            message.id,
            {"source_version": source_version},
        )
        db.commit()
        return
    for attachment in db.scalars(select(Attachment).where(Attachment.message_id == message.id)):
        if attachment.graph_id not in seen:
            attachment.removed_at = now()
    db.commit()
