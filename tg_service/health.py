"""Periodic self-checks; the bot posts to the confirmation chat when something breaks or recovers."""
import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone

import httpx

from . import outbox, repo
from .config import settings
from .db import get_pool

log = logging.getLogger(__name__)
_client = None

LABELS = {
    "telegram": "соединение с Telegram",
    "listener": "поток сообщений",
    "proxy": "прокси до Telegram",
    "worker": "медиа-воркер",
    "media_jobs": "медиа-задачи",
    "disk": "диск",
    "db": "база данных",
}


def bind(client):
    global _client
    _client = client


def _in_quiet_hours(now: datetime) -> bool:
    try:
        a, b = settings.health_quiet_hours.split("-")
        start = now.replace(hour=int(a[:2]), minute=int(a[3:5]), second=0, microsecond=0)
        end = now.replace(hour=int(b[:2]), minute=int(b[3:5]), second=0, microsecond=0)
    except Exception:
        return False
    return start <= now < end if start <= end else (now >= start or now < end)


async def run_checks() -> dict[str, tuple[bool, str]]:
    checks: dict[str, tuple[bool, str]] = {}
    pool = await get_pool()

    try:
        async with pool.acquire() as conn:
            newest = await conn.fetchval("SELECT max(date) FROM messages")
            failed = await conn.fetchval(
                "SELECT count(*) FROM media_jobs WHERE status = 'failed' AND finished_at > now() - interval '1 hour'")
        checks["db"] = (True, "ok")
    except Exception as e:
        checks["db"] = (False, f"{type(e).__name__}: {e}")
        return checks

    connected = bool(_client and _client.is_connected())
    checks["telegram"] = (connected, "подключено" if connected else "клиент отключён")

    now_local = datetime.now()
    age = (datetime.now(timezone.utc) - newest) if newest else timedelta(days=1)
    limit = timedelta(hours=3) if _in_quiet_hours(now_local) else timedelta(minutes=45)
    checks["listener"] = (age < limit, f"последнее сообщение {int(age.total_seconds() // 60)} мин назад")

    if settings.tg_proxy:
        try:
            host = settings.tg_proxy.split("@")[-1].split("//")[-1]
            h, p = host.split(":")
            _, w = await asyncio.wait_for(asyncio.open_connection(h, int(p)), timeout=5)
            w.close()
            checks["proxy"] = (True, "порт отвечает")
        except Exception as e:
            checks["proxy"] = (False, f"{settings.tg_proxy}: {type(e).__name__}")

    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"http://127.0.0.1:{settings.media_worker_port}/health")
        d = r.json()
        checks["worker"] = (True, f"эмбеддингов {d.get('embedded')}/{d.get('eligible')}")
    except Exception as e:
        checks["worker"] = (False, f"не отвечает ({type(e).__name__})")

    checks["media_jobs"] = (failed < 10, f"ошибок за час: {failed}")

    free_gb = shutil.disk_usage(settings.media_dir if __import__('os').path.exists(settings.media_dir) else ".").free / 1e9
    checks["disk"] = (free_gb > 10, f"свободно {free_gb:.0f} ГБ")
    return checks


async def health_loop():
    if not settings.health_interval:
        return
    await asyncio.sleep(60)  # let startup sync settle
    pool = await get_pool()
    while True:
        try:
            checks = await run_checks()
            async with pool.acquire() as conn:
                previous = await repo.health_get(conn)
                for key, (ok, detail) in checks.items():
                    was = previous.get(key)
                    if was is not None and was["ok"] != ok:
                        text = (f"⚠️ {LABELS.get(key, key)}: {detail}" if not ok
                                else f"✅ {LABELS.get(key, key)} снова в норме: {detail}")
                        try:
                            await outbox._post(text)
                        except Exception as e:
                            log.warning("health alert failed: %s", e)
                        log.warning("health %s -> %s: %s", key, "ok" if ok else "FAIL", detail)
                    await repo.health_set(conn, key, ok, detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("health check failed")
        await asyncio.sleep(settings.health_interval)
