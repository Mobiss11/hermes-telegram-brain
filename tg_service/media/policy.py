"""What gets downloaded / transcribed / extracted automatically, and how files are named."""
import mimetypes
import re
from pathlib import Path

from ..config import settings

AUDIO_TYPES = {"voice", "video_note", "audio", "video", "gif"}
TRANSCRIBE_AUTO_TYPES = {"voice", "video_note"}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

EXTRACTABLE_MIMES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/json": "json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
EXTRACTABLE_EXT = {".pdf", ".txt", ".md", ".csv", ".json", ".docx", ".xlsx", ".log", ".py", ".sql", ".yaml", ".yml", ".toml"}


def doc_kind(mime: str | None, name: str | None) -> str | None:
    """Return extractor key for a document, or None if we can't read it."""
    if mime in EXTRACTABLE_MIMES:
        return EXTRACTABLE_MIMES[mime]
    ext = Path(name or "").suffix.lower()
    if ext in EXTRACTABLE_EXT:
        return ext.lstrip(".") if ext not in (".log", ".py", ".sql", ".yaml", ".yml", ".toml") else "txt"
    return None


def is_image(media_type: str | None, media: dict | None) -> bool:
    media = media or {}
    return media_type == "photo" or (
        media_type == "document" and (media.get("mime") in IMAGE_MIMES or Path(media.get("name") or "").suffix.lower() in IMAGE_EXT)
    )


def default_action_for(media_type: str | None, media: dict | None) -> str | None:
    """Follow-up after a download when the caller didn't say: transcribe audio/video, describe images, extract docs."""
    if media_type in AUDIO_TYPES:
        return "transcribe"
    if is_image(media_type, media):
        return "describe"
    if media_type == "document" and doc_kind((media or {}).get("mime"), (media or {}).get("name")):
        return "extract"
    return None


def auto_action_for(media_type: str | None, media: dict | None, chat_type: str | None) -> str | None:
    """Automatic pipeline for freshly ingested messages (live + backfill)."""
    if chat_type == "channel" or chat_type is None:
        return None
    media = media or {}
    if settings.media_auto_transcribe and media_type in TRANSCRIBE_AUTO_TYPES:
        return "transcribe"
    if settings.media_auto_describe and is_image(media_type, media):
        size = media.get("size") or 0
        return "describe" if size <= settings.media_auto_describe_max_mb * 1024 * 1024 else None
    if settings.media_auto_extract and media_type == "document":
        size = media.get("size") or 0
        if size <= settings.media_auto_extract_max_mb * 1024 * 1024 and doc_kind(media.get("mime"), media.get("name")):
            return "extract"
    return None


_SAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


def file_path_for(chat_id: int, msg_id: int, media_type: str | None, media: dict | None) -> Path:
    media = media or {}
    name = media.get("name")
    ext = Path(name).suffix.lower() if name else ""
    if not ext:
        ext = mimetypes.guess_extension(media.get("mime") or "") or {
            "voice": ".ogg", "video_note": ".mp4", "photo": ".jpg", "video": ".mp4", "gif": ".mp4",
            "sticker": ".webp", "audio": ".mp3",
        }.get(media_type or "", ".bin")
    stem = f"{msg_id}"
    if name:
        stem += "_" + _SAFE.sub("_", Path(name).stem)[:60]
    return Path(settings.media_dir) / str(chat_id) / f"{stem}{ext}"
