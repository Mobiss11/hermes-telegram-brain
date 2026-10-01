"""Fail-closed chat allowlist for archive ingestion and retrieval boundaries."""


def parse_chat_ids(value: str) -> frozenset[int]:
    """Parse comma-separated marked Telethon peer ids; reject malformed input."""
    ids: set[int] = set()
    for raw in (value or "").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            chat_id = int(token)
        except ValueError as exc:
            raise ValueError("TG_ALLOWED_CHAT_IDS must contain only integer peer ids") from exc
        if chat_id == 0:
            raise ValueError("TG_ALLOWED_CHAT_IDS must not contain 0")
        ids.add(chat_id)
    return frozenset(ids)