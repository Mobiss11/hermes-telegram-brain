"""All SQL lives here."""
from datetime import datetime

import asyncpg

MESSAGE_COLUMNS = [
    "chat_id", "msg_id", "sender_id", "post_author", "date", "edit_date", "text", "entities",
    "is_out", "mentioned", "is_post", "silent", "pinned", "via_bot_id",
    "reply_to_msg_id", "reply_to_top_id", "topic_id", "reply_to_chat_id", "quote_text",
    "fwd_from_id", "fwd_from_name", "fwd_date", "fwd_channel_post", "fwd_post_author",
    "fwd_saved_from_chat_id", "fwd_saved_from_msg_id",
    "media_type", "media", "grouped_id", "action_type", "action",
    "views", "forwards", "replies_count", "reactions", "ttl_period", "raw",
]
_UPDATABLE = [
    "sender_id", "post_author", "edit_date", "text", "entities", "pinned",
    "media_type", "media", "views", "forwards", "replies_count", "reactions", "raw",
]

_INSERT_MESSAGE = (
    f"INSERT INTO messages ({', '.join(MESSAGE_COLUMNS)}) VALUES "
    f"({', '.join(f'${i + 1}' for i in range(len(MESSAGE_COLUMNS)))}) "
    "ON CONFLICT (chat_id, msg_id) DO UPDATE SET "
    + ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, messages.{c})" if c in ("sender_id", "post_author") else f"{c} = EXCLUDED.{c}"
        for c in _UPDATABLE
    )
    + ", updated_at = now()"
)

# Columns returned to API consumers (raw is excluded unless asked for).
LIST_COLUMNS = [c for c in MESSAGE_COLUMNS if c != "raw"] + ["deleted_at", "inserted_at", "updated_at"]
_SELECT_MSG = (
    "SELECT " + ", ".join(f"m.{c}" for c in LIST_COLUMNS) + ", "
    "COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), c.title) AS sender_name, "
    "COALESCE(u.username, c.username) AS sender_username, "
    "u.is_bot AS sender_is_bot, "
    "mf.path AS file_path, "
    "(SELECT mc.text FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id AND mc.kind = 'transcript') AS transcript, "
    "(SELECT LEFT(mc.text, 3000) FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id AND mc.kind = 'document') AS document_text, "
    "(SELECT mc.text FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id AND mc.kind = 'image') AS image_text "
    "FROM messages m "
    "LEFT JOIN users u ON u.id = m.sender_id "
    "LEFT JOIN chats c ON c.id = m.sender_id "
    "LEFT JOIN media_files mf ON mf.chat_id = m.chat_id AND mf.msg_id = m.msg_id "
)


# ---------- chats / users ----------

async def upsert_chat(conn: asyncpg.Connection, row: dict):
    await conn.execute(
        """
        INSERT INTO chats (id, type, title, username, first_name, last_name, is_forum, participants_count, raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (id) DO UPDATE SET
            type = CASE WHEN EXCLUDED.type = 'unknown' THEN chats.type ELSE EXCLUDED.type END,
            title = COALESCE(EXCLUDED.title, chats.title),
            username = COALESCE(EXCLUDED.username, chats.username),
            first_name = COALESCE(EXCLUDED.first_name, chats.first_name),
            last_name = COALESCE(EXCLUDED.last_name, chats.last_name),
            is_forum = EXCLUDED.is_forum OR chats.is_forum,
            participants_count = COALESCE(EXCLUDED.participants_count, chats.participants_count),
            raw = COALESCE(EXCLUDED.raw, chats.raw),
            updated_at = now()
        """,
        row["id"], row["type"], row["title"], row["username"], row["first_name"], row["last_name"],
        row["is_forum"], row["participants_count"], row["raw"],
    )


async def ensure_chat_stub(conn: asyncpg.Connection, chat_id: int):
    await conn.execute(
        "INSERT INTO chats (id, type) VALUES ($1, 'unknown') ON CONFLICT (id) DO NOTHING", chat_id
    )


