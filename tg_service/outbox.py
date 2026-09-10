"""Sending on behalf of the owner. Agents create drafts; the owner confirms in the confirmation chat
(Saved Messages or OUTBOX_CHAT), by button on the bot's card or by typing "ok <id>".

Flow: create_draft() -> notice posted to Saved Messages -> owner replies "ok <id>" / "no <id>" ->
handle_command() sends or rejects. "stop" disables sending until the service restarts.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import repo
from .bot import Bot
from .config import settings
from .db import get_pool
from .tg.ingest import ingest_message

log = logging.getLogger(__name__)

_client = None
_me_id: int | None = None
_chat = "me"            # resolved input entity for the confirmation chat
_chat_id: int | None = None
_stopped = False
bot: Bot | None = None  # when set, prompts/results/digest are posted by the bot (with buttons) instead of the userbot

_CMD = re.compile(r"^\s*(ok|да|yes|send|go|no|нет|cancel|отмена|stop|стоп)\s*#?\s*(\d+)?\s*$", re.IGNORECASE)
_YES = {"ok", "да", "yes", "send", "go"}
_NO = {"no", "нет", "cancel", "отмена"}
_STOP = {"stop", "стоп"}

_TYPE_LABEL = {"user": "личка", "chat": "группа", "supergroup": "группа", "channel": "канал"}


async def bind(client, me_id: int):
    """Bind the Telethon client and resolve OUTBOX_CHAT. Falls back to Saved Messages if it can't be resolved."""
    global _client, _me_id, _chat, _chat_id
    _client, _me_id = client, me_id
    target = (settings.outbox_chat or "me").strip()
    if target.lower() in ("me", "self", "saved", ""):
        _chat, _chat_id = "me", me_id
        log.info("outbox confirmations go to Saved Messages")
        return
    try:
        ref = int(target) if target.lstrip("-").isdigit() else target
        entity = await client.get_entity(ref)
        _chat = await client.get_input_entity(entity)
        from telethon import utils
        _chat_id = utils.get_peer_id(entity)
        log.info("outbox confirmations go to %s (%s, id %s)", utils.get_display_name(entity), type(entity).__name__, _chat_id)
    except Exception as e:
        _chat, _chat_id = "me", me_id
        log.error("OUTBOX_CHAT=%r could not be resolved (%s); falling back to Saved Messages. "
                  "Use @username, or open a dialog with that peer once so Telegram exposes it.", target, e)


    await _setup_bot(client)


async def _setup_bot(client):
    """Start the Bot API client and make sure the bot is a member of the confirmation chat."""
    global bot
    if not settings.bot_token:
        return
    b = Bot(settings.bot_token)
    try:
        await b.me()
    except Exception as e:
        log.error("BOT_TOKEN is set but getMe failed (%s); prompts will come from the userbot instead", e)
        return
    if _chat == "me":
        log.error("BOT_TOKEN needs OUTBOX_CHAT to be a group (a bot can't post into Saved Messages); using the userbot")
        return
    try:
        await b.get_chat(_chat_id)
    except Exception:
        log.info("bot @%s is not in the confirmation chat, inviting it", b.username)
        try:
            from telethon.tl import functions, types as t
            bot_entity = await client.get_entity(b.username)
            peer = await client.get_input_entity(_chat)
            if isinstance(peer, t.InputPeerChannel):
                await client(functions.channels.InviteToChannelRequest(peer, [bot_entity]))
            else:
                await client(functions.messages.AddChatUserRequest(chat_id=abs(_chat_id), user_id=bot_entity, fwd_limit=0))
            await b.get_chat(_chat_id)
        except Exception as e:
            log.error("could not add bot @%s to the confirmation chat (%s); prompts will come from the userbot", b.username, e)
            return
    bot = b
    log.info("outbox prompts and digest are posted by bot @%s", b.username)


def confirmation_chat_id() -> int | None:
    return _chat_id


def _buttons(draft_id: int):
    return [[("✅ Отправить", f"ok:{draft_id}"), ("🚫 Отменить", f"no:{draft_id}")]]


def is_stopped() -> bool:
    return _stopped


class DraftRejected(Exception):
    pass


