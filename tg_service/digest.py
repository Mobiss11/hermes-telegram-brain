"""Daily digest of what went out through agents, posted by the bot into the confirmation chat."""
import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from . import outbox
from .config import settings
from .db import get_pool

log = logging.getLogger(__name__)

_STATUS = {"sent": "✅", "scheduled": "📅", "rejected": "🚫", "expired": "⌛", "failed": "❌", "pending": "⏳"}


async def build_digest(hours: int = 24) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        drafts = await conn.fetch(
            """
            SELECT o.id, o.status, o.text, o.requested_by, o.sent_msg_id, c.title, c.username
            FROM outbox o LEFT JOIN chats c ON c.id = o.chat_id
            WHERE o.created_at > now() - make_interval(hours => $1) ORDER BY o.id
            """, hours,
        )
        stats = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM messages WHERE inserted_at > now() - make_interval(hours => $1)) AS msgs,
                   (SELECT count(*) FROM message_content WHERE kind = 'transcript' AND created_at > now() - make_interval(hours => $1)) AS transcripts,
                   (SELECT count(*) FROM message_content WHERE kind = 'document' AND created_at > now() - make_interval(hours => $1)) AS docs,
                   (SELECT count(*) FROM message_content WHERE kind = 'image' AND created_at > now() - make_interval(hours => $1)) AS images,
                   (SELECT coalesce(sum((meta->>'cost')::numeric), 0) FROM message_content WHERE kind = 'image' AND created_at > now() - make_interval(hours => $1)) AS vision_cost,
                   (SELECT count(*) FROM media_jobs WHERE status = 'failed' AND created_at > now() - make_interval(hours => $1)) AS media_failed
            """, hours,
        )
    counts = {}
    for d in drafts:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    lines = [f"📋 <b>Сводка за {hours} ч</b>"]
    if drafts:
        lines.append("Черновики: " + ", ".join(f"{_STATUS.get(k, k)} {k} {v}" for k, v in sorted(counts.items())))
        for d in drafts:
            who = d["title"] or "?"
            lines.append(f"{_STATUS.get(d['status'], '•')} #{d['id']} → {escape(who)}: «{escape((d['text'] or '')[:70])}»"
                         + (f" ({escape(d['requested_by'])})" if d["requested_by"] else ""))
    else:
        lines.append("Через агента ничего не отправлялось.")
    lines.append(f"Сообщений записано: {stats['msgs']}, транскриптов: {stats['transcripts']}, документов: {stats['docs']}, "
                 f"картинок: {stats['images']} (${float(stats['vision_cost'] or 0):.2f})"
                 + (f", ошибок медиа: {stats['media_failed']}" if stats["media_failed"] else ""))
    async with pool.acquire() as conn:
        health = await conn.fetch("SELECT key, ok, detail FROM health_state ORDER BY key")
    bad = [f"{h['key']}: {h['detail']}" for h in health if not h["ok"]]
    lines.append("Здоровье: " + ("⚠️ " + "; ".join(bad) if bad else "✅ всё в норме"))
    return "\n".join(lines)


def _next_run(now: datetime) -> datetime:
    hh, mm = (int(x) for x in settings.digest_time.split(":"))
    run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run


async def digest_loop():
    if not settings.digest_time:
        return
    while True:
        now = datetime.now()
        wait = (_next_run(now) - now).total_seconds()
        log.info("next digest in %.0f min", wait / 60)
        await asyncio.sleep(wait)
        try:
            text = await build_digest()
            if outbox.bot:
                await outbox.bot.send(outbox.confirmation_chat_id(), text)
            else:
                await outbox._send_me(text, parse_mode="html", link_preview=False)
            log.info("digest posted")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("digest failed")
