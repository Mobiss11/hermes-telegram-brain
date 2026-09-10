"""Whisper via mlx-whisper (Apple Silicon). The model is loaded on first use; the worker process
exits after idling so memory is released, launchd starts it again."""
import logging
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)


def transcribe(path: Path) -> tuple[str, dict]:
    import mlx_whisper

    kw = {"path_or_hf_repo": settings.whisper_model, "verbose": False}
    if settings.whisper_language:
        kw["language"] = settings.whisper_language
    result = mlx_whisper.transcribe(str(path), **kw)
    segments = result.get("segments") or []
    text = (result.get("text") or "").strip()
    duration = float(segments[-1]["end"]) if segments else None
    meta = {
        "language": result.get("language"),
        "model": settings.whisper_model,
        "duration": duration,
        "segments": len(segments),
    }
    return text, meta