async def _check_policy(conn, chat_id: int, text: str, has_attachments: bool = False):
    if not settings.send_enabled:
        raise DraftRejected("sending is disabled in config (SEND_ENABLED=false)")
    if _stopped:
        raise DraftRejected("sending was stopped by the owner ('stop' in Saved Messages); restart the service to re-enable")
    chat = await conn.fetchrow("SELECT type, title, username, send_policy FROM chats WHERE id = $1", chat_id)
    if chat is None:
        raise DraftRejected("unknown chat")
    if chat["type"] in ("channel", "unknown") or chat["send_policy"] == "deny":
        raise DraftRejected(f"sending to this chat is denied (type={chat['type']}, policy={chat['send_policy']})")
    if (not text or not text.strip()) and not has_attachments:
        raise DraftRejected("empty text")
    if len(text) > settings.send_max_chars:
        raise DraftRejected(f"text longer than {settings.send_max_chars} chars")
    if settings.send_rate_per_hour or settings.send_rate_per_chat_per_hour:  # 0 = no limit
        total, per_chat = await repo.outbox_rate(conn, chat_id)
        if settings.send_rate_per_hour and total >= settings.send_rate_per_hour:
            raise DraftRejected(f"rate limit: {settings.send_rate_per_hour} drafts/hour reached")
        if settings.send_rate_per_chat_per_hour and per_chat >= settings.send_rate_per_chat_per_hour:
            raise DraftRejected(f"rate limit: {settings.send_rate_per_chat_per_hour} drafts/hour for this chat reached")
    return chat


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_TG_REF = re.compile(r"^tg:(-?\d+):(\d+)$")


def _file_roots() -> list[Path]:
    roots = [Path(settings.media_dir).resolve()]
    for r in (settings.send_file_roots or "").split(","):
        if r.strip():
            roots.append(Path(r.strip()).expanduser().resolve())
    return roots


async def _resolve_attachments(conn, items: list[str] | None) -> list[dict] | None:
    """Turn agent-supplied refs into checked attachment records.
    A ref is a local file path (inside SEND_FILE_ROOTS / MEDIA_DIR) or "tg:<chat_id>:<msg_id>" for existing Telegram media."""
    if not items:
        return None
    if len(items) > settings.send_max_attachments:
        raise DraftRejected(f"too many attachments (max {settings.send_max_attachments})")
    out = []
    for ref in items:
        ref = str(ref).strip()
        m = _TG_REF.match(ref)
        if m:
            cid, mid = int(m.group(1)), int(m.group(2))
            msg = await conn.fetchrow("SELECT media_type, media FROM messages WHERE chat_id = $1 AND msg_id = $2", cid, mid)
            if msg is None or not msg["media_type"]:
                raise DraftRejected(f"{ref}: no such message with media")
            media = msg["media"] or {}
            out.append({"kind": "tg", "chat_id": cid, "msg_id": mid, "media_type": msg["media_type"],
                        "name": media.get("name") or f"{msg['media_type']} #{mid}", "size": media.get("size")})
            continue
        path = Path(ref).expanduser()
        try:
            real = path.resolve(strict=True)
        except FileNotFoundError:
            raise DraftRejected(f"{ref}: file not found")
        if not real.is_file():
            raise DraftRejected(f"{ref}: not a file")
        if not any(real == r or r in real.parents for r in _file_roots()):
            raise DraftRejected(f"{ref}: outside allowed dirs ({', '.join(str(r) for r in _file_roots())})")
        size = real.stat().st_size
        if size > settings.send_max_file_mb * 1024 * 1024:
            raise DraftRejected(f"{ref}: larger than {settings.send_max_file_mb} MB")
        out.append({"kind": "file", "path": str(real), "name": real.name, "size": size})
    return out


def _parse_schedule(value) -> datetime | None:
    """ISO 8601; a naive value is taken as the machine's local time. Must be 1 min .. 365 days ahead."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise DraftRejected(f"schedule_at: cannot parse {value!r}, use ISO 8601 like 2026-09-07T10:00")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local timezone of this machine
    now = datetime.now().astimezone()
    if dt < now + timedelta(minutes=1):
        raise DraftRejected("schedule_at must be at least 1 minute in the future")
    if dt > now + timedelta(days=365):
        raise DraftRejected("schedule_at must be within a year")
    return dt


def _fmt_size(n) -> str:
    if not n:
        return ""
    return f" ({n / 1048576:.1f} MB)" if n >= 1048576 else f" ({n / 1024:.0f} KB)"


def _chat_label(chat) -> str:
    name = chat["title"] or f"id{chat.get('id', '')}"
    uname = f" (@{chat['username']})" if chat["username"] else ""
    return f"{name}{uname} · {_TYPE_LABEL.get(chat['type'], chat['type'])}"


async def _context_lines(conn, chat_id: int) -> list[str]:
    rows = await repo.recent_context(conn, chat_id, 3)
    out = []
    for r in reversed(rows):
        who = "Я" if r["is_out"] else (r["sender_name"] or r["post_author"] or "?")
        body = r["text"] or r["transcript"] or (f"[{r['media_type']}]" if r["media_type"] else "")
        out.append(f"  <i>{escape(who)}:</i> {escape(body[:90])}{'…' if len(body) > 90 else ''}")
    return out


async def _notice_text(conn, draft, chat) -> str:
    action = draft.get("action") or "send"
    if action == "edit":
        lines = [f"✏️ <b>Черновик #{draft['id']}: изменить сообщение #{draft['target_msg_id']}</b>",
                 f"Где: {escape(_chat_label(chat))}",
                 f"Было: «{escape((draft.get('original_text') or '')[:300])}»", "———", escape(draft["text"]), "———"]
        return _card_footer(lines, draft)
    if action == "delete":
        lines = [f"🗑 <b>Черновик #{draft['id']}: удалить сообщение #{draft['target_msg_id']}</b>",
                 f"Где: {escape(_chat_label(chat))}",
                 f"Текст: «{escape((draft.get('original_text') or '')[:300])}»"]
        if draft["reason"]:
            lines.append(f"Причина: {escape(draft['reason'])}")
        return _card_footer(lines, draft)
    lines = [f"📤 <b>Черновик #{draft['id']}</b>", f"Кому: {escape(_chat_label(chat))}"]
    ctx = await _context_lines(conn, draft["chat_id"])
    if ctx:
        lines.append("Последнее в чате:")
        lines += ctx
    if draft["reply_to_msg_id"]:
        parent = await conn.fetchrow(
            "SELECT text, media_type FROM messages WHERE chat_id = $1 AND msg_id = $2", draft["chat_id"], draft["reply_to_msg_id"]
        )
        preview = (parent["text"] or f"[{parent['media_type']}]") if parent else "?"
        lines.append(f"Ответ на #{draft['reply_to_msg_id']}: «{escape(preview[:120])}»")
    if draft.get("schedule_at"):
        lines.append(f"📅 Запланировать на: <b>{draft['schedule_at'].astimezone().strftime('%d.%m.%Y %H:%M')}</b> (время машины)")
    for a in draft.get("attachments") or []:
        icon = "🖼" if a.get("media_type") == "photo" or Path(a.get("name", "")).suffix.lower() in _IMAGE_EXT else "📎"
        src = "из Telegram" if a["kind"] == "tg" else "файл"
        lines.append(f"{icon} {escape(a['name'])}{_fmt_size(a.get('size'))} · {src}")
    if draft["reason"]:
        lines.append(f"Причина: {escape(draft['reason'])}")
    if draft["requested_by"]:
        lines.append(f"От: {escape(draft['requested_by'])}")
    lines += ["———", escape(draft["text"] or "(без текста)"), "———"]
    return _card_footer(lines, draft)


def _card_footer(lines: list[str], draft) -> str:
    if bot:
        lines.append(f"Кнопки ниже или текстом <code>ok {draft['id']}</code> / <code>no {draft['id']}</code>. "
                     f"Ответ на карточку своим текстом заменяет текст. "
                     f"Истекает через {settings.send_draft_ttl_minutes} мин. <code>stop</code> выключает отправку.")
    else:
        lines.append(f"Ответь <code>ok {draft['id']}</code> чтобы отправить, <code>no {draft['id']}</code> чтобы отменить. "
                     f"Истекает через {settings.send_draft_ttl_minutes} мин. <code>stop</code> выключает отправку.")
    return "\n".join(lines)


async def create_draft(*, chat_id: int, text: str = "", reply_to_msg_id: int | None = None, reason: str | None = None,
                       requested_by: str | None = None, idempotency_key: str | None = None,
                       attachments: list[str] | None = None, schedule_at=None,
                       action: str = "send", target_msg_id: int | None = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if action not in ("send", "edit", "delete"):
            raise DraftRejected("action must be send, edit or delete")
        original = None
        if action in ("edit", "delete"):
            if not target_msg_id:
                raise DraftRejected("target_msg_id is required")
            target = await conn.fetchrow(
                "SELECT text, is_out, deleted_at, media_type FROM messages WHERE chat_id = $1 AND msg_id = $2", chat_id, target_msg_id
            )
            if target is None:
                raise DraftRejected("target message is not in the database")
            if not target["is_out"]:
                raise DraftRejected("only the owner's own messages can be edited or deleted")
            if target["deleted_at"]:
                raise DraftRejected("target message is already deleted")
            original = target["text"] or (f"[{target['media_type']}]" if target["media_type"] else "")
            if action == "delete":
                text = ""
        chat = await _check_policy(conn, chat_id, text, has_attachments=bool(attachments) or action == "delete")
        atts = await _resolve_attachments(conn, attachments) if action == "send" else None
        when = _parse_schedule(schedule_at) if action == "send" else None
        draft, created = await repo.create_draft(
            conn, chat_id=chat_id, reply_to_msg_id=reply_to_msg_id, text=text or "", reason=reason,
            requested_by=requested_by, idempotency_key=idempotency_key, ttl_minutes=settings.send_draft_ttl_minutes,
            attachments=atts, schedule_at=when, action=action, target_msg_id=target_msg_id, original_text=original,
        )
        if created:
            notice = await _notice_text(conn, draft, chat)
    if created:
        if _client is None:
            raise DraftRejected("telegram client is not bound")
        if bot:
            # preview local images in the confirmation chat so the owner sees what goes out
            for a in (atts or [])[:3]:
                if a["kind"] == "file" and Path(a["path"]).suffix.lower() in _IMAGE_EXT and a["size"] <= 10 * 1048576:
                    try:
                        await bot.send_photo(_chat_id, a["path"], caption=f"превью для черновика #{draft['id']}")
                    except Exception as e:
                        log.warning("preview failed: %s", e)
            sent = await bot.send(_chat_id, notice, buttons=_buttons(draft["id"]))
            notice_id = sent["message_id"]
        else:
            sent = await _send_me(notice, parse_mode="html", link_preview=False)
            notice_id = sent.id
        async with pool.acquire() as conn:
            await repo.set_draft_notice(conn, draft["id"], notice_id)
        log.info("draft #%s for chat %s created by %s, awaiting confirmation", draft["id"], chat_id, requested_by)
    return dict(draft)


async def _store(sent):
    """Telethon emits no NewMessage for the client's own sends, so store them ourselves."""
    try:
        await ingest_message(sent)
    except Exception as e:
        log.warning("could not store own message %s: %s", getattr(sent, "id", "?"), e)


