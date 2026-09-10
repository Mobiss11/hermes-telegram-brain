"""Image description + OCR through a vision model on OpenRouter (default MiniMax M3)."""
import base64
import io
import logging
from pathlib import Path

import httpx

from ..config import settings

log = logging.getLogger(__name__)

PROMPT = ("Опиши изображение в 2-4 предложениях: что это, что на нём происходит, какие объекты и люди. "
          "Затем на отдельной строке напиши 'OCR:' и дословно перепиши весь текст с изображения, сохраняя строки "
          "(если текста нет, напиши 'нет'). Отвечай на языке текста на изображении, иначе по-русски.")

_MAX_SIDE = 1600


def _prepare(path: Path) -> tuple[bytes, str]:
    """Downscale big images so the request stays small; returns (bytes, mime)."""
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > _MAX_SIDE:
            im.thumbnail((_MAX_SIDE, _MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def _images_from_pdf(path: Path, pages: int) -> list[bytes]:
    import pypdfium2 as pdfium

    out = []
    doc = pdfium.PdfDocument(str(path))
    for i in range(min(len(doc), pages)):
        bitmap = doc[i].render(scale=1.5)
        im = bitmap.to_pil().convert("RGB")
        if max(im.size) > _MAX_SIDE:
            im.thumbnail((_MAX_SIDE, _MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        out.append(buf.getvalue())
    return out


async def _ask(images: list[tuple[bytes, str]], prompt: str = PROMPT) -> tuple[str, dict]:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    content = [{"type": "text", "text": prompt}]
    for data, mime in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    body = {"model": settings.vision_model, "max_tokens": 1500, "messages": [{"role": "user", "content": content}]}
    async with httpx.AsyncClient(proxy=settings.tg_proxy or None, timeout=180) as c:
        r = await c.post("https://openrouter.ai/api/v1/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {settings.openrouter_api_key}",
                                  "HTTP-Referer": "https://github.com/Mobiss11/hermes-telegram-brain", "X-Title": "tg-service"})
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"vision: {d['error'].get('message', d['error'])}")
    text = (d["choices"][0]["message"]["content"] or "").strip()
    usage = d.get("usage") or {}
    return text, {"model": settings.vision_model, "tokens": usage.get("total_tokens"), "cost": usage.get("cost")}


async def describe_image(path: Path) -> tuple[str, dict]:
    data, mime = _prepare(path)
    return await _ask([(data, mime)])


async def describe_pdf_pages(path: Path, pages: int | None = None) -> tuple[str, dict]:
    imgs = _images_from_pdf(path, pages or settings.vision_pdf_pages)
    if not imgs:
        raise RuntimeError("pdf has no pages")
    text, meta = await _ask([(b, "image/jpeg") for b in imgs],
                            prompt="Это страницы отсканированного документа. Перепиши дословно весь текст со всех страниц, "
                                   "страницы разделяй строкой '--- page N ---'. Не пересказывай, только текст.")
    meta["pages"] = len(imgs)
    return text, meta
