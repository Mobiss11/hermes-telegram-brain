import unittest

from tg_service.tg.allowlist import parse_chat_ids


class ParseChatIdsTests(unittest.TestCase):
    def test_parses_marked_peer_ids_and_deduplicates(self):
        self.assertEqual(parse_chat_ids("-1001, 42, -1001"), frozenset({-1001, 42}))

    def test_empty_value_is_empty_and_can_fail_closed_at_startup(self):
        self.assertEqual(parse_chat_ids("  , "), frozenset())

    def test_rejects_non_integer_and_zero(self):
        with self.assertRaises(ValueError):
            parse_chat_ids("@channel")
        with self.assertRaises(ValueError):
            parse_chat_ids("0")


if __name__ == "__main__":
    unittest.main()