import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tg_service import repo


class _Connection:
    def __init__(self):
        self.fetchrow = AsyncMock(return_value=None)
        self.execute = AsyncMock()


class AllowlistJobTests(unittest.TestCase):
    def test_non_allowlisted_jobs_are_not_claimed_or_rewritten_when_empty(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", ""):
            self.assertIsNone(asyncio.run(repo.claim_next_job(conn)))
            self.assertIsNone(asyncio.run(repo.claim_media_job(conn, ("download",))))
            asyncio.run(repo.reset_running_jobs(conn))
            asyncio.run(repo.reset_running_media_jobs(conn, ("download",)))
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

    def test_claim_queries_are_scoped_to_the_allowlist(self):
        conn = _Connection()
        with patch.object(repo.settings, "tg_allowed_chat_ids", "-100100"):
            asyncio.run(repo.claim_next_job(conn))
            asyncio.run(repo.claim_media_job(conn, ("download",)))
        sync_query, sync_ids = conn.fetchrow.await_args_list[0].args
        media_query, actions, media_ids = conn.fetchrow.await_args_list[1].args
        self.assertIn("chat_id = ANY($1::bigint[])", sync_query)
        self.assertEqual(sync_ids, [-100100])
        self.assertIn("chat_id = ANY($2::bigint[])", media_query)
        self.assertEqual(actions, ["download"])
        self.assertEqual(media_ids, [-100100])


if __name__ == "__main__":
    unittest.main()