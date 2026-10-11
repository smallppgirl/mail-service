import io
import json
from datetime import timedelta
import httpx
from mail_service.db import now
from mail_service.graph import Graph
from mail_service.models import Blob, SendAttachment, SendRequest
from mail_service.sending import run_send
from mail_service.storage import store_file


def prepare(database, mailbox, application, data):
    blob = Blob(
        mailbox_id=mailbox[0].id,
        app_id=application.id,
        bucket="mail-attachments",
        key="outgoing-file",
        name="report.bin",
    )
    database.add(blob)
    database.commit()
    store_file(blob, io.BytesIO(data))
    request = SendRequest(
        mailbox_id=mailbox[0].id,
        app_id=application.id,
        idempotency_key="attachment",
        payload_hash="hash",
        payload={
            "to": [{"address": "person@example.com"}],
            "subject": "Report",
            "body": {"type": "text", "content": "Attached"},
        },
    )
    database.add(request)
    database.flush()
    link = SendAttachment(send_id=request.id, blob_id=blob.id)
    database.add(link)
    database.commit()
    return request, link, blob


def test_large_upload_resumes_after_lost_chunk_response(database, mailbox, application, object_store):
    data = b"a" * (3 * 1024 * 1024 + 100)
    request, link, blob = prepare(database, mailbox, application, data)
    state = {"offset": 0, "lost": False, "complete": False, "puts": [], "sent": 0}

    def handler(http_request):
        path = http_request.url.path
        if http_request.method == "POST" and path.endswith("/messages"):
            return httpx.Response(201, json={"id": "draft"})
        if http_request.method == "GET" and path.endswith("/messages/draft"):
            return httpx.Response(200, json={"id": "draft", "isDraft": True})
        if path.endswith("/createUploadSession"):
            return httpx.Response(
                201,
                json={
                    "uploadUrl": "https://outlook.office.com/upload",
                    "expirationDateTime": (now() + timedelta(hours=1)).isoformat(),
                    "nextExpectedRanges": ["0-"],
                },
            )
        if http_request.url.host == "outlook.office.com":
            if http_request.method == "GET":
                return httpx.Response(200, json={"nextExpectedRanges": [f"{state['offset']}-"]})
            state["puts"].append(http_request.headers["Content-Range"])
            state["offset"] += len(http_request.content)
            if not state["lost"]:
                state["lost"] = True
                raise httpx.ReadTimeout("Chunk accepted, response lost")
            state["complete"] = True
            return httpx.Response(201, json={})
        if path.endswith("/attachments"):
            items = [{"id": "graph-file", "name": blob.name, "size": len(data)}] if state["complete"] else []
            return httpx.Response(200, json={"value": items})
        if path.endswith("/$value"):
            return httpx.Response(200, content=data)
        if path.endswith("/send"):
            state["sent"] += 1
            return httpx.Response(202)
        raise AssertionError(f"Unexpected {http_request.method} {path}")

    graph = Graph(
        "test-user",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "token",
    )
    run_send(database, request, graph)
    assert request.status == "preparation_unknown" and state["sent"] == 0
    assert state["offset"] == 3 * 1024 * 1024
    run_send(database, request, graph)
    assert request.status == "accepted" and link.status == "ready"
    assert state["sent"] == 1 and len(state["puts"]) == 2
    assert state["puts"][1].startswith("bytes 3145728-")


def test_small_attachment_response_loss_recovers_existing_bytes(database, mailbox, application, object_store):
    data = b"small-file"
    request, link, blob = prepare(database, mailbox, application, data)
    state = {"uploaded": False, "attachment_posts": 0, "sent": 0}

    def handler(http_request):
        path = http_request.url.path
        if http_request.method == "POST" and path.endswith("/messages"):
            return httpx.Response(201, json={"id": "draft"})
        if http_request.method == "GET" and path.endswith("/messages/draft"):
            return httpx.Response(200, json={"id": "draft", "isDraft": True})
        if path.endswith("/attachments"):
            if http_request.method == "POST":
                state["attachment_posts"] += 1
                assert json.loads(http_request.content)["name"] == blob.name
                state["uploaded"] = True
                raise httpx.ReadTimeout("File accepted, response lost")
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "file", "name": blob.name, "size": len(data)}]
                    if state["uploaded"]
                    else []
                },
            )
        if path.endswith("/$value"):
            return httpx.Response(200, content=data)
        if path.endswith("/send"):
            state["sent"] += 1
            return httpx.Response(202)
        raise AssertionError(path)

    graph = Graph(
        "test-user",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "token",
    )
    run_send(database, request, graph)
    assert request.status == "preparation_unknown"
    run_send(database, request, graph)
    assert request.status == "accepted" and link.status == "ready"
    assert state["attachment_posts"] == state["sent"] == 1
