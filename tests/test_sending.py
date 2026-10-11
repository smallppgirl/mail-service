from datetime import timedelta
import httpx
from sqlalchemy import select
from mail_service.db import now
from mail_service.graph import GraphError
from mail_service.models import SendAttempt, SendRequest
from mail_service.sending import MARKER, run_send


def new_request(database, mailbox, application):
    row = SendRequest(
        mailbox_id=mailbox[0].id,
        app_id=application.id,
        idempotency_key="one",
        payload_hash="hash",
        payload={
            "to": [{"address": "person@example.com"}],
            "subject": "Hello",
            "body": {"type": "text", "content": "Hi"},
        },
    )
    database.add(row)
    database.commit()
    return row


def test_acceptance_is_not_delivery_and_sent_copy_reconciles(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    graph = graph_factory([{"id": "draft-immutable"}, {}])
    run_send(database, request, graph)
    assert request.status == "accepted"
    assert request.sent_observed_at is None
    assert request.draft_id == "draft-immutable"
    run_send(
        database,
        request,
        graph_factory([{"id": request.draft_id, "isDraft": False, "sentDateTime": "2026-10-11T01:00:00Z"}]),
    )
    assert request.status == "sent_observed"


def test_lost_send_response_is_not_automatically_resent(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    graph = graph_factory([{"id": "draft"}, httpx.ReadTimeout("lost response")])
    run_send(database, request, graph)
    assert request.status == "submission_unknown"
    retry = graph_factory([{"id": "draft", "isDraft": True}])
    run_send(database, request, retry)
    assert request.status == "submission_unknown"
    assert not any(call[0] == "POST" for call in retry.calls)
    request.unknown_since = now() - timedelta(hours=1)
    database.commit()
    run_send(database, request, graph_factory([{"id": "draft", "isDraft": True}]))
    assert request.status == "needs_review"


def test_worker_crash_after_submitting_goes_to_reconciliation(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    request.draft_id = "draft"
    request.status = "submitting"
    request.unknown_since = now()
    database.commit()
    graph = graph_factory([{"id": "draft", "isDraft": False, "sentDateTime": "2026-10-11T01:00:00Z"}])
    run_send(database, request, graph)
    assert request.status == "sent_observed"
    assert len(graph.calls) == 1 and graph.calls[0][0] == "GET"


def test_lost_draft_creation_response_recovers_marker(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    run_send(database, request, graph_factory([httpx.ReadTimeout("lost draft response")]))
    assert request.status == "preparation_unknown"
    recovered = {
        "id": "recovered-draft",
        "isDraft": True,
        "internetMessageHeaders": [{"name": MARKER, "value": request.id}],
    }
    graph = graph_factory([{"value": [recovered]}, {"value": []}, {}])
    run_send(database, request, graph)
    assert request.draft_id == "recovered-draft" and request.status == "accepted"
    posts = [call for call in graph.calls if call[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/send")


def test_send_5xx_is_ambiguous(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    run_send(database, request, graph_factory([{"id": "draft"}, GraphError(503)]))
    assert request.status == "submission_unknown"


def test_explicit_429_keeps_existing_draft_for_safe_retry(database, mailbox, application, graph_factory):
    import pytest

    request = new_request(database, mailbox, application)
    with pytest.raises(GraphError):
        run_send(database, request, graph_factory([{"id": "draft"}, GraphError(429, retry_after=60)]))
    assert request.status == "draft_ready"
    assert request.draft_id == "draft"
    graph = graph_factory([{}])
    run_send(database, request, graph)
    assert len(graph.calls) == 1
    assert request.status == "accepted"


def test_attempt_remains_auditable_after_timeout(database, mailbox, application, graph_factory):
    request = new_request(database, mailbox, application)
    run_send(database, request, graph_factory([{"id": "draft"}, httpx.ReadTimeout("lost")]))
    attempts = database.scalars(select(SendAttempt).where(SendAttempt.send_id == request.id)).all()
    assert {a.phase for a in attempts} == {"create_draft", "submit"}
    assert any(a.outcome == "in_flight" for a in attempts)
