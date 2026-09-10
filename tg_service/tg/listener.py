"""Live update handlers."""
import logging

from telethon import TelegramClient, events

from .. import outbox, repo
from ..db import get_pool
from .ingest import ingest_message

log = logging.getLogger(__name__)


async def _tracked(chat_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await repo.is_tracked(conn, chat_id)


def register_handlers(client: TelegramClient, me_id: int | None = None):
    @client.on(events.NewMessage())
    async def on_new(event: events.NewMessage.Event):
        if not await _tracked(event.chat_id):
            return
        try:
            chat = await event.get_chat()
            sender = await event.get_sender()
            await ingest_message(event.message, chat=chat, sender=sender)
        except Exception:
            log.exception("failed to ingest new message %s/%s", event.chat_id, event.message.id)
        # confirmation chat: owner's replies drive the outbox, bot cards get their user-side ids remembered
        if event.chat_id == outbox.confirmation_chat_id():
            try:
                await outbox.on_confirmation_chat_message(event.message)
            except Exception:
                log.exception("outbox command failed")

    @client.on(events.MessageEdited())
    async def on_edit(event: events.MessageEdited.Event):
        if not await _tracked(event.chat_id):
            return
        try:
            sender = await event.get_sender()
            await ingest_message(event.message, sender=sender, bump_cursor=False, track_edit=True)
        except Exception:
            log.exception("failed to ingest edit %s/%s", event.chat_id, event.message.id)

    @client.on(events.MessageDeleted())
    async def on_delete(event: events.MessageDeleted.Event):
        pool = await get_pool()
        async with pool.acquire() as conn:
            n = await repo.mark_deleted(conn, list(event.deleted_ids), event.chat_id)
        log.info("deleted %d ids in chat %s -> %d rows marked", len(event.deleted_ids), event.chat_id, n)

    @client.on(events.ChatAction())
    async def on_action(event: events.ChatAction.Event):
        # Service messages (joins, pins, title changes, calls) don't come through NewMessage.
        msg = event.action_message
        if msg is None or not await _tracked(event.chat_id):
            return
        try:
            chat = await event.get_chat()
            await ingest_message(msg, chat=chat)
        except Exception:
            log.exception("failed to ingest service message %s/%s", event.chat_id, msg.id)

    log.info("update handlers registered")
