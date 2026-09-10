"""Startup work: import all dialogs, seed recent history, close gaps since last run."""
import asyncio
import logging

from telethon import TelegramClient

from .. import repo
from ..config import settings
from ..db import get_pool
from .ingest import ingest_batch, ingest_message
from .mapper import chat_row

log = logging.getLogger(__name__)


async def sync_dialogs(client: TelegramClient) -> int:
    """Upsert every dialog and its top message (without moving the cursor)."""
    pool = await get_pool()
    n = 0
    async for d in client.iter_dialogs():
        if d.entity is None:
            continue
        async with pool.acquire() as conn:
            await repo.upsert_chat(conn, chat_row(d.entity))
        if d.message is not None:
            try:
                sender = await d.message.get_sender()
            except Exception:
                sender = None
            await ingest_message(d.message, sender=sender, bump_cursor=False)
        n += 1
    log.info("dialogs synced: %d", n)
    return n


async def _pull(client, chat_id: int, **kw) -> tuple[int, int | None]:
    """Iterate messages and store them in batches. Returns (count, last msg id seen)."""
    batch, count, last_id = [], 0, None
    async for m in client.iter_messages(chat_id, **kw):
        batch.append(m)
        last_id = m.id
        if len(batch) >= settings.backfill_batch:
            count += await ingest_batch(client, batch)
            batch = []
    count += await ingest_batch(client, batch)
    return count, last_id


async def catch_up(client: TelegramClient):
    """For each tracked chat: seed INITIAL_HISTORY on first sight, otherwise fetch everything after last_msg_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        chats = await conn.fetch(
            "SELECT id, type, title, history_seeded, last_msg_id FROM chats "
            "WHERE is_tracked AND type <> 'unknown' ORDER BY last_message_at DESC NULLS LAST"
        )
    log.info("catch-up over %d chats", len(chats))
    for c in chats:
        cid = c["id"]
        try:
            if not c["history_seeded"]:
                if settings.initial_history > 0:
                    n, _ = await _pull(client, cid, limit=settings.initial_history)
                    log.info("seeded %s (%s): %d messages", c["title"], cid, n)
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE chats SET history_seeded = TRUE WHERE id = $1", cid)
            elif c["last_msg_id"]:
                n, last_id = await _pull(
                    client, cid, min_id=c["last_msg_id"], reverse=True, limit=settings.catchup_limit
                )
                if n:
                    log.info("caught up %s (%s): %d messages", c["title"], cid, n)
                if n >= settings.catchup_limit and last_id:
                    # Gap bigger than the limit: hand the rest to a backfill job.
                    async with pool.acquire() as conn:
                        await repo.create_job(conn, chat_id=cid, min_id=last_id, requested_by="catch_up")
                    log.info("gap in %s exceeds limit, queued backfill job from %s", cid, last_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("catch-up failed for %s (%s): %s", c["title"], cid, e)
        await asyncio.sleep(0.2)
    log.info("catch-up finished")


async def startup_sync(client: TelegramClient):
    await sync_dialogs(client)
    await catch_up(client)
