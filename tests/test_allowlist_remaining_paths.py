import asyncio
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from tg_service import repo
from tg_service.tg import sync


class _Connection:
    def __init__(self):
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock()


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class RemainingAllowlistPathTests(unittest.TestCase):
    def test_outbox_reads_and_mutations_are_scoped(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", "-100100"):
            asyncio.run(repo.get_draft(conn, 1))
            asyncio.run(repo.list_drafts(conn, None))
            asyncio.run(repo.update_draft_text(conn, 1, "replacement"))
            asyncio.run(repo.decide_draft(conn, 1, "rejected"))
            asyncio.run(repo.expire_drafts(conn))
            asyncio.run(repo.reject_all_pending(conn, "stopped"))
            asyncio.run(repo.set_draft_notice(conn, 1, 2))
            asyncio.run(repo.set_draft_notice_user(conn, 1, 2))

        queries = [call.args[0] for call in conn.fetchrow.await_args_list + conn.fetch.await_args_list + conn.execute.await_args_list]
        self.assertEqual(len(queries), 8)
        self.assertTrue(all("chat_id = ANY(" in query for query in queries))

    def test_job_and_media_id_mutations_are_scoped(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", "-100100"):
            asyncio.run(repo.job_progress(conn, 1, 3, 9))
            asyncio.run(repo.job_status(conn, 1))
            asyncio.run(repo.finish_job(conn, 1, "done"))
            asyncio.run(repo.finish_media_job(conn, 1, "done"))
            asyncio.run(repo.get_media_job(conn, 1))
            asyncio.run(repo.expired_media_files(conn, "2026-01-01"))

        queries = [call.args[0] for call in conn.fetchrow.await_args_list + conn.fetch.await_args_list + conn.fetchval.await_args_list + conn.execute.await_args_list]
        self.assertEqual(len(queries), 6)
        self.assertTrue(all("chat_id = ANY(" in query for query in queries))

    def test_catch_up_query_is_scoped_before_rows_are_read(self):
        conn = _Connection()
        pool = _Pool(conn)
        with (
            patch.object(sync.settings, "tg_allowed_chat_ids", "-100100"),
            patch.object(sync, "get_pool", new_callable=AsyncMock, return_value=pool),
        ):
            asyncio.run(sync.catch_up(cast(Any, object())))

        self.assertIsNotNone(conn.fetch.await_args)
        query, allowed = conn.fetch.await_args.args
        self.assertIn("id = ANY($1::bigint[])", query)
        self.assertEqual(allowed, [-100100])


if __name__ == "__main__":
    unittest.main()
