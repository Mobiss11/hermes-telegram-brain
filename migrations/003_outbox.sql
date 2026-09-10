-- Drafts created by agents. Nothing is sent until the owner replies "ok <id>" in Saved Messages.
CREATE TABLE outbox (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    reply_to_msg_id BIGINT,
    text            TEXT NOT NULL,
    reason          TEXT,                       -- why the agent wants to send it (shown in the confirmation prompt)
    requested_by    TEXT,
    idempotency_key TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | rejected | expired | failed
    expires_at      TIMESTAMPTZ NOT NULL,
    notice_msg_id   BIGINT,                     -- the prompt message in Saved Messages
    decided_at      TIMESTAMPTZ,
    sent_msg_id     BIGINT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX outbox_status ON outbox (status, id);
CREATE INDEX outbox_chat_recent ON outbox (chat_id, created_at DESC);

-- deny | confirm. Channels are always denied regardless of this value; 'auto' is intentionally not implemented.
ALTER TABLE chats ADD COLUMN send_policy TEXT NOT NULL DEFAULT 'confirm';
UPDATE chats SET send_policy = 'deny' WHERE type = 'channel';
