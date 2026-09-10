"""Worker executing sync_jobs. Runs in the same process as the Telethon client."""
import asyncio
import logging

from telethon import TelegramClient

from .. import repo
from ..config import settings
from ..db import get_pool
from .ingest import ingest_batch

log = logging.getLogger(__name__)


async def _run_job(client: TelegramClient, job):
    pool = await get_pool()
    kw = {}
    if job["to_date"]:
        kw["offset_date"] = job["to_date"]
    if job["min_id"]:
        kw["min_id"] = job["min_id"]
    if job["max_id"]:
        kw["max_id"] = job["max_id"]
    if job["max_messages"]:
        kw["limit"] = job["max_messages"]
    from_date = job["from_date"]

    processed, last_id, batch = 0, None, []
    async for m in client.iter_messages(job["chat_id"], **kw):
        if from_date and m.date < from_date:
            break
        batch.append(m)
        last_id = m.id
        if len(batch) >= settings.backfill_batch:
            processed += await ingest_batch(client, batch)
            batch = []
            async with pool.acquire() as conn:
                await repo.job_progress(conn, job["id"], processed, last_id)
                if await repo.job_status(conn, job["id"]) == "cancelled":
                    log.info("job %s cancelled after %d messages", job["id"], processed)
                    return
    processed += await ingest_batch(client, batch)
    async with pool.acquire() as conn:
        await repo.job_progress(conn, job["id"], processed, last_id)
        await repo.finish_job(conn, job["id"], "done")
    log.info("job %s done: %d messages from chat %s", job["id"], processed, job["chat_id"])


async def backfill_worker(client: TelegramClient, poll_interval: float = 2.0):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await repo.reset_running_jobs(conn)
    while True:
        async with pool.acquire() as conn:
            job = await repo.claim_next_job(conn)
        if job is None:
            await asyncio.sleep(poll_interval)
            continue
        log.info("job %s started: chat %s %s..%s", job["id"], job["chat_id"], job["from_date"], job["to_date"])
        try:
            await _run_job(client, job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("job %s failed", job["id"])
            async with pool.acquire() as conn:
                await repo.finish_job(conn, job["id"], "failed", f"{type(e).__name__}: {e}")
