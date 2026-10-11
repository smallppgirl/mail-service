# Hybrid Outlook Mail Service

A Python service for several applications sharing one Microsoft 365 mailbox.
It captures mailbox content in PostgreSQL, stores attachments in private MinIO
objects, tracks current Outlook state, and accepts durable asynchronous send
requests. Docker Compose includes all required services.

**Status:** runnable initial implementation with automated mocked-Graph tests.
Live tenant access and an end-to-end container deployment must be validated in
your environment. This project does not contain Microsoft credentials.

## Requirements

- Docker Engine/Desktop with Docker Compose v2 (Linux, macOS or Windows).
- A Microsoft 365 organizational mailbox with application Graph access.
- An Entra application authorized for `Mail.ReadWrite` and `Mail.Send`.
- Tenant ID, client ID, client secret and mailbox user ID/UPN.

Client-credentials access is for organizational mailboxes. Personal Outlook.com
accounts require a different authentication flow. Configure effective mailbox
permissions through Exchange Online Application RBAC; broad Entra grants can be
additive, so verify access to the intended mailbox and denial to others.

## Quick start

From this directory:

```bash
python3 scripts/configure.py
```

This creates a private `.env` with random PostgreSQL, Redis, MinIO and API
passwords. It refuses to overwrite an existing file. Edit these six fields:

```dotenv
GRAPH_TENANT_ID=your-tenant-id
GRAPH_CLIENT_ID=your-client-id
GRAPH_CLIENT_SECRET='your-client-secret'
GRAPH_USER_ID=mailbox-user-id-or-upn
MAILBOX_ADDRESS=mailbox@example.com
```

Single-quote a Graph secret containing `$` or other interpolation characters in
the dotenv file. Do not commit `.env` or put credentials into the ZIP/repository.

```bash
docker compose config --quiet
docker compose up --build -d
docker compose logs -f init api worker sender beat
```

The initialization service applies Alembic migrations, creates the mailbox/API
administrator record, creates the private attachment bucket, and enables bucket
versioning. Other application services start after initialization succeeds.
Initial synchronization begins automatically and processes pages in bounded
chunks; a large mailbox can take multiple worker runs to finish.

- API: <http://localhost:8000>
- Interactive API documentation: <http://localhost:8000/docs>
- MinIO console: <http://localhost:9001> (uses your configured MinIO credentials)
- PostgreSQL and Redis have no published host ports.

API and MinIO console are bound to localhost by default. For access from another
machine, place the API behind an HTTPS reverse proxy and keep infrastructure
ports private. Set `API_PORT` to change the local API port.

## Authenticate applications

Use the `BOOTSTRAP_API_KEY` from `.env` in `X-API-Key` for initial administration.
Discover the internal mailbox UUID:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/v1/mailboxes
```

Create a separate key for each application; set the values below to your own:

```bash
docker compose exec api mail-service create-app \
  --name invoice-app \
  --mailbox YOUR_INTERNAL_MAILBOX_UUID \
  --scopes mail:read,mail:send,mail:process
```

The generated key is shown once. Only its SHA-256 hash is stored in PostgreSQL.
An application receives only its configured mailbox grants. Available scopes:
`mail:read`, `mail:send`, `mail:process`, `mail:sync`; `admin` is unrestricted
service administration and should not be given to ordinary applications.
Keys can be disabled by setting the application's `enabled` field false in the
database; key rotation creates a new application identity in this version.

## Query messages

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/v1/mailboxes/$MAILBOX_ID/messages?q=invoice&limit=50"
```

Filters: `q`, `availability`, `folder_id`, `received_after`, `is_read`, `limit`,
`cursor`. Date filters must include a timezone. PostgreSQL full-text search uses
the subject and stored body. Results include freshness and rebuilding status.
Keyset pagination uses received time (or first capture time) and the internal ID.

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/v1/messages/$MESSAGE_ID"

curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/v1/mailboxes/$MAILBOX_ID/sync-status"
```

`messages` has one row per mailbox item, identified by a Graph immutable ID.
`message_versions` preserves captured content versions; deletion never erases
these rows. Unavailable email stays searchable unless you filter for
`availability=present`. `unknown` is a temporary unresolved location;
`not_found` means a mailbox-level GET returned 404. These are observations, not
proof of permanent deletion across archive mailboxes or inaccessible stores.

All discovered folders, including child/hidden folders, are tracked initially.
Disable unwanted folders with `PATCH /v1/folders/{id}` and `{"tracked":false}`.
Live state for an untracked folder stops refreshing; retained content remains.

Treat stored HTML as untrusted content when displaying it in another app.

## Attachments

Captured attachments appear at `GET /v1/messages/{id}/attachments`. They include
inline content IDs and capture versions/statuses. Download with
`GET /v1/attachments/{id}/download?version_id=...`; omit the version to get the
latest captured version. Downloads are authenticated API streams, so callers
do not need MinIO credentials or a directly reachable MinIO endpoint.

Outgoing files use a multipart upload:

```bash
curl -H "X-API-Key: $API_KEY" -F "file=@invoice.pdf" \
  "http://localhost:8000/v1/mailboxes/$MAILBOX_ID/uploads"
