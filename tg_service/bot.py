"""Minimal Telegram Bot API client (httpx, goes through TG_PROXY when set)."""
import asyncio
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(proxy=settings.tg_proxy or None, timeout=httpx.Timeout(70, connect=20))
        self.username: str | None = None
        self.id: int | None = None

    async def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        r = await self.http.post(f"{self.base}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    async def me(self):
        info = await self.call("getMe")
        self.username, self.id = info["username"], info["id"]
        return info

    async def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
                   reply_to: int | None = None, parse_mode: str | None = "HTML"):
        markup = None
        if buttons:
            markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                               reply_markup=markup, reply_to_message_id=reply_to, disable_web_page_preview=True)

    async def edit(self, chat_id: int, message_id: int, text: str, buttons=None, parse_mode: str | None = "HTML"):
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]} if buttons else {"inline_keyboard": []}
        return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                               parse_mode=parse_mode, reply_markup=markup, disable_web_page_preview=True)

    async def send_photo(self, chat_id: int, path: str, caption: str | None = None):
        with open(path, "rb") as f:
            r = await self.http.post(f"{self.base}/sendPhoto", data={"chat_id": chat_id, "caption": caption or ""},
                                     files={"photo": (path.rsplit("/", 1)[-1], f)})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"sendPhoto: {data.get('description')}")
        return data["result"]

    async def answer(self, callback_id: str, text: str | None = None, alert: bool = False):
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)
        except Exception as e:
            log.debug("answerCallbackQuery: %s", e)

    async def get_chat(self, chat_id: int):
        return await self.call("getChat", chat_id=chat_id)

    async def poll(self, on_callback, on_message=None):
        """Long-poll getUpdates forever; on_callback(query) for button presses."""
        offset = None
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50,
                                          allowed_updates=["callback_query", "message"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        await on_callback(u["callback_query"])
                    elif "message" in u and on_message:
                        await on_message(u["message"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("bot update handler failed")