async def _send_me(text: str, **kw):
    sent = await _client.send_message(_chat, text, **kw)
    await _store(sent)
    return sent


async def _post(text: str, reply_to: int | None = None):
    """Status line into the confirmation chat, from the bot when available."""
    if bot:
        await bot.send(_chat_id, escape(text), reply_to=reply_to)
    else:
        await _send_me(text, reply_to=reply_to)


async def _reply_notice(draft, text: str):
    """Attach the outcome to the prompt: the bot edits its card (buttons removed), the userbot replies to it."""
    try:
        if bot and draft["notice_msg_id"]:
            pool = await get_pool()
            async with pool.acquire() as conn:
                chat = await conn.fetchrow("SELECT id, type, title, username FROM chats WHERE id = $1", draft["chat_id"])
                card = await _notice_text(conn, draft, chat) if chat else escape(draft["text"])
            card = card.rsplit("\n", 1)[0] if chat else card  # drop the "buttons/ok" hint line
            await bot.edit(_chat_id, draft["notice_msg_id"], f"{card}\n\n<b>{escape(text)}</b>")
        else:
            await _send_me(text, reply_to=draft["notice_msg_id"] if draft["notice_msg_id"] else None)
    except Exception as e:
        log.warning("could not post outbox status: %s", e)
        try:
            await _post(text, reply_to=draft["notice_msg_id"])
        except Exception:
            pass