async def upsert_user(conn: asyncpg.Connection, row: dict):
    await conn.execute(
        """
        INSERT INTO users (id, username, first_name, last_name, phone, is_bot, is_self, is_contact, is_premium, is_deleted, raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = COALESCE(EXCLUDED.first_name, users.first_name),
            last_name = EXCLUDED.last_name,
            phone = COALESCE(EXCLUDED.phone, users.phone),
            is_bot = EXCLUDED.is_bot, is_self = EXCLUDED.is_self, is_contact = EXCLUDED.is_contact,
            is_premium = EXCLUDED.is_premium, is_deleted = EXCLUDED.is_deleted,
            raw = EXCLUDED.raw, updated_at = now()
        """,
        row["id"], row["username"], row["first_name"], row["last_name"], row["phone"],
        row["is_bot"], row["is_self"], row["is_contact"], row["is_premium"], row["is_deleted"], row["raw"],
    )


async def bump_chat_cursor(conn: asyncpg.Connection, chat_id: int, msg_id: int, date: datetime):
    await conn.execute(
        """
        UPDATE chats SET
            last_msg_id = GREATEST(COALESCE(last_msg_id, 0), $2),
            first_synced_msg_id = LEAST(COALESCE(first_synced_msg_id, $2), $2),
            last_message_at = GREATEST(COALESCE(last_message_at, 'epoch'::timestamptz), $3),
            updated_at = now()
        WHERE id = $1
        """,
        chat_id, msg_id, date,
    )


async def list_chats(conn, *, q: str | None, type_: str | None, tracked: bool | None, limit: int, offset: int):
    return await conn.fetch(
        """
        SELECT c.*,
               (SELECT count(*) FROM messages m WHERE m.chat_id = c.id) AS message_count
        FROM chats c
        WHERE ($1::text IS NULL OR c.title ILIKE '%' || $1 || '%' OR lower(c.username) = lower($1))
          AND ($2::text IS NULL OR c.type = $2)
          AND ($3::boolean IS NULL OR c.is_tracked = $3)
        ORDER BY c.last_message_at DESC NULLS LAST
        LIMIT $4 OFFSET $5
        """,
        q, type_, tracked, limit, offset,
    )


async def get_chat(conn, chat_id: int):
    return await conn.fetchrow(
        """
        SELECT c.*,
               s.message_count, s.oldest_date, s.newest_date
        FROM chats c
        LEFT JOIN LATERAL (
            SELECT count(*) AS message_count, min(date) AS oldest_date, max(date) AS newest_date
            FROM messages m WHERE m.chat_id = c.id
        ) s ON true
        WHERE c.id = $1
        """,
        chat_id,
    )


async def set_tracked(conn, chat_id: int, tracked: bool):
    return await conn.fetchrow(
        "UPDATE chats SET is_tracked = $2, updated_at = now() WHERE id = $1 RETURNING *", chat_id, tracked
    )


async def is_tracked(conn, chat_id: int) -> bool:
    v = await conn.fetchval("SELECT is_tracked FROM chats WHERE id = $1", chat_id)
    return True if v is None else v


async def get_user(conn, user_id: int):
    return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)


# ---------- messages ----------

async def upsert_message(conn: asyncpg.Connection, row: dict):
    await conn.execute(_INSERT_MESSAGE, *[row[c] for c in MESSAGE_COLUMNS])


async def upsert_messages(conn: asyncpg.Connection, rows: list[dict]):
    if not rows:
        return
    await conn.executemany(_INSERT_MESSAGE, [[r[c] for c in MESSAGE_COLUMNS] for r in rows])


async def get_message_text(conn, chat_id: int, msg_id: int):
    return await conn.fetchrow(
        "SELECT text, media, edit_date, date FROM messages WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id
    )


