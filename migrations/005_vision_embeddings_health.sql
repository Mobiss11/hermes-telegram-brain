-- outbox: besides sending, agents may propose editing / deleting the owner's own messages
ALTER TABLE outbox ADD COLUMN action TEXT NOT NULL DEFAULT 'send';   -- send | edit | delete
ALTER TABLE outbox ADD COLUMN target_msg_id BIGINT;                   -- message being edited / deleted
ALTER TABLE outbox ADD COLUMN original_text TEXT;                     -- its text at draft time (shown on the card)
ALTER TABLE outbox ADD COLUMN notice_user_msg_id BIGINT;              -- the card's id as the owner's account sees it (basic groups number messages per user)

-- semantic search (local model, see EMBED_MODEL)
CREATE TABLE message_embeddings (
    chat_id    BIGINT NOT NULL,
    msg_id     BIGINT NOT NULL,
    model      TEXT NOT NULL,
    embedding  vector(384) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX message_embeddings_hnsw ON message_embeddings USING hnsw (embedding vector_cosine_ops);

-- health monitor state (alerts on transitions only)
CREATE TABLE health_state (
    key        TEXT PRIMARY KEY,
    ok         BOOLEAN NOT NULL,
    detail     TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