async def _send(draft):
    pool = await get_pool()
    action = draft.get("action") or "send"
    if action == "edit":
        msg = await _client.edit_message(draft["chat_id"], draft["target_msg_id"], draft["text"])
        async with pool.acquire() as conn:
            row = await repo.decide_draft(conn, draft["id"], "sent", sent_msg_id=draft["target_msg_id"])
            old = await repo.get_message_text(conn, draft["chat_id"], draft["target_msg_id"])
            if old is not None:
                await repo.record_edit(conn, draft["chat_id"], draft["target_msg_id"], old["text"], old["media"])
            await conn.execute("UPDATE messages SET text = $3, edit_date = now(), updated_at = now() WHERE chat_id = $1 AND msg_id = $2",
                               draft["chat_id"], draft["target_msg_id"], draft["text"])
        log.info("draft #%s edited msg %s in chat %s", draft["id"], draft["target_msg_id"], draft["chat_id"])
        return row
    if action == "delete":
        await _client.delete_messages(draft["chat_id"], [draft["target_msg_id"]])
        async with pool.acquire() as conn:
            row = await repo.decide_draft(conn, draft["id"], "sent", sent_msg_id=draft["target_msg_id"])
            await repo.mark_deleted(conn, [draft["target_msg_id"]], draft["chat_id"])
        log.info("draft #%s deleted msg %s in chat %s", draft["id"], draft["target_msg_id"], draft["chat_id"])
        return row
    kw = {"reply_to": draft["reply_to_msg_id"] or None}
    if draft.get("schedule_at"):
        kw["schedule"] = draft["schedule_at"]
    atts = draft.get("attachments") or []
    if atts:
        files = []
        for a in atts:
            if a["kind"] == "tg":
                src = await _client.get_messages(a["chat_id"], ids=a["msg_id"])
                if src is None or src.media is None:
                    raise RuntimeError(f"attachment {a['name']} is no longer available in Telegram")
                files.append(src.media)
            else:
                files.append(a["path"])
        sent = await _client.send_file(draft["chat_id"], files, caption=draft["text"] or None, **kw)
        first = sent[0] if isinstance(sent, list) else sent
    else:
        first = sent = await _client.send_message(draft["chat_id"], draft["text"], **kw)
    status = "scheduled" if draft.get("schedule_at") else "sent"
    async with pool.acquire() as conn:
        row = await repo.decide_draft(conn, draft["id"], status, sent_msg_id=first.id)
    if status == "sent":
        for m in (sent if isinstance(sent, list) else [sent]):
            await _store(m)
    log.info("draft #%s %s to chat %s as msg %s", draft["id"], status, draft["chat_id"], first.id)
    return row


async def handle_command(text: str) -> bool:
    """Called by the listener for the owner's outgoing messages in the confirmation chat. Returns True if handled."""
    global _stopped
    m = _CMD.match(text or "")
    if not m:
        return False
    word, num = m.group(1).lower(), m.group(2)
    pool = await get_pool()

    if word in _STOP:
        _stopped = True
        async with pool.acquire() as conn:
            rows = await repo.reject_all_pending(conn, "stopped by owner")
        await _post(f"🛑 Отправка выключена до перезапуска сервиса. Отменено черновиков: {len(rows)}.")
        log.warning("outbox stopped by owner, %d drafts rejected", len(rows))
        return True

    if not num:
        return False
    await decide(int(num), approve=word in _YES)
    return True


async def decide(draft_id: int, approve: bool) -> str:
    """Apply the owner's decision. Returns a short status text (also posted to the confirmation chat)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        draft = await repo.get_draft(conn, draft_id)
    if draft is None:
        await _post(f"Черновика #{draft_id} нет.")
        return "нет такого черновика"
    if draft["status"] != "pending":
        msg = f"Черновик #{draft_id} уже {draft['status']}."
        await _reply_notice(draft, msg)
        return msg

    if not approve:
        async with pool.acquire() as conn:
            await repo.decide_draft(conn, draft_id, "rejected", error="rejected by owner")
        await _reply_notice(draft, f"🚫 #{draft_id} отменён.")
        return "отменён"

    # confirm
    if _stopped:
        await _reply_notice(draft, "🛑 Отправка выключена (stop). Перезапусти сервис.")
        return "отправка выключена"
    async with pool.acquire() as conn:
        still = await conn.fetchval("SELECT status = 'pending' AND expires_at > now() FROM outbox WHERE id = $1", draft_id)
    if not still:
        async with pool.acquire() as conn:
            await repo.decide_draft(conn, draft_id, "expired")
        await _reply_notice(draft, f"⌛ #{draft_id} истёк, попроси агента создать новый.")
        return "истёк"
    try:
        row = await _send(draft)
    except Exception as e:
        async with pool.acquire() as conn:
            await repo.decide_draft(conn, draft_id, "failed", error=f"{type(e).__name__}: {e}")
        await _reply_notice(draft, f"❌ #{draft_id} не отправлен: {type(e).__name__}: {e}")
        log.exception("sending draft #%s failed", draft_id)
        return "ошибка отправки"
    if row["status"] == "scheduled":
        when = draft["schedule_at"].astimezone().strftime("%d.%m %H:%M")
        await _reply_notice(draft, f"📅 #{draft_id} запланировано на {when} (отменить можно в Telegram в «Отложенных»).")
        return "запланировано"
    if draft.get("action") == "edit":
        await _reply_notice(draft, f"✏️ #{draft_id}: сообщение {draft['target_msg_id']} изменено.")
        return "изменено"
    if draft.get("action") == "delete":
        await _reply_notice(draft, f"🗑 #{draft_id}: сообщение {draft['target_msg_id']} удалено.")
        return "удалено"
    await _reply_notice(draft, f"✅ #{draft_id} отправлено (msg {row['sent_msg_id']}).")
    return "отправлено"


async def cancel_by_agent(draft_id: int, requested_by: str | None = None) -> dict:
    """An agent withdraws its own pending draft (harmless, so no confirmation needed)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        draft = await repo.get_draft(conn, draft_id)
        if draft is None:
            raise DraftRejected("no such draft")
        if draft["status"] != "pending":
            raise DraftRejected(f"draft is already {draft['status']}")
        row = await repo.decide_draft(conn, draft_id, "rejected", error=f"withdrawn by {requested_by or 'agent'}")
    await _reply_notice(draft, f"↩️ #{draft_id} отозван агентом.")
    log.info("draft #%s withdrawn by %s", draft_id, requested_by)
    return dict(row)


