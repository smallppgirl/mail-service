import hashlib
from sqlalchemy import func, select
from mail_service.models import Application, Blob, Job, Mailbox, SendRequest
from mail_service.sync import apply_message


PAYLOAD = {
    "to": [{"address": "person@example.com"}],
    "subject": "Test",
    "body": {"type": "text", "content": "Hello"},
}


def test_idempotent_send_is_durable_and_conflicting_payload_rejected(client, database, mailbox):
    path = f"/v1/mailboxes/{mailbox[0].id}/send-requests"
    first = client.post(path, json=PAYLOAD, headers={"Idempotency-Key": "job-one"})
    assert first.status_code == 202, first.text
    second = client.post(path, json=PAYLOAD, headers={"Idempotency-Key": "job-one"})
    assert second.json()["id"] == first.json()["id"]
    conflict = client.post(
        path, json={**PAYLOAD, "subject": "Different"}, headers={"Idempotency-Key": "job-one"}
    )
    assert conflict.status_code == 409
    assert database.scalar(select(func.count()).select_from(SendRequest)) == 1
    assert database.scalar(select(func.count()).select_from(Job)) == 1


def test_key_and_timezone_are_required(client, mailbox):
    path = f"/v1/mailboxes/{mailbox[0].id}/send-requests"
    assert client.post(path, json=PAYLOAD).status_code == 422
    assert (
        client.post(
            path, json={**PAYLOAD, "not_before": "2026-10-11T10:00:00"}, headers={"Idempotency-Key": "one"}
        ).status_code
        == 422
    )


def test_app_cannot_read_another_mailbox_or_send_request(client, database, mailbox):
    other = Mailbox(graph_user_id="other", address="other@example.com")
    database.add(other)
    database.commit()
    assert client.get(f"/v1/mailboxes/{other.id}/messages").status_code == 403
    assert client.get("/v1/mailboxes", headers={"X-API-Key": "wrong"}).status_code == 401


def test_other_apps_uploads_cannot_be_attached(client, database, mailbox):
    other = Application(
        name="other",
        key_hash=hashlib.sha256(b"other-key").hexdigest(),
        scopes=["mail:send"],
        mailbox_ids=[mailbox[0].id],
    )
    database.add(other)
    database.flush()
    blob = Blob(
        mailbox_id=mailbox[0].id,
        app_id=other.id,
        bucket="bucket",
        key="private",
        name="secret",
        status="ready",
        size=10,
    )
    database.add(blob)
    database.commit()
    response = client.post(
        f"/v1/mailboxes/{mailbox[0].id}/send-requests",
        json={**PAYLOAD, "attachment_ids": [blob.id]},
        headers={"Idempotency-Key": "one"},
    )
    assert response.status_code == 403


def test_query_archive_and_processing_state_isolated(client, database, mailbox, email_item):
    message = apply_message(database, mailbox[1], email_item, 1)
    database.commit()
    result = client.get(f"/v1/mailboxes/{mailbox[0].id}/messages?q=invoice").json()
    assert result["items"][0]["id"] == message.id
    assert result["freshness"]["rebuilding"] is True
    state = client.put(
        f"/v1/messages/{message.id}/processing-state",
        json={"status": "processed", "metadata": {"invoice": "one"}},
    )
    assert state.status_code == 200
    assert client.get(f"/v1/messages/{message.id}/processing-state").json()["status"] == "processed"
    assert client.get(f"/v1/messages/{message.id}").json()["body_content"] == "Retain this invoice"


def test_pagination_has_no_duplicates(client, database, mailbox, email_item):
    for i in range(5):
        apply_message(database, mailbox[1], {**email_item, "id": f"message-{i}"}, 1)
    database.commit()
    path = f"/v1/mailboxes/{mailbox[0].id}/messages?limit=2"
    seen = []
    while True:
        result = client.get(path).json()
        seen.extend(item["id"] for item in result["items"])
        if not result["next_cursor"]:
            break
        path = f"/v1/mailboxes/{mailbox[0].id}/messages?limit=2&cursor={result['next_cursor']}"
    assert len(seen) == len(set(seen)) == 5


def test_cancel_queued_send(client, mailbox):
    response = client.post(
        f"/v1/mailboxes/{mailbox[0].id}/send-requests",
        json=PAYLOAD,
        headers={"Idempotency-Key": "cancel-one"},
    )
    request_id = response.json()["id"]
    result = client.post(f"/v1/send-requests/{request_id}/cancel")
    assert result.status_code == 200 and result.json()["status"] == "cancelled"
