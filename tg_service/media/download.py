"""Download stage: runs inside the main process because it needs the Telethon client."""
import asyncio
import hashlib
import logging
from pathlib import Path

from telethon import TelegramClient

from .. import repo
from ..config import settings
from ..db import get_pool
from .policy import default_action_for, file_path_for

log = logging.getLogger(__name__)


class MediaGone(Exception):
    """The message or its media no longer exists in Telegram; not an error of ours."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _download(client: TelegramClient, job) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg = await conn.fetchrow(
            "SELECT media_type, media FROM messages WHERE chat_id = $1 AND msg_id = $2", job["chat_id"], job["msg_id"]
        )
        existing = await repo.get_media_file(conn, job["chat_id"], job["msg_id"])
    if msg is None:
        raise RuntimeError("message not in database")
    if not msg["media_type"]:
        raise RuntimeError("message has no media")

    if existing and Path(existing["path"]).exists():
        path = Path(existing["path"])
    else:
        size = (msg["media"] or {}).get("size") or 0
        if size > settings.media_max_download_mb * 1024 * 1024:
            raise RuntimeError(f"file too large: {size // 1048576} MB")
        tg_msg = await client.get_messages(job["chat_id"], ids=job["msg_id"])
        if tg_msg is None:
            async with pool.acquire() as conn:
                await repo.mark_deleted(conn, [job["msg_id"]], job["chat_id"])
            raise MediaGone("message deleted in Telegram")
        if tg_msg.media is None:
            raise MediaGone("media removed from the message")
        path = file_path_for(job["chat_id"], job["msg_id"], msg["media_type"], msg["media"])
        path.parent.mkdir(parents=True, exist_ok=True)
        result = await client.download_media(tg_msg, file=str(path))
        if not result:
            raise RuntimeError("download_media returned nothing")
        path = Path(result)
        async with pool.acquire() as conn:
            await repo.upsert_media_file(
                conn, job["chat_id"], job["msg_id"], str(path.resolve()),
                (msg["media"] or {}).get("mime"), path.stat().st_size, _sha256(path),
            )
        log.info("downloaded %s/%s -> %s (%d bytes)", job["chat_id"], job["msg_id"], path, path.stat().st_size)

    then = job["then"]
    if then == "auto":
        then = default_action_for(msg["media_type"], msg["media"])
    if then in ("transcribe", "extract", "describe"):
        async with pool.acquire() as conn:
            await repo.enqueue_media_job(conn, job["chat_id"], job["msg_id"], then, requested_by=job["requested_by"])


async def download_worker(client: TelegramClient, poll_interval: float = 2.0):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await repo.reset_running_media_jobs(conn, ("download",))
    while True:
        async with pool.acquire() as conn:
            job = await repo.claim_media_job(conn, ("download",))
        if job is None:
            await asyncio.sleep(poll_interval)
            continue
        try:
            await _download(client, job)
            status, error = "done", None
        except asyncio.CancelledError:
            raise
        except MediaGone as e:
            log.info("download job %s skipped: %s", job["id"], e)
            status, error = "skipped", str(e)
        except Exception as e:
            log.warning("download job %s failed: %s", job["id"], e)
            status, error = "failed", f"{type(e).__name__}: {e}"
        async with pool.acquire() as conn:
            await repo.finish_media_job(conn, job["id"], status, error)
