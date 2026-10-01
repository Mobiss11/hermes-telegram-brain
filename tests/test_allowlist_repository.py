import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tg_service import repo


class _Connection:
    def __init__(self):
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.execute = AsyncMock()


class AllowlistRepositoryTests(unittest.TestCase):
    def test_chat_scoped_read_and_write_reject_nonallowlisted_id(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", "-100100"):
            with self.assertRaises(PermissionError):
                asyncio.run(repo.get_message(conn, -100200, 1))
            with self.assertRaises(PermissionError):
                asyncio.run(repo.upsert_message(conn, {"chat_id": -100200}))
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

    def test_searches_and_media_backlog_are_sql_scoped(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", "-100100"):
            asyncio.run(repo.search_messages(conn, "test"))
            asyncio.run(repo.semantic_search(conn, "[0.0]"))
            asyncio.run(repo.media_candidates(conn, since="2026-01-01", kinds=["photo"]))
        search_query, *search_args = conn.fetch.await_args_list[0].args
        semantic_query, *semantic_args = conn.fetch.await_args_list[1].args
        media_query, *media_args = conn.fetch.await_args_list[2].args
        self.assertIn("m.chat_id = ANY($8::bigint[])", search_query)
        self.assertEqual(search_args[-1], [-100100])
        self.assertIn("m.chat_id = ANY($6::bigint[])", semantic_query)
        self.assertEqual(semantic_args[-1], [-100100])
        self.assertIn("m.chat_id = ANY($6::bigint[])", media_query)
        self.assertEqual(media_args[-1], [-100100])


if __name__ == "__main__":
    unittest.main()
