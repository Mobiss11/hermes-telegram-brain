"""Single process: Telethon client + live listener + backfill worker + HTTP API on one event loop."""
import asyncio
import logging

import uvicorn

from .api.app import create_app
from .config import settings
from .db import close_pool, migrate
from .media.cleanup import cleanup_loop
from .media.download import download_worker
from . import digest, health, outbox
from .tg.backfill import backfill_worker
from .tg.client import make_client
from .tg.listener import register_handlers
from .tg.sync import startup_sync

log = logging.getLogger("tg_service")


async def main():
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token in the URL

    await migrate()
    client = make_client()
    await client.connect()
    if not await client.is_user_authorized():
        log.error("session is not authorized, run `tg-login` first")
        await client.disconnect()
        await close_pool()
        return
    me = await client.get_me()
    log.info("logged in as %s (%s)", me.first_name, me.id)

    register_handlers(client, me_id=me.id)
    await outbox.bind(client, me.id)
    health.bind(client)
    server = uvicorn.Server(uvicorn.Config(create_app(), host=settings.api_host, port=settings.api_port, log_level="info"))

    background = [
        asyncio.create_task(startup_sync(client), name="startup_sync"),
        asyncio.create_task(backfill_worker(client), name="backfill"),
        asyncio.create_task(download_worker(client), name="media_download"),
        asyncio.create_task(cleanup_loop(), name="media_cleanup"),
        asyncio.create_task(outbox.expire_loop(), name="outbox_expiry"),
        asyncio.create_task(outbox.bot_loop(), name="outbox_bot"),
        asyncio.create_task(digest.digest_loop(), name="digest"),
        asyncio.create_task(health.health_loop(), name="health"),
        asyncio.create_task(client.run_until_disconnected(), name="telethon"),
    ]
    try:
        await server.serve()  # returns on SIGINT/SIGTERM
    finally:
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await client.disconnect()
        await close_pool()
        log.info("stopped")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
