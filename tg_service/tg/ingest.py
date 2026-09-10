"""Write Telethon messages/entities into the database."""
import logging

from telethon.tl import types as t

from .. import repo
from ..db import get_pool
from ..media.policy import auto_action_for
from .mapper import chat_row, message_row, user_row

log = logging.getLogger(__name__)


async def upsert_peer(conn, entity):
    if entity is None:
        return
    if isinstance(entity, t.User):
        await repo.upsert_user(conn, user_row(entity))
    elif isinstance(entity, (t.Chat, t.ChatForbidden, t.Channel, t.ChannelForbidden)):
        await repo.upsert_chat(conn, chat_row(entity))


async def ingest_message(m, *, chat=None, sender=None, bump_cursor=True, track_edit=False):
    """Store one message. chat/sender are Telethon entities if already resolved."""
    pool = await get_pool()
    row = message_row(m)
    async with pool.acquire() as conn:
        if chat is not None:
            await upsert_peer(conn, chat)
        await repo.ensure_chat_stub(conn, row["chat_id"])
        if sender is not None:
            await upsert_peer(conn, sender)
        if track_edit:
            old = await repo.get_message_text(conn, row["chat_id"], row["msg_id"])
            if old is not None and (old["text"] != row["text"] or old["media"] != row["media"]):
                await repo.record_edit(conn, row["chat_id"], row["msg_id"], old["text"], old["media"])
        is_new = await conn.fetchval(
            "SELECT 1 FROM messages WHERE chat_id = $1 AND msg_id = $2", row["chat_id"], row["msg_id"]
        ) is None
        await repo.upsert_message(conn, row)
        if bump_cursor:
            await repo.bump_chat_cursor(conn, row["chat_id"], row["msg_id"], row["date"])
        if is_new and row["media_type"]:
            await _auto_media(conn, row)
    return row


async def _auto_media(conn, row: dict):
    """Queue automatic download+transcribe/extract for a freshly stored message, per policy."""
    action = auto_action_for(row["media_type"], row["media"], await repo.chat_type(conn, row["chat_id"]))
    if action:
        await repo.enqueue_media_job(conn, row["chat_id"], row["msg_id"], "download", then=action, requested_by="auto")


async def ingest_batch(client, messages, *, bump_cursor=True):
    """Store a batch from iter_messages. Resolves senders through Telethon's entity cache."""
    if not messages:
        return 0
    pool = await get_pool()
    rows = []
    peers = {}
    for m in messages:
        rows.append(message_row(m))
        if m.sender_id is not None and m.sender_id not in peers:
            try:
                peers[m.sender_id] = await m.get_sender()
            except Exception as e:  # entity not cached, keep going
                log.debug("sender %s not resolved: %s", m.sender_id, e)
                peers[m.sender_id] = None
    async with pool.acquire() as conn:
        await repo.ensure_chat_stub(conn, rows[0]["chat_id"])
        for p in peers.values():
            await upsert_peer(conn, p)
        existing = {
            r["msg_id"] for r in await conn.fetch(
                "SELECT msg_id FROM messages WHERE chat_id = $1 AND msg_id = ANY($2::bigint[])",
                rows[0]["chat_id"], [r["msg_id"] for r in rows],
            )
        }
        await repo.upsert_messages(conn, rows)
        for r in rows:
            if r["media_type"] and r["msg_id"] not in existing:
                await _auto_media(conn, r)
        if bump_cursor:
            newest = max(rows, key=lambda r: r["msg_id"])
            oldest = min(rows, key=lambda r: r["msg_id"])
            await repo.bump_chat_cursor(conn, newest["chat_id"], newest["msg_id"], newest["date"])
            await repo.bump_chat_cursor(conn, oldest["chat_id"], oldest["msg_id"], oldest["date"])
    return len(rows)