async def record_edit(conn, chat_id: int, msg_id: int, old_text, old_media):
    await conn.execute(
        "INSERT INTO message_edits (chat_id, msg_id, old_text, old_media) VALUES ($1, $2, $3, $4)",
        chat_id, msg_id, old_text, old_media,
    )


async def mark_deleted(conn, msg_ids: list[int], chat_id: int | None) -> int:
    if chat_id is not None:
        res = await conn.execute(
            "UPDATE messages SET deleted_at = now(), updated_at = now() "
            "WHERE chat_id = $1 AND msg_id = ANY($2::bigint[]) AND deleted_at IS NULL",
            chat_id, msg_ids,
        )
    else:
        # Private chats and basic groups share one message-id sequence per account,
        # so a bare msg_id identifies at most one message among them.
        res = await conn.execute(
            "UPDATE messages SET deleted_at = now(), updated_at = now() "
            "WHERE msg_id = ANY($1::bigint[]) AND deleted_at IS NULL "
            "AND chat_id IN (SELECT id FROM chats WHERE type IN ('user', 'chat'))",
            msg_ids,
        )
    return int(res.split()[-1])


async def list_messages(
    conn, chat_id: int, *, from_date=None, to_date=None, before_id=None, after_id=None,
    sender_id=None, topic_id=None, include_deleted=False, order="desc", limit=100,
):
    direction = "ASC" if order == "asc" else "DESC"
    return await conn.fetch(
        _SELECT_MSG
        + """
        WHERE m.chat_id = $1
          AND ($2::timestamptz IS NULL OR m.date >= $2)
          AND ($3::timestamptz IS NULL OR m.date <= $3)
          AND ($4::bigint IS NULL OR m.msg_id < $4)
          AND ($5::bigint IS NULL OR m.msg_id > $5)
          AND ($6::bigint IS NULL OR m.sender_id = $6)
          AND ($7::bigint IS NULL OR m.topic_id = $7)
          AND ($8::boolean OR m.deleted_at IS NULL)
        ORDER BY m.msg_id """ + direction + " LIMIT $9",
        chat_id, from_date, to_date, before_id, after_id, sender_id, topic_id, include_deleted, limit,
    )


async def get_message(conn, chat_id: int, msg_id: int):
    return await conn.fetchrow(_SELECT_MSG + "WHERE m.chat_id = $1 AND m.msg_id = $2", chat_id, msg_id)