```

The returned `id` is used in a send request's `attachment_ids`. Uploads belong
to an application/mailbox and cannot be referenced by another application.
This version streams upload content through the API instead of handing out
direct presigned upload URLs. Set request-size limits on your reverse proxy
as well: multipart parsing may spool content before the application size check.

Default limits are 20 MiB per captured/outgoing attachment and a conservative
25 MiB total outgoing message budget including base64 expansion. Set them to
match your actual Exchange message limits. Graph attachments under 3 MB use a
normal POST; larger files use resumable upload sessions. Test large attachments
on a shared/delegated mailbox before relying on that path.

Reference/cloud-link attachments are retained as metadata; this service does
not follow links with separate external authorization. `itemAttachment` content
is captured through Graph's raw representation where supported. A file deleted
before capture is marked unavailable. A file over the configured limit is
marked `too_large`; files successfully captured stay in MinIO after Outlook
deletion. Incoming bytes are not malware-scanned by this initial implementation.

## Send email

```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: invoice-2026-1001" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/v1/mailboxes/$MAILBOX_ID/send-requests" \
  -d '{
    "to": [{"address": "customer@example.com"}],
    "subject": "Invoice",
    "body": {"type": "text", "content": "Please see the attachment."},
    "attachment_ids": ["YOUR_UPLOAD_ID"],
    "not_before": "2026-10-11T09:00:00+08:00"
  }'
```

`attachment_ids` and `not_before` are optional. `cc` and `bcc` are supported.
New outgoing files are ordinary file attachments; inline outgoing MIME and
reply/forward endpoints are not included in this version.

A successful API `202` means **durably queued in this service**, not sent.
Repeated application/mailbox/idempotency key with the same payload returns the
same request; a different payload returns `409`. Payload comparison includes
any explicit `not_before` value. Keep the same payload when retrying a client
request.

Poll `GET /v1/send-requests/{id}`:

```text
queued → draft_creating → preparing → draft_ready → submitting
       → accepted → sent_observed
