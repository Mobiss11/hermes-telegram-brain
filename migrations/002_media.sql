-- Downloaded files on local disk.
CREATE TABLE media_files (
    chat_id       BIGINT NOT NULL,
    msg_id        BIGINT NOT NULL,
    path          TEXT NOT NULL,
    mime          TEXT,
    size          BIGINT,
    sha256        TEXT,
    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, msg_id)
);

-- Text derived from media: Whisper transcripts, document text.
CREATE TABLE message_content (
    chat_id    BIGINT NOT NULL,
    msg_id     BIGINT NOT NULL,
    kind       TEXT NOT NULL,                -- transcript | document
    text       TEXT,
    language   TEXT,
    model      TEXT,
    duration   REAL,
    meta       JSONB,                        -- pages, sheets, segments count, truncated flag ...
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(text, '')) || to_tsvector('english', coalesce(text, ''))
    ) STORED,
    PRIMARY KEY (chat_id, msg_id, kind)
);
CREATE INDEX message_content_tsv ON message_content USING gin (tsv);

-- Queue. `download` runs in the main process (needs Telethon), `transcribe` / `extract` in tg-media-worker.
-- `then` chains a follow-up action after a successful download.
CREATE TABLE media_jobs (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    msg_id       BIGINT NOT NULL,
    action       TEXT NOT NULL,               -- download | transcribe | extract
    "then"       TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    requested_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX media_jobs_active ON media_jobs (chat_id, msg_id, action) WHERE status IN ('queued', 'running');
CREATE INDEX media_jobs_status ON media_jobs (status, action, id);
