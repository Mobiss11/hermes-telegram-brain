"""Delete downloaded files after MEDIA_RETENTION_DAYS. Transcripts / document text stay in message_content;
a later fetch_media simply downloads the file again."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import repo
from ..config import settings
from ..db import get_pool

log = logging.getLogger(__name__)


async def cleanup_once() -> tuple[int, int]:
    if settings.media_retention_days <= 0:
        return 0, 0
    pool = await get_pool()
    before = datetime.now(timezone.utc) - timedelta(days=settings.media_retention_days)
    async with pool.acquire() as conn:
        rows = await repo.expired_media_files(conn, before)
    n, freed = 0, 0
    for r in rows:
        path = Path(r["path"])
        try:
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
            parent = path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as e:
            log.warning("could not delete %s: %s", path, e)
            continue
        async with pool.acquire() as conn:
            await repo.delete_media_file(conn, r["chat_id"], r["msg_id"])
        n += 1
    if n:
        log.info("media cleanup: removed %d files older than %d days, freed %.1f MB",
                 n, settings.media_retention_days, freed / 1048576)
    return n, freed


async def cleanup_loop(interval: float = 3600):
    while True:
        try:
            await cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("media cleanup failed")
        await asyncio.sleep(interval)
