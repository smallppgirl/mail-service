import boto3
from moto import mock_aws
from mail_service import storage
import hashlib
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from mail_service import db as database_module
from mail_service.api import app
from mail_service.models import Application, Base, Checkpoint, Folder, Mailbox


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    # SQLite's legacy transaction mode otherwise commits an outermost SAVEPOINT
    # independently. Mirror real PostgreSQL page transaction behavior in tests.
    @event.listens_for(engine, "connect")
    def configure_transactions(connection, _):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    monkeypatch.setattr(database_module, "engine", lambda: engine)
    with sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


@pytest.fixture
def mailbox(database):
    mailbox = Mailbox(graph_user_id="test-user", address="mail@example.com")
    database.add(mailbox)
    database.flush()
    folder = Folder(mailbox_id=mailbox.id, graph_id="inbox-graph", name="Inbox")
    database.add(folder)
    database.flush()
    database.add(Checkpoint(folder_id=folder.id))
    database.commit()
    return mailbox, folder


@pytest.fixture
def application(database, mailbox):
    row = Application(
        name="app-one",
        key_hash=hashlib.sha256(b"test-key").hexdigest(),
        scopes=["mail:read", "mail:send", "mail:process", "mail:sync"],
        mailbox_ids=[mailbox[0].id],
    )
    database.add(row)
    database.commit()
    return row


@pytest.fixture
def client(database, application):
    with TestClient(app) as client:
        client.headers["X-API-Key"] = "test-key"
        yield client


class FakeGraph:
    root = "/users/test-user"
    last_response = None

    def __init__(self, pages=None):
        self.responses = list(pages or [])
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, path, **kwargs):
        self.calls.append(("POST", path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def pages(self, path, **kwargs):
        while path:
            response = self.get(path, **kwargs)
            yield response
            path = response.get("@odata.nextLink")

    def download(self, path, file, limit):
        file.write(b"attachment-data")
        file.seek(0)


@pytest.fixture
def graph_factory():
    return FakeGraph


@pytest.fixture
def email_item():
    return {
        "id": "immutable-one",
        "parentFolderId": "inbox-graph",
        "subject": "Invoice",
        "receivedDateTime": "2026-10-10T10:00:00Z",
        "isRead": False,
        "isDraft": False,
        "changeKey": "version-one",
        "hasAttachments": False,
        "sender": {"emailAddress": {"address": "sender@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "mail@example.com"}}],
        "body": {"contentType": "text", "content": "Retain this invoice"},
    }


@pytest.fixture
def object_store(monkeypatch):
    with mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        client.create_bucket(Bucket="mail-attachments")
        client.put_bucket_versioning(Bucket="mail-attachments", VersioningConfiguration={"Status": "Enabled"})
        monkeypatch.setattr(storage, "s3", lambda: client)
        yield client