async def get_raw(conn, chat_id: int, msg_id: int):
    return await conn.fetchval("SELECT raw FROM messages WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id)


async def get_edits(conn, chat_id: int, msg_id: int):
    return await conn.fetch(
        "SELECT replaced_at, old_text, old_media FROM message_edits WHERE chat_id = $1 AND msg_id = $2 ORDER BY id",
        chat_id, msg_id,
    )


async def get_album(conn, chat_id: int, grouped_id: int):
    return await conn.fetch(
        _SELECT_MSG + "WHERE m.chat_id = $1 AND m.grouped_id = $2 ORDER BY m.msg_id", chat_id, grouped_id
    )


async def get_replies(conn, chat_id: int, msg_id: int, limit: int = 50):
    return await conn.fetch(
        _SELECT_MSG + "WHERE m.chat_id = $1 AND m.reply_to_msg_id = $2 ORDER BY m.msg_id LIMIT $3",
        chat_id, msg_id, limit,
    )


async def search_messages(
    conn, q: str, *, chat_id=None, from_date=None, to_date=None, sender_id=None,
    mode="fts", limit=50, offset=0,
):
    if mode == "substring":
        where_q = (
            "(m.text ILIKE '%' || $1 || '%' OR (m.chat_id, m.msg_id) IN "
            "(SELECT chat_id, msg_id FROM message_content WHERE text ILIKE '%' || $1 || '%'))"
        )
    else:
        tsq = "(websearch_to_tsquery('russian', $1) || websearch_to_tsquery('english', $1))"
        where_q = (
            f"(m.tsv @@ {tsq} OR (m.chat_id, m.msg_id) IN "
            f"(SELECT chat_id, msg_id FROM message_content WHERE tsv @@ {tsq}))"
        )
    return await conn.fetch(
        _SELECT_MSG
        + f"""
        WHERE {where_q}
          AND ($2::bigint IS NULL OR m.chat_id = $2)
          AND ($3::timestamptz IS NULL OR m.date >= $3)
          AND ($4::timestamptz IS NULL OR m.date <= $4)
          AND ($5::bigint IS NULL OR m.sender_id = $5)
          AND m.deleted_at IS NULL
        ORDER BY m.date DESC
        LIMIT $6 OFFSET $7
        """,
        q, chat_id, from_date, to_date, sender_id, limit, offset,
    )


# ---------- sync jobs ----------

async def create_job(conn, **kw):
    return await conn.fetchrow(
        """
        INSERT INTO sync_jobs (chat_id, from_date, to_date, min_id, max_id, max_messages, requested_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *
        """,
        kw["chat_id"], kw.get("from_date"), kw.get("to_date"), kw.get("min_id"), kw.get("max_id"),
        kw.get("max_messages"), kw.get("requested_by"),
    )


async def get_job(conn, job_id: int):
    return await conn.fetchrow("SELECT * FROM sync_jobs WHERE id = $1", job_id)


async def list_jobs(conn, status: str | None, limit: int = 50):
    return await conn.fetch(
        "SELECT * FROM sync_jobs WHERE ($1::text IS NULL OR status = $1) ORDER BY id DESC LIMIT $2", status, limit
    )


async def cancel_job(conn, job_id: int):
    return await conn.fetchrow(
        "UPDATE sync_jobs SET status = 'cancelled', finished_at = now() "
        "WHERE id = $1 AND status IN ('queued', 'running') RETURNING *",
        job_id,
    )


async def claim_next_job(conn):
    return await conn.fetchrow(
        """
        UPDATE sync_jobs SET status = 'running', started_at = now()
        WHERE id = (SELECT id FROM sync_jobs WHERE status = 'queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING *
        """
    )


async def job_progress(conn, job_id: int, processed: int, last_msg_id: int | None):
    await conn.execute(
        "UPDATE sync_jobs SET processed = $2, last_msg_id = $3 WHERE id = $1", job_id, processed, last_msg_id
    )


async def job_status(conn, job_id: int) -> str | None:
    return await conn.fetchval("SELECT status FROM sync_jobs WHERE id = $1", job_id)


async def finish_job(conn, job_id: int, status: str, error: str | None = None):
    await conn.execute(
        "UPDATE sync_jobs SET status = $2, error = $3, finished_at = now() WHERE id = $1 AND status = 'running'",
        job_id, status, error,
    )


async def reset_running_jobs(conn):
    """Jobs left 'running' by a crashed process go back to the queue."""
    await conn.execute("UPDATE sync_jobs SET status = 'queued', started_at = NULL WHERE status = 'running'")


# ---------- media ----------

MEDIA_ACTIONS = ("download", "transcribe", "extract")


async def enqueue_media_job(conn, chat_id: int, msg_id: int, action: str, then: str | None = None,
                            requested_by: str | None = None):
    """Idempotent: an active (queued/running) job for the same message+action is reused."""
    row = await conn.fetchrow(
        """
        INSERT INTO media_jobs (chat_id, msg_id, action, "then", requested_by) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (chat_id, msg_id, action) WHERE status IN ('queued', 'running') DO UPDATE
            SET "then" = COALESCE(media_jobs."then", EXCLUDED."then")
        RETURNING *
        """,
        chat_id, msg_id, action, then, requested_by,
    )
    return row


async def claim_media_job(conn, actions: tuple[str, ...]):
    return await conn.fetchrow(
        """
        UPDATE media_jobs SET status = 'running', started_at = now(), attempts = attempts + 1
        WHERE id = (SELECT id FROM media_jobs WHERE status = 'queued' AND action = ANY($1::text[])
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING *
        """,
        list(actions),
    )


async def finish_media_job(conn, job_id: int, status: str, error: str | None = None):
    await conn.execute(
        "UPDATE media_jobs SET status = $2, error = $3, finished_at = now() WHERE id = $1", job_id, status, error
    )


async def reset_running_media_jobs(conn, actions: tuple[str, ...]):
    await conn.execute(
        "UPDATE media_jobs SET status = 'queued', started_at = NULL WHERE status = 'running' AND action = ANY($1::text[])",
        list(actions),
    )


async def get_media_job(conn, job_id: int):
    return await conn.fetchrow("SELECT * FROM media_jobs WHERE id = $1", job_id)


async def list_media_jobs(conn, status: str | None, limit: int = 50):
    return await conn.fetch(
        "SELECT * FROM media_jobs WHERE ($1::text IS NULL OR status = $1) ORDER BY id DESC LIMIT $2", status, limit
    )


async def media_jobs_for_message(conn, chat_id: int, msg_id: int):
    return await conn.fetch(
        "SELECT id, action, \"then\", status, error, created_at, finished_at FROM media_jobs "
        "WHERE chat_id = $1 AND msg_id = $2 ORDER BY id DESC LIMIT 10",
        chat_id, msg_id,
    )


async def get_media_file(conn, chat_id: int, msg_id: int):
    return await conn.fetchrow("SELECT * FROM media_files WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id)


async def upsert_media_file(conn, chat_id: int, msg_id: int, path: str, mime: str | None, size: int | None, sha256: str | None):
    await conn.execute(
        """
        INSERT INTO media_files (chat_id, msg_id, path, mime, size, sha256) VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (chat_id, msg_id) DO UPDATE SET path = EXCLUDED.path, mime = EXCLUDED.mime,
            size = EXCLUDED.size, sha256 = EXCLUDED.sha256, downloaded_at = now()
        """,
        chat_id, msg_id, path, mime, size, sha256,
    )


async def get_content(conn, chat_id: int, msg_id: int):
    return await conn.fetch(
        "SELECT kind, text, language, model, duration, meta, created_at FROM message_content "
        "WHERE chat_id = $1 AND msg_id = $2 ORDER BY kind",
        chat_id, msg_id,
    )


async def upsert_content(conn, chat_id: int, msg_id: int, kind: str, text: str, *, language=None, model=None,
                         duration=None, meta=None):
    await conn.execute(
        """
        INSERT INTO message_content (chat_id, msg_id, kind, text, language, model, duration, meta)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (chat_id, msg_id, kind) DO UPDATE SET text = EXCLUDED.text, language = EXCLUDED.language,
            model = EXCLUDED.model, duration = EXCLUDED.duration, meta = EXCLUDED.meta, created_at = now()
        """,
        chat_id, msg_id, kind, text, language, model, duration, meta,
    )


async def media_candidates(conn, *, since, kinds: list[str], non_channel_only: bool = True, max_mb: int | None = None,
                           limit: int = 5000):
    """Messages with media that have no derived content yet (for backlog enqueueing)."""
    return await conn.fetch(
        """
        SELECT m.chat_id, m.msg_id, m.media_type, m.media
        FROM messages m JOIN chats c ON c.id = m.chat_id
        WHERE m.media_type = ANY($1::text[]) AND m.date >= $2 AND m.deleted_at IS NULL
          AND (NOT $3::boolean OR c.type <> 'channel')
          AND ($4::bigint IS NULL OR COALESCE((m.media->>'size')::bigint, 0) <= $4)
          AND NOT EXISTS (SELECT 1 FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id)
          AND NOT EXISTS (SELECT 1 FROM media_jobs j WHERE j.chat_id = m.chat_id AND j.msg_id = m.msg_id
                          AND j.status IN ('queued', 'running', 'skipped'))
        ORDER BY m.date DESC LIMIT $5
        """,
        kinds, since, non_channel_only, (max_mb * 1024 * 1024) if max_mb else None, limit,
    )


async def chat_type(conn, chat_id: int) -> str | None:
    return await conn.fetchval("SELECT type FROM chats WHERE id = $1", chat_id)


async def expired_media_files(conn, before):
    return await conn.fetch("SELECT chat_id, msg_id, path, size FROM media_files WHERE downloaded_at < $1", before)


async def delete_media_file(conn, chat_id: int, msg_id: int):
    await conn.execute("DELETE FROM media_files WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id)


# ---------- outbox ----------

async def create_draft(conn, *, chat_id, reply_to_msg_id, text, reason, requested_by, idempotency_key, ttl_minutes,
                       attachments=None, schedule_at=None, action="send", target_msg_id=None, original_text=None):
    if idempotency_key:
        existing = await conn.fetchrow("SELECT * FROM outbox WHERE idempotency_key = $1", idempotency_key)
        if existing:
            return existing, False
    row = await conn.fetchrow(
        """
        INSERT INTO outbox (chat_id, reply_to_msg_id, text, reason, requested_by, idempotency_key, expires_at,
                            attachments, schedule_at, action, target_msg_id, original_text)
        VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(mins => $7), $8, $9, $10, $11, $12) RETURNING *
        """,
        chat_id, reply_to_msg_id, text, reason, requested_by, idempotency_key, ttl_minutes, attachments, schedule_at,
        action, target_msg_id, original_text,
    )
    return row, True


async def update_draft_text(conn, draft_id: int, text: str):
    return await conn.fetchrow(
        "UPDATE outbox SET text = $2 WHERE id = $1 AND status = 'pending' RETURNING *", draft_id, text
    )


async def set_draft_notice_user(conn, draft_id: int, user_msg_id: int):
    await conn.execute("UPDATE outbox SET notice_user_msg_id = $2 WHERE id = $1", draft_id, user_msg_id)


async def draft_by_notice_user(conn, user_msg_id: int):
    return await conn.fetchrow(
        "SELECT * FROM outbox WHERE notice_user_msg_id = $1 ORDER BY id DESC LIMIT 1", user_msg_id
    )


async def recent_context(conn, chat_id: int, limit: int = 3):
    return await conn.fetch(
        _SELECT_MSG + "WHERE m.chat_id = $1 AND m.deleted_at IS NULL AND m.action_type IS NULL "
        "ORDER BY m.msg_id DESC LIMIT $2", chat_id, limit,
    )


# ---------- embeddings ----------

async def messages_to_embed(conn, *, include_channels: bool, limit: int = 256):
    return await conn.fetch(
        """
        SELECT m.chat_id, m.msg_id, m.text,
               COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), cs.title) AS sender_name,
               (SELECT string_agg(mc.text, ' ') FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id) AS derived
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        LEFT JOIN users u ON u.id = m.sender_id
        LEFT JOIN chats cs ON cs.id = m.sender_id
        LEFT JOIN message_embeddings e ON e.chat_id = m.chat_id AND e.msg_id = m.msg_id
        WHERE e.chat_id IS NULL AND m.deleted_at IS NULL AND m.action_type IS NULL
          AND ($1::boolean OR c.type <> 'channel')
          AND (length(m.text) >= 15 OR EXISTS (SELECT 1 FROM message_content mc WHERE mc.chat_id = m.chat_id AND mc.msg_id = m.msg_id))
        ORDER BY m.date DESC LIMIT $2
        """,
        include_channels, limit,
    )


async def upsert_embeddings(conn, rows: list[tuple]):
    """rows: (chat_id, msg_id, model, vector_as_str)"""
    await conn.executemany(
        "INSERT INTO message_embeddings (chat_id, msg_id, model, embedding) VALUES ($1, $2, $3, $4::vector) "
        "ON CONFLICT (chat_id, msg_id) DO UPDATE SET model = EXCLUDED.model, embedding = EXCLUDED.embedding, created_at = now()",
        rows,
    )


async def drop_embedding(conn, chat_id: int, msg_id: int):
    await conn.execute("DELETE FROM message_embeddings WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id)


async def semantic_search(conn, vector: str, *, chat_id=None, from_date=None, to_date=None, limit=20):
    return await conn.fetch(
        _SELECT_MSG
        + """
        JOIN message_embeddings e ON e.chat_id = m.chat_id AND e.msg_id = m.msg_id
        WHERE ($2::bigint IS NULL OR m.chat_id = $2)
          AND ($3::timestamptz IS NULL OR m.date >= $3)
          AND ($4::timestamptz IS NULL OR m.date <= $4)
          AND m.deleted_at IS NULL
        ORDER BY e.embedding <=> $1::vector
        LIMIT $5
        """,
        vector, chat_id, from_date, to_date, limit,
    )


async def embedding_stats(conn):
    return await conn.fetchrow(
        "SELECT (SELECT count(*) FROM message_embeddings) AS embedded, "
        "(SELECT count(*) FROM messages WHERE deleted_at IS NULL AND length(text) >= 15) AS eligible"
    )


# ---------- health ----------

async def health_get(conn):
    return {r["key"]: r for r in await conn.fetch("SELECT * FROM health_state")}


async def health_set(conn, key: str, ok: bool, detail: str | None):
    await conn.execute(
        """
        INSERT INTO health_state (key, ok, detail) VALUES ($1, $2, $3)
        ON CONFLICT (key) DO UPDATE SET ok = EXCLUDED.ok, detail = EXCLUDED.detail,
            changed_at = CASE WHEN health_state.ok = EXCLUDED.ok THEN health_state.changed_at ELSE now() END
        """,
        key, ok, detail,
    )



async def get_draft(conn, draft_id: int):
    return await conn.fetchrow("SELECT * FROM outbox WHERE id = $1", draft_id)


async def list_drafts(conn, status: str | None, limit: int = 50):
    return await conn.fetch(
        "SELECT * FROM outbox WHERE ($1::text IS NULL OR status = $1) ORDER BY id DESC LIMIT $2", status, limit
    )


async def set_draft_notice(conn, draft_id: int, notice_msg_id: int):
    await conn.execute("UPDATE outbox SET notice_msg_id = $2 WHERE id = $1", draft_id, notice_msg_id)


async def decide_draft(conn, draft_id: int, status: str, *, sent_msg_id=None, error=None):
    """Atomic transition from pending; returns the row or None if it was not pending anymore."""
    return await conn.fetchrow(
        "UPDATE outbox SET status = $2, sent_msg_id = $3, error = $4, decided_at = now() "
        "WHERE id = $1 AND status = 'pending' RETURNING *",
        draft_id, status, sent_msg_id, error,
    )


async def expire_drafts(conn):
    return await conn.fetch(
        "UPDATE outbox SET status = 'expired', decided_at = now() "
        "WHERE status = 'pending' AND expires_at < now() RETURNING *"
    )


async def reject_all_pending(conn, error: str):
    return await conn.fetch(
        "UPDATE outbox SET status = 'rejected', error = $1, decided_at = now() WHERE status = 'pending' RETURNING *",
        error,
    )


async def outbox_rate(conn, chat_id: int) -> tuple[int, int]:
    row = await conn.fetchrow(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE chat_id = $1) AS per_chat
        FROM outbox WHERE created_at > now() - interval '1 hour'
        """,
        chat_id,
    )
    return row["total"], row["per_chat"]


async def set_send_policy(conn, chat_id: int, policy: str):
    return await conn.fetchrow(
        "UPDATE chats SET send_policy = $2, updated_at = now() WHERE id = $1 RETURNING *", chat_id, policy
    )