```

`accepted` means Graph accepted submission. `sent_observed` means the service
observed the correlated message with a non-draft state and sent timestamp.
Neither state proves recipient delivery. `delivery_status` is `unknown` in this
version; automatic NDR/bounce classification is not implemented.

The service creates and saves an immutable Graph draft ID before sending. An
opaque `x-mail-service-request-id` header supports recovery after draft creation
response loss. Each external create/send attempt has a durable audit row.

### Ambiguous outcomes

A send timeout, lost response, worker crash during submission, or server error
can mean Graph processed the request. Such requests go to `submission_unknown`,
or remain `submitting` until recovery. They are reconciled rather than blindly
resent. Unresolved uncertainty becomes `needs_review` after the configurable
window (15 minutes by default). Attachment preparation ambiguity is similarly
held for reconciliation. Explicit `429` rejection is safely retried using
`Retry-After`.

Use `POST /v1/send-requests/{id}/cancel` before submission. An operation already
holding the mailbox lock may cause `409`; cancellation cannot recall sent mail.
Cancelled drafts may remain in Outlook for inspection.

Use `POST /v1/send-requests/{id}/retry` for `failed` or `needs_review`. It creates
a new correlated request and keeps the old record. For `needs_review`, the body
must include `{"acknowledge_duplicate_risk":true}`. Retrying can duplicate an
earlier successful external send. Repeated retries of the same request return
the same retry request; retry that child separately if necessary.

## Durable jobs and synchronization recovery

PostgreSQL holds all business state; Redis only transports job IDs. The
dispatcher republishes queued jobs unclaimed for 60 seconds. Workers claim jobs
in the database, and duplicate broker delivery cannot rerun a completed job.
Running jobs have leases and are reclaimed after expiry only if mailbox
ownership is available. Redis uses AOF and `noeviction`.

All Graph operations for a mailbox are serialized with a PostgreSQL advisory
lock across worker processes. The sender queue also starts with concurrency one.
This deliberately favors correctness over maximum throughput. Sending does not
remove the need for a Graph call per message and can use more calls than direct
`sendMail`, because drafts and attachments support better recovery.

Delta synchronization uses one checkpoint per folder. Page data, events,
attachment jobs, and the next URL commit together. Lost/expired delta state
starts a new generation without deleting captured content. A folder removal is
resolved at mailbox scope before being classified unavailable. Unseen members
are reconciled only when a complete baseline finishes. A daily rebaseline
repairs current state for documented immutable-ID delta move edge cases.

A malformed item is quarantined in `sync_issues` in the same transaction as the
page checkpoint. This prevents one bad item from blocking all mail. Inspect
`GET /v1/mailboxes/{id}/sync-issues`; retry with
`POST /v1/sync-issues/{id}/retry` after correcting the underlying parser issue.
The raw item is retained in PostgreSQL for administrator debugging.

Failed jobs remain durable. Administrators can inspect
`GET /v1/mailboxes/{id}/jobs?status=failed` and retry non-send jobs with
`POST /v1/jobs/{id}/retry`. Send jobs must use the send-request retry path.
`POST /v1/mailboxes/{id}/sync-requests` coalesces an explicit refresh request.

Captured attachment uploads reserve a stable key before uploading. SHA-256 and
size checks make upload-before-database-commit recovery repeatable. Bucket
versioning preserves referenced object versions. Content-addressed deduplication
and automatic orphan/retention cleanup are not included; retain pending and
unreferenced objects until a future cleanup policy is configured.

## Events and application processing

`GET /v1/mailboxes/{id}/events?after=0` returns a durable local change feed.
Consumers save `next_cursor` after their own durable processing and deduplicate
by event ID. Events are retained indefinitely in this version.

Use `PUT /v1/messages/{id}/processing-state` with
`{"status":"processed","metadata":{"invoice_id":"123"}}`. This state is
per application and does not change Outlook read/unread status. Read it with
the matching GET endpoint. No general Outlook mutation/delete API is exposed.

## Layout and database

```text
mail_service/api.py       REST APIs and application authorization
mail_service/models.py    SQLAlchemy models
mail_service/graph.py     Graph authentication and HTTP transport
mail_service/sync.py      Folder discovery, delta, moves and rebuilds
mail_service/storage.py   S3 storage and attachment capture
mail_service/sending.py   Draft preparation, submission and reconciliation
mail_service/tasks.py     Celery scheduling, dispatch and workers
migrations/               Frozen initial Alembic migration
tests/                    Recovery, authorization, sync and S3 tests
```

Tables: `mailboxes`, `applications`, `folders`, `sync_checkpoints`, `messages`,
`message_folder_state`, `message_versions`, `attachments`, `attachment_versions`,
`blobs`, `send_requests`, `send_attachments`, `send_attempts`, `jobs`, `events`,
`app_message_state`, `sync_issues`. PostgreSQL uses JSONB for structured content
and a GIN full-text index for message search. SQLite is only an offline testing
backend; it does not provide production mailbox-lock semantics.

To add migrations after modifying models:

```bash
docker compose exec api alembic revision --autogenerate -m "describe change"
```

For development, run Alembic in a local checkout with a reachable PostgreSQL
URL so generated migrations are saved to your source tree; the runtime container
source is not mounted or writable. Review generated changes and rebuild the
image. Apply migrations once through `init`, rather than concurrently in every
worker. Dependency lockfiles record the exact versions used for verification.

## Tests

With Docker:

```bash
docker compose --profile test run --build --rm tests
```

The test service requires only PostgreSQL and runs a real PostgreSQL migration/
advisory-lock test in a disposable schema. It never contacts Microsoft or sends
real email. Other tests use isolated SQLite and mocked Graph/S3.

Locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -r requirements-test.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Set `TEST_DATABASE_URL` to a reachable PostgreSQL database to enable the
integration test locally. It creates/drops only its randomly named schema.
Without that setting, the integration test is explicitly skipped.

## Operations

```bash
docker compose ps
docker compose logs --tail=100 worker sender beat
docker compose restart worker sender
docker compose down
```

Named volumes persist data across container restarts and `docker compose down`.
Do not use `down -v` unless you intend to delete PostgreSQL, Redis and MinIO data.
Monitor sync freshness, failed jobs, quarantined items, attachment states, and
requests stuck in accepted/unknown states. Health readiness checks PostgreSQL;
it does not certify Graph authorization or end-to-end email delivery.

Back up PostgreSQL and MinIO together. Keep referenced MinIO versions long
enough for PostgreSQL point-in-time recovery. After restoring a database, pause
send workers and reconcile previously submitting requests against Outlook
before resuming: an older database backup may predate an already completed send.
Verify restoration on a separate instance.

The example Compose deployment uses MinIO root credentials internally for
bootstrap and application access. For production, provision a dedicated bucket
policy/application identity and move credentials into your secret-management
system. Graph secrets and preauthenticated upload-session URLs must not be
logged. Administrator list APIs omit job payloads/session URLs.

## Reference documentation

- [Message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0)
- [Immutable Outlook identifiers](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- [Graph known issues](https://learn.microsoft.com/en-us/graph/known-issues)
- [Large Outlook attachments](https://learn.microsoft.com/en-us/graph/outlook-large-attachments)
- [Application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)
- [Celery Redis broker](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
