from sqlalchemy import func, select
import pytest
from mail_service.graph import GraphError
from mail_service.models import Checkpoint, Event, Folder, Job, Membership, Message, MessageVersion
from mail_service.sync import apply_message, resolve_message, sync_folder


def test_resume_committed_page_after_crash(database, mailbox, graph_factory, email_item):
    folder = mailbox[1]
    next_url = "https://graph.microsoft.com/v1.0/users/test-user/next"
    graph = graph_factory([{"value": [email_item], "@odata.nextLink": next_url}, RuntimeError("crash")])
    with pytest.raises(RuntimeError):
        sync_folder(database, folder, graph)
    database.rollback()
    assert database.get(Checkpoint, folder.id).next_url == next_url
    assert database.scalar(select(func.count()).select_from(Message)) == 1
    second = graph_factory([{"value": [], "@odata.deltaLink": "delta-completed"}])
    sync_folder(database, folder, second)
    assert second.calls[0] == ("GET", next_url)
    assert database.get(Checkpoint, folder.id).delta_url == "delta-completed"


def test_replay_does_not_duplicate_versions_or_events(database, mailbox, email_item):
    apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    count = database.scalar(select(func.count()).select_from(Event))
    apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    assert database.scalar(select(func.count()).select_from(MessageVersion)) == 1
    assert database.scalar(select(func.count()).select_from(Event)) == count


def test_page_transaction_rolls_back_without_advancing_cursor(
    database, mailbox, graph_factory, email_item, monkeypatch
):
    original_commit = database.commit

    def fail_commit():
        raise RuntimeError("Database commit failed")

    monkeypatch.setattr(database, "commit", fail_commit)
    graph = graph_factory([{"value": [email_item], "@odata.deltaLink": "must-not-save"}])
    with pytest.raises(RuntimeError):
        sync_folder(database, mailbox[1], graph)
    database.rollback()
    monkeypatch.setattr(database, "commit", original_commit)
    assert database.get(Checkpoint, mailbox[1].id).delta_url is None
    assert database.scalar(select(func.count()).select_from(Message)) == 0


def test_folder_removal_keeps_body_and_move_resolves(database, mailbox, graph_factory, email_item):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    apply_message(database, mailbox[1], {"id": email_item["id"], "@removed": {"reason": "deleted"}}, 1)
    database.commit()
    assert message.availability == "unknown"
    assert message.body_content == "Retain this invoice"
    folder = Folder(mailbox_id=mailbox[0].id, graph_id="destination", name="Archive")
    database.add(folder)
    database.flush()
    database.add(Checkpoint(folder_id=folder.id))
    database.commit()
    resolve_message(database, message, graph_factory([{**email_item, "parentFolderId": "destination"}]))
    assert message.availability == "present"
    assert message.current_folder_graph_id == "destination"
    assert database.get(Membership, (message.id, mailbox[1].id)).present is False


def test_partial_update_preserves_absent_fields(database, mailbox, email_item):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    apply_message(database, mailbox[1], {"id": email_item["id"], "isRead": True}, 1)
    database.commit()
    assert message.is_read is True
    assert message.subject == "Invoice"
    assert message.body_content == "Retain this invoice"


def test_invalid_token_rebuild_retains_archive(database, mailbox, graph_factory, email_item):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    cp = database.get(Checkpoint, mailbox[1].id)
    cp.delta_url = "old-delta"
    cp.baseline = False
    database.commit()
    assert sync_folder(database, mailbox[1], graph_factory([GraphError(410)])) is False
    assert cp.baseline and cp.generation == 2
    assert message.body_content
    sync_folder(database, mailbox[1], graph_factory([{"value": [], "@odata.deltaLink": "new-delta"}]))
    assert message.availability == "unknown"
    assert database.scalar(select(Job).where(Job.kind == "resolve"))
    resolve_message(database, message, graph_factory([GraphError(404)]))
    assert message.availability == "not_found"
    assert database.scalar(select(func.count()).select_from(MessageVersion)) == 1


def test_inline_only_message_queues_attachment_capture(database, mailbox, email_item):
    item = {**email_item, "body": {"contentType": "html", "content": '<img src="cid:image">'}}
    message = apply_message(database, mailbox[1], item, 1)
    database.commit()
    assert message.has_attachments is False
    assert database.scalar(select(Job).where(Job.kind == "attachments"))


def test_malformed_item_is_quarantined_before_cursor_advances(database, mailbox, graph_factory, email_item):
    from mail_service.models import SyncIssue

    broken = {**email_item, "id": "broken", "receivedDateTime": "invalid-date"}
    graph = graph_factory([{"value": [broken, email_item], "@odata.deltaLink": "safe-checkpoint"}])
    sync_folder(database, mailbox[1], graph)
    assert database.scalar(select(func.count()).select_from(Message)) == 1
    assert database.scalar(select(SyncIssue)).item == broken
    assert database.get(Checkpoint, mailbox[1].id).delta_url == "safe-checkpoint"
