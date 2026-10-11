# Verification

Verified in this workspace on 2026-10-11 with Python 3.12.14.

- `pytest -q`: **39 passed, 1 skipped**.
- `ruff check .`: passed.
- Python compile checks: passed.
- Initial Alembic upgrade/downgrade against SQLite: passed.
- PostgreSQL model DDL compilation and offline Alembic SQL generation: passed.
- Compose YAML parsing and deployment structure checks: passed.
- Mocked S3 round trips/version handling through boto3: passed.
- Mocked Graph delta pagination, atomic page rollback, token reset, folder moves,
  partial updates, inline attachment discovery, and malformed-item quarantine: passed.
- Send idempotency, scope/ownership checks, draft recovery, uncertain submissions,
  throttling rejection, and explicit retry acknowledgment: passed.
- Small attachment response-loss recovery and large attachment chunk resume: passed.
- Queue-loss republishing, stale job recovery, completed-job deduplication, and
  unresolved-send scheduling: passed.

The skipped test requires a real PostgreSQL server and verifies the migration
and mailbox advisory lock in an isolated schema. Enable it with
`TEST_DATABASE_URL` or run the Docker Compose test service.

Docker is not installed in this workspace. Image build/container startup and
live MinIO integration were not executed. Microsoft credentials were not
provided; no live Graph request or real email send was performed.

One upstream Starlette warning notes that its TestClient's HTTPX integration is
deprecated; tests passed with the pinned versions. This does not affect the
production Graph HTTP client.
