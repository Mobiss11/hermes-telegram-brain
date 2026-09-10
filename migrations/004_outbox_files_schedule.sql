ALTER TABLE outbox ADD COLUMN attachments JSONB;      -- [{"kind": "file", "path": ..., "name": ..., "size": ...} | {"kind": "tg", "chat_id": ..., "msg_id": ..., "name": ..., "media_type": ...}]
ALTER TABLE outbox ADD COLUMN schedule_at TIMESTAMPTZ; -- when set, the message is sent via Telegram's scheduled messages
-- status gains 'scheduled' (confirmed and queued in Telegram for schedule_at)
