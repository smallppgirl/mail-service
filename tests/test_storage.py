import io
import hashlib
from sqlalchemy import func, select
from mail_service import storage
from mail_service.models import AttachmentVersion, Blob, Event
from mail_service.sync import apply_message


def test_upload_before_commit_can_be_replayed_without_new_object_version(database, mailbox, object_store):
    blob = Blob(mailbox_id=mailbox[0].id, bucket="mail-attachments", key="stable/key", name="file.txt")
    database.add(blob)
    database.commit()
    storage.store_file(blob, io.BytesIO(b"hello"))
    original = blob.version_id
    database.rollback()
    storage.store_file(blob, io.BytesIO(b"hello"))
    database.commit()
    assert blob.version_id == original
    assert blob.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert storage.open_blob(blob).read() == b"hello"


def test_capture_is_replay_safe_and_inline_preserved(
    database, mailbox, object_store, graph_factory, email_item
):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    attachment = {
        "id": "attachment-one",
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "image.png",
        "contentType": "image/png",
        "size": 15,
        "isInline": True,
        "contentId": "image",
    }
    storage.capture_attachments(database, message.id, "v1", graph_factory([{"value": [attachment]}]))
    version = database.scalar(select(AttachmentVersion))
    assert version.status == "ready"
    blob_id = version.blob_id
    storage.capture_attachments(database, message.id, "v1", graph_factory([{"value": [attachment]}]))
    assert database.scalar(select(func.count()).select_from(Blob)) == 1
    assert version.blob_id == blob_id
    assert (
        database.scalar(select(func.count()).select_from(Event).where(Event.kind == "attachment.ready")) == 1
    )


def test_reference_attachment_is_metadata_not_file(database, mailbox, graph_factory, email_item):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    attachment = {
        "id": "reference",
        "@odata.type": "#microsoft.graph.referenceAttachment",
        "name": "Link",
        "sourceUrl": "https://example.com/private-file",
    }
    storage.capture_attachments(database, message.id, "v1", graph_factory([{"value": [attachment]}]))
    assert database.scalar(select(AttachmentVersion)).status == "reference"
    assert database.scalar(select(func.count()).select_from(Blob)) == 0