async def on_bot_callback(q: dict):
    """Inline button pressed on a bot card. Only the owner's presses count."""
    data = q.get("data") or ""
    if q.get("from", {}).get("id") != _me_id:
        await bot.answer(q["id"], "Только владелец может подтверждать", alert=True)
        return
    if ":" not in data:
        await bot.answer(q["id"])
        return
    action, _, num = data.partition(":")
    if not num.isdigit() or action not in ("ok", "no"):
        await bot.answer(q["id"])
        return
    log.info("button %s pressed by owner for draft #%s", action, num)
    result = await decide(int(num), approve=action == "ok")
    await bot.answer(q["id"], f"#{num}: {result}")


_CARD_ID = re.compile(r"Черновик #(\d+)")


async def on_confirmation_chat_message(msg) -> bool:
    """Every message the userbot sees in the confirmation chat.
    - bot's card arriving: remember its id as the owner's account numbers it (basic groups differ per user)
    - owner's reply to a card: 'ok'/'no' decide that draft; any other text replaces the draft text
    - owner's plain 'ok N' / 'no N' / 'stop': handle_command"""
    text = msg.message or ""
    if not msg.out:
        if bot and msg.sender_id == bot.id:
            m = _CARD_ID.search(text)
            if m:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await repo.set_draft_notice_user(conn, int(m.group(1)), msg.id)
                return True
        return False
    reply_to = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
    if reply_to:
        pool = await get_pool()
        async with pool.acquire() as conn:
            draft = await repo.draft_by_notice_user(conn, reply_to)
        if draft is not None:
            word = text.strip().lower().rstrip(".!")
            if word in _YES:
                await decide(draft["id"], approve=True)
                return True
            if word in _NO:
                await decide(draft["id"], approve=False)
                return True
            if draft["status"] != "pending":
                await _post(f"Черновик #{draft['id']} уже {draft['status']}, текст не изменить.")
                return True
            if draft.get("action") == "delete":
                await _post("У удаления нет текста, нажми кнопку.")
                return True
            if not text.strip():
                return True
            async with pool.acquire() as conn:
                updated = await repo.update_draft_text(conn, draft["id"], text.strip())
                chat = await conn.fetchrow("SELECT id, type, title, username FROM chats WHERE id = $1", draft["chat_id"])
                card = await _notice_text(conn, updated, chat)
            if bot and updated["notice_msg_id"]:
                try:
                    await bot.edit(_chat_id, updated["notice_msg_id"], "✏️ <b>текст заменён</b>\n" + card, buttons=_buttons(draft["id"]))
                except Exception as e:
                    log.warning("card edit failed: %s", e)
                    await _post(f"✏️ #{draft['id']}: текст заменён.")
            else:
                await _post(f"✏️ #{draft['id']}: текст заменён на: {text.strip()[:200]}")
            log.info("draft #%s text replaced by owner", draft["id"])
            return True
    return await handle_command(text)


async def bot_loop():
    if bot:
        await bot.poll(on_bot_callback)


async def expire_loop(interval: float = 60):
    pool = await get_pool()
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await repo.expire_drafts(conn)
            for d in rows:
                await _reply_notice(d, f"⌛ #{d['id']} истёк без подтверждения.")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbox expiry failed")
        await asyncio.sleep(interval)
