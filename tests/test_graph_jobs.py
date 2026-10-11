from datetime import timedelta
import httpx
import pytest
from sqlalchemy import select
from mail_service.models import Job
from mail_service.db import now
from mail_service.graph import Graph, GraphError
from mail_service.jobs import due_for_publication, ensure_job
from mail_service import tasks


def test_graph_immutable_header_and_no_implicit_post_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": "Unavailable"}})

    graph = Graph(
        "test", client=httpx.Client(transport=httpx.MockTransport(handler)), token_provider=lambda: "token"
    )
    with pytest.raises(GraphError):
        graph.post("/users/test/messages")
    assert len(calls) == 1
    assert 'IdType="ImmutableId"' in calls[0].headers["Prefer"]


def test_continuation_url_cannot_leak_bearer_token():
    graph = Graph(
        "test",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        token_provider=lambda: "token",
    )
    with pytest.raises(ValueError):
        graph.get("https://attacker.example/users/test")
    graph.close()


def test_upload_does_not_send_graph_authorization_header():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"nextExpectedRanges": ["0-"]})

    graph = Graph(
        "test", client=httpx.Client(transport=httpx.MockTransport(handler)), token_provider=lambda: "token"
    )
    graph.upload_request("GET", "https://outlook.office.com/upload?token=secret")
    assert "Authorization" not in calls[0].headers


def test_lost_queue_message_is_republished(database, mailbox, monkeypatch):
    job = ensure_job(database, mailbox[0].id, "resolve", "resolve:test", {"message_id": "missing"})
    job.status = "queued"
    job.last_published_at = now() - timedelta(minutes=2)
    database.commit()
    calls = []
    monkeypatch.setattr(tasks.execute, "apply_async", lambda **kwargs: calls.append(kwargs))
    tasks.dispatch()
    assert calls[0]["args"] == [job.id]
    database.refresh(job)
    assert due_for_publication(job) is False


def test_duplicate_task_delivery_does_not_execute_terminal_job(database, mailbox, monkeypatch):
    job = ensure_job(database, mailbox[0].id, "folders", "folders:terminal", {})
    job.status = "done"
    database.commit()

    def fail(*args, **kwargs):
        pytest.fail("A terminal job must not call Graph")

    monkeypatch.setattr(tasks, "Graph", fail)
    tasks.execute(job.id)


def test_expired_running_job_is_reclaimed(database, mailbox, monkeypatch):
    job = ensure_job(database, mailbox[0].id, "folders", "folders:expired", {})
    job.status = "running"
    job.lease_until = now() - timedelta(minutes=1)
    database.commit()
    calls = []
    monkeypatch.setattr(tasks.execute, "apply_async", lambda **kwargs: calls.append(kwargs))
    tasks.dispatch()
    database.refresh(job)
    assert job.status == "queued" and len(calls) == 1


def test_failed_send_retry_requires_risk_acknowledgment(client, database, mailbox):
    from mail_service.models import SendRequest

    body = {
        "to": [{"address": "person@example.com"}],
        "subject": "Test",
        "body": {"type": "text", "content": "Hi"},
    }
    result = client.post(
        f"/v1/mailboxes/{mailbox[0].id}/send-requests", json=body, headers={"Idempotency-Key": "risk"}
    )
    request = database.get(SendRequest, result.json()["id"])
    request.status = "needs_review"
    database.commit()
    path = f"/v1/send-requests/{request.id}/retry"
    assert client.post(path, json={}).status_code == 409
    retry = client.post(path, json={"acknowledge_duplicate_risk": True})
    assert retry.status_code == 202 and retry.json()["id"] != request.id
    assert client.post(path, json={"acknowledge_duplicate_risk": True}).json()["id"] == retry.json()["id"]


def test_scheduler_rearms_only_unresolved_send_jobs(client, database, mailbox):
    from mail_service.models import SendRequest

    body = {
        "to": [{"address": "person@example.com"}],
        "subject": "Test",
        "body": {"type": "text", "content": "Hi"},
    }
    ids = []
    for key, status in [("one", "accepted"), ("two", "sent_observed")]:
        result = client.post(
            f"/v1/mailboxes/{mailbox[0].id}/send-requests", json=body, headers={"Idempotency-Key": key}
        )
        request = database.get(SendRequest, result.json()["id"])
        request.status = status
        job = database.scalar(select(Job).where(Job.dedupe_key == f"send:{request.id}"))
        job.status = "done"
        job.run_after = now() - timedelta(minutes=1)
        ids.append(job.id)
        database.commit()
    database.commit()
    tasks.schedule()
    database.expire_all()
    assert database.get(Job, ids[0]).status == "pending"
    assert database.get(Job, ids[1]).status == "done"
