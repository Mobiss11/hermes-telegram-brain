CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- Chats: users (DMs), basic groups, supergroups, channels.
-- id is Telethon's "marked" peer id (users positive, groups negative, channels -100...).
CREATE TABLE chats (
    id                  BIGINT PRIMARY KEY,
    type                TEXT NOT NULL,               -- user | chat | supergroup | channel | unknown
    title               TEXT,
    username            TEXT,
    first_name          TEXT,
    last_name           TEXT,
    is_forum            BOOLEAN NOT NULL DEFAULT FALSE,
    participants_count  INTEGER,
    is_tracked          BOOLEAN NOT NULL DEFAULT TRUE,   -- false = listener ignores this chat
    history_seeded      BOOLEAN NOT NULL DEFAULT FALSE,  -- INITIAL_HISTORY already pulled
    last_msg_id         BIGINT,                          -- newest contiguous message we have
    first_synced_msg_id BIGINT,                          -- oldest message we have
    last_message_at     TIMESTAMPTZ,
    raw                 JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX chats_title_trgm ON chats USING gin (title gin_trgm_ops);
CREATE INDEX chats_username ON chats (lower(username));

CREATE TABLE users (
    id          BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    phone       TEXT,
    is_bot      BOOLEAN NOT NULL DEFAULT FALSE,
    is_self     BOOLEAN NOT NULL DEFAULT FALSE,
    is_contact  BOOLEAN NOT NULL DEFAULT FALSE,
    is_premium  BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted  BOOLEAN NOT NULL DEFAULT FALSE,
    raw         JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX users_username ON users (lower(username));

CREATE TABLE messages (
    chat_id             BIGINT NOT NULL,
    msg_id              BIGINT NOT NULL,
    sender_id           BIGINT,                 -- user id, or channel id when posting as channel; NULL for anonymous posts
    post_author         TEXT,                   -- channel signature
    date                TIMESTAMPTZ NOT NULL,
    edit_date           TIMESTAMPTZ,
    text                TEXT,
    entities            JSONB,                  -- formatting/links/mentions (Telethon MessageEntity*)
    is_out              BOOLEAN NOT NULL DEFAULT FALSE,   -- sent by me
    mentioned           BOOLEAN NOT NULL DEFAULT FALSE,   -- I am mentioned / replied to
    is_post             BOOLEAN NOT NULL DEFAULT FALSE,   -- channel post
    silent              BOOLEAN NOT NULL DEFAULT FALSE,
    pinned              BOOLEAN NOT NULL DEFAULT FALSE,
    via_bot_id          BIGINT,

    -- replies / threads / forum topics
    reply_to_msg_id     BIGINT,                 -- direct parent
    reply_to_top_id     BIGINT,                 -- thread root (comments / forum topic)
    topic_id            BIGINT,                 -- forum topic id (resolved), NULL outside forums
    reply_to_chat_id    BIGINT,                 -- reply that points into another chat (channel comments)
    quote_text          TEXT,                   -- quoted fragment when replying to a part of a message

    -- forwards
    fwd_from_id         BIGINT,                 -- original sender/channel peer id (if not hidden)
    fwd_from_name       TEXT,                   -- name when original sender is hidden
    fwd_date            TIMESTAMPTZ,
    fwd_channel_post    BIGINT,                 -- original msg id in the source channel
    fwd_post_author     TEXT,
    fwd_saved_from_chat_id BIGINT,              -- "saved messages" / forwarded-from-forward provenance
    fwd_saved_from_msg_id  BIGINT,

    -- media
    media_type          TEXT,                   -- photo | video | voice | video_note | audio | sticker | gif | document | poll | webpage | contact | geo | venue | dice | game | invoice | ...
    media               JSONB,                  -- name, mime, size, duration, w/h, poll answers, url/title for webpage, ...
    grouped_id          BIGINT,                 -- album id

    -- service messages (user joined, pinned, title changed, call, ...)
    action_type         TEXT,
    action              JSONB,

    -- stats
    views               INTEGER,
    forwards            INTEGER,
    replies_count       INTEGER,
    reactions           JSONB,                  -- [{"emoji": "👍", "count": 3, "mine": false}]
    ttl_period          INTEGER,

    deleted_at          TIMESTAMPTZ,
    raw                 JSONB,                  -- full Telethon object (bytes stripped)
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(text, '')) || to_tsvector('english', coalesce(text, ''))
    ) STORED,

    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX messages_chat_date   ON messages (chat_id, date DESC);
CREATE INDEX messages_date        ON messages (date DESC);
CREATE INDEX messages_sender      ON messages (sender_id, date DESC);
CREATE INDEX messages_reply       ON messages (chat_id, reply_to_msg_id) WHERE reply_to_msg_id IS NOT NULL;
CREATE INDEX messages_topic       ON messages (chat_id, topic_id, date DESC) WHERE topic_id IS NOT NULL;
CREATE INDEX messages_grouped     ON messages (chat_id, grouped_id) WHERE grouped_id IS NOT NULL;
CREATE INDEX messages_tsv         ON messages USING gin (tsv);

-- Previous text versions, written when an edit changes the text.
CREATE TABLE message_edits (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    msg_id      BIGINT NOT NULL,
    replaced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    old_text    TEXT,
    old_media   JSONB
);
CREATE INDEX message_edits_msg ON message_edits (chat_id, msg_id);

-- Backfill jobs picked up by the worker that owns the Telethon client.
CREATE TABLE sync_jobs (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    from_date    TIMESTAMPTZ,       -- stop when messages get older than this
    to_date      TIMESTAMPTZ,       -- start from messages older than this
    min_id       BIGINT,            -- alternative bounds by message id
    max_id       BIGINT,
    max_messages INTEGER,
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
    processed    INTEGER NOT NULL DEFAULT 0,
    last_msg_id  BIGINT,
    error        TEXT,
    requested_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE INDEX sync_jobs_status ON sync_jobs (status, id);
