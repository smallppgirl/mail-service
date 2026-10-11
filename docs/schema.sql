BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE applications (
    id VARCHAR(36) NOT NULL, 
    name TEXT NOT NULL, 
    key_hash VARCHAR(64) NOT NULL, 
    scopes JSONB NOT NULL, 
    mailbox_ids JSONB NOT NULL, 
    enabled BOOLEAN NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (key_hash)
);

CREATE TABLE mailboxes (
    id VARCHAR(36) NOT NULL, 
    graph_user_id TEXT NOT NULL, 
    address TEXT NOT NULL, 
    enabled BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (graph_user_id)
);

CREATE TABLE blobs (
    id VARCHAR(36) NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    app_id VARCHAR(36), 
    bucket TEXT NOT NULL, 
    key TEXT NOT NULL, 
    version_id TEXT, 
    sha256 VARCHAR(64), 
    size INTEGER NOT NULL, 
    name TEXT NOT NULL, 
    content_type TEXT NOT NULL, 
    status VARCHAR(30) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(app_id) REFERENCES applications (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    UNIQUE (key)
);

CREATE INDEX ix_blobs_mailbox_id ON blobs (mailbox_id);

CREATE TABLE folders (
    id VARCHAR(36) NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    graph_id TEXT NOT NULL, 
    parent_graph_id TEXT, 
    name TEXT NOT NULL, 
    tracked BOOLEAN NOT NULL, 
    removed_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    UNIQUE (mailbox_id, graph_id)
);

CREATE INDEX ix_folders_mailbox_id ON folders (mailbox_id);

CREATE TABLE jobs (
    id VARCHAR(36) NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    kind VARCHAR(30) NOT NULL, 
    dedupe_key TEXT NOT NULL, 
    payload JSONB NOT NULL, 
    status VARCHAR(30) NOT NULL, 
    run_after TIMESTAMP WITH TIME ZONE NOT NULL, 
    lease_until TIMESTAMP WITH TIME ZONE, 
    attempts INTEGER NOT NULL, 
    last_published_at TIMESTAMP WITH TIME ZONE, 
    error JSONB, 
    PRIMARY KEY (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    UNIQUE (dedupe_key)
);

CREATE INDEX ix_jobs_mailbox_id ON jobs (mailbox_id);

CREATE INDEX ix_jobs_run_after ON jobs (run_after);

CREATE INDEX ix_jobs_status ON jobs (status);

CREATE TABLE messages (
    id VARCHAR(36) NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    graph_id TEXT NOT NULL, 
    internet_message_id TEXT, 
    conversation_id TEXT, 
    subject TEXT, 
    sender JSONB, 
    recipients JSONB, 
    received_at TIMESTAMP WITH TIME ZONE, 
    sent_at TIMESTAMP WITH TIME ZONE, 
    modified_at TIMESTAMP WITH TIME ZONE, 
    change_key TEXT, 
    is_read BOOLEAN, 
    is_draft BOOLEAN, 
    has_attachments BOOLEAN, 
    categories JSONB, 
    availability VARCHAR(30) NOT NULL, 
    current_folder_graph_id TEXT, 
    body_type VARCHAR(20), 
    body_content TEXT, 
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    last_observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    UNIQUE (mailbox_id, graph_id)
);

CREATE INDEX ix_messages_internet_message_id ON messages (internet_message_id);

CREATE INDEX ix_messages_mailbox_id ON messages (mailbox_id);

CREATE INDEX messages_received_idx ON messages (mailbox_id, received_at, id);

CREATE TABLE send_requests (
    id VARCHAR(36) NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    app_id VARCHAR(36) NOT NULL, 
    idempotency_key VARCHAR(200) NOT NULL, 
    payload_hash VARCHAR(64) NOT NULL, 
    payload JSONB NOT NULL, 
    status VARCHAR(30) NOT NULL, 
    draft_id TEXT, 
    not_before TIMESTAMP WITH TIME ZONE NOT NULL, 
    accepted_at TIMESTAMP WITH TIME ZONE, 
    sent_observed_at TIMESTAMP WITH TIME ZONE, 
    unknown_since TIMESTAMP WITH TIME ZONE, 
    error JSONB, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(app_id) REFERENCES applications (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    UNIQUE (app_id, mailbox_id, idempotency_key)
);

CREATE INDEX ix_send_requests_app_id ON send_requests (app_id);

CREATE INDEX ix_send_requests_mailbox_id ON send_requests (mailbox_id);

CREATE TABLE app_message_state (
    app_id VARCHAR(36) NOT NULL, 
    message_id VARCHAR(36) NOT NULL, 
    status TEXT NOT NULL, 
    data JSONB NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (app_id, message_id), 
    FOREIGN KEY(app_id) REFERENCES applications (id), 
    FOREIGN KEY(message_id) REFERENCES messages (id)
);

CREATE TABLE attachments (
    id VARCHAR(36) NOT NULL, 
    message_id VARCHAR(36) NOT NULL, 
    graph_id TEXT NOT NULL, 
    kind TEXT NOT NULL, 
    name TEXT NOT NULL, 
    content_type TEXT NOT NULL, 
    size INTEGER NOT NULL, 
    is_inline BOOLEAN NOT NULL, 
    content_id TEXT, 
    source JSONB NOT NULL, 
    removed_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(message_id) REFERENCES messages (id), 
    UNIQUE (message_id, graph_id)
);

CREATE INDEX ix_attachments_message_id ON attachments (message_id);

CREATE TABLE events (
    id SERIAL NOT NULL, 
    mailbox_id VARCHAR(36) NOT NULL, 
    message_id VARCHAR(36), 
    kind TEXT NOT NULL, 
    payload JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id), 
    FOREIGN KEY(message_id) REFERENCES messages (id)
);

CREATE INDEX events_mailbox_cursor_idx ON events (mailbox_id, id);

CREATE TABLE message_folder_state (
    message_id VARCHAR(36) NOT NULL, 
    folder_id VARCHAR(36) NOT NULL, 
    present BOOLEAN NOT NULL, 
    generation INTEGER NOT NULL, 
    PRIMARY KEY (message_id, folder_id), 
    FOREIGN KEY(folder_id) REFERENCES folders (id), 
    FOREIGN KEY(message_id) REFERENCES messages (id)
);

CREATE TABLE message_versions (
    id VARCHAR(36) NOT NULL, 
    message_id VARCHAR(36) NOT NULL, 
    content_hash VARCHAR(64) NOT NULL, 
    content JSONB NOT NULL, 
    captured_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(message_id) REFERENCES messages (id), 
    UNIQUE (message_id, content_hash)
);

CREATE INDEX ix_message_versions_message_id ON message_versions (message_id);

CREATE TABLE send_attachments (
    send_id VARCHAR(36) NOT NULL, 
    blob_id VARCHAR(36) NOT NULL, 
    graph_id TEXT, 
    upload_state JSONB, 
    status VARCHAR(30) NOT NULL, 
    PRIMARY KEY (send_id, blob_id), 
    FOREIGN KEY(blob_id) REFERENCES blobs (id), 
    FOREIGN KEY(send_id) REFERENCES send_requests (id)
);

CREATE TABLE send_attempts (
    id VARCHAR(36) NOT NULL, 
    send_id VARCHAR(36) NOT NULL, 
    phase TEXT NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    status_code INTEGER, 
    graph_request_id TEXT, 
    outcome TEXT NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(send_id) REFERENCES send_requests (id)
);

CREATE INDEX ix_send_attempts_send_id ON send_attempts (send_id);

CREATE TABLE sync_checkpoints (
    folder_id VARCHAR(36) NOT NULL, 
    delta_url TEXT, 
    next_url TEXT, 
    query_version INTEGER NOT NULL, 
    generation INTEGER NOT NULL, 
    baseline BOOLEAN NOT NULL, 
    status VARCHAR(30) NOT NULL, 
    last_success_at TIMESTAMP WITH TIME ZONE, 
    last_error JSONB, 
    PRIMARY KEY (folder_id), 
    FOREIGN KEY(folder_id) REFERENCES folders (id)
);

CREATE TABLE sync_issues (
    id VARCHAR(36) NOT NULL, 
    folder_id VARCHAR(36) NOT NULL, 
    item_hash VARCHAR(64) NOT NULL, 
    item JSONB NOT NULL, 
    error JSONB NOT NULL, 
    resolved BOOLEAN NOT NULL, 
    captured_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(folder_id) REFERENCES folders (id), 
    UNIQUE (folder_id, item_hash)
);

CREATE INDEX ix_sync_issues_folder_id ON sync_issues (folder_id);

CREATE TABLE attachment_versions (
    id VARCHAR(36) NOT NULL, 
    attachment_id VARCHAR(36) NOT NULL, 
    source_version TEXT NOT NULL, 
    blob_id VARCHAR(36), 
    status VARCHAR(30) NOT NULL, 
    error JSONB, 
    captured_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(attachment_id) REFERENCES attachments (id), 
    FOREIGN KEY(blob_id) REFERENCES blobs (id), 
    UNIQUE (attachment_id, source_version)
);

CREATE INDEX ix_attachment_versions_attachment_id ON attachment_versions (attachment_id);

CREATE INDEX messages_search_idx ON messages USING gin (to_tsvector('simple', coalesce(subject, '') || ' ' || coalesce(body_content, '')));

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

COMMIT;

