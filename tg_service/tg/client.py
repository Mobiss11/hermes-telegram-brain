from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient

from ..config import settings


def _proxy():
    """Parse TG_PROXY (socks5://[user:pass@]host:port, also socks4/http) into Telethon's proxy tuple."""
    if not settings.tg_proxy:
        return None
    u = urlparse(settings.tg_proxy)
    scheme = u.scheme.lower()
    if scheme not in ("socks5", "socks4", "http"):
        raise ValueError(f"unsupported TG_PROXY scheme: {u.scheme}")
    proxy = [scheme, u.hostname, u.port or 1080, True]  # rdns=True: resolve DC hosts on the proxy side
    if u.username:
        proxy += [u.username, u.password or ""]
    return tuple(proxy)


def make_client() -> TelegramClient:
    Path(settings.tg_session).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.tg_session,
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=_proxy(),
        flood_sleep_threshold=settings.flood_sleep_threshold,
        sequential_updates=True,
        catch_up=False,  # we do our own gap-filling, see sync.py
    )
