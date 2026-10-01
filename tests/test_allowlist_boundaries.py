import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tg_service import main as service_main
from tg_service.tg import ingest
from tg_service.tg import listener
from tg_service.media import worker


class _Connection:
    async def fetchval(self, *_args, **_kwargs):
        return False


class _Acquire:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


class AllowlistBoundaryTests(unittest.TestCase):
    def test_empty_allowlist_stops_before_migrations_or_telegram_login(self):
        with (
            patch.object(service_main.settings, "tg_allowed_chat_ids", ""),
            patch.object(service_main, "migrate", new_callable=AsyncMock) as migrate,
        ):
            asyncio.run(service_main.main())
        migrate.assert_not_awaited()

    def test_non_allowlisted_message_never_opens_a_database_pool(self):
        with (
            patch.object(ingest.settings, "tg_allowed_chat_ids", "-100100"),
            patch.object(ingest, "message_row", return_value={"chat_id": -100200}),
            patch.object(ingest, "get_pool", new_callable=AsyncMock) as get_pool,
        ):
            result = asyncio.run(ingest.ingest_message(object()))
        self.assertIsNone(result)
        get_pool.assert_not_awaited()

    def test_allowlisted_message_reaches_the_next_ingestion_boundary(self):
        with (
            patch.object(ingest.settings, "tg_allowed_chat_ids", "-100100"),
            patch.object(ingest, "message_row", return_value={"chat_id": -100100, "msg_id": 1, "date": None}),
            patch.object(ingest, "get_pool", new_callable=AsyncMock, return_value=_Pool()) as get_pool,
            patch.object(ingest.repo, "ensure_chat_stub", new_callable=AsyncMock),
            patch.object(ingest.repo, "upsert_message", new_callable=AsyncMock),
            patch.object(ingest.repo, "bump_chat_cursor", new_callable=AsyncMock),
        ):
            result = asyncio.run(ingest.ingest_message(object()))
        self.assertEqual(result, {"chat_id": -100100, "msg_id": 1, "date": None})
        get_pool.assert_awaited_once()

    def test_live_listener_refuses_a_non_allowlisted_chat_before_a_pool_lookup(self):
        with (
            patch.object(listener.settings, "tg_allowed_chat_ids", "-100100"),
            patch.object(listener, "get_pool", new_callable=AsyncMock) as get_pool,
        ):
            allowed = asyncio.run(listener._tracked(-100200))
        self.assertFalse(allowed)
        get_pool.assert_not_awaited()

    def test_media_worker_refuses_an_empty_allowlist_before_any_pool_lookup(self):
        with (
            patch.object(worker.settings, "tg_allowed_chat_ids", ""),
            patch.object(worker, "get_pool", new_callable=AsyncMock) as get_pool,
        ):
            asyncio.run(worker.main())
        get_pool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()