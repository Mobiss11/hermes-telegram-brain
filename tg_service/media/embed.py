"""Local sentence embeddings (multilingual e5) for semantic search. Loaded lazily, unloaded when idle."""
import gc
import logging
import time

from ..config import settings

log = logging.getLogger(__name__)

_model = None
_last_used = 0.0
DIM = 384


def _load():
    global _model, _last_used
    if _model is None:
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        _model = SentenceTransformer(settings.embed_model)
        log.info("embedding model %s loaded in %.1fs", settings.embed_model, time.time() - t0)
    _last_used = time.time()
    return _model


def encode(texts: list[str], kind: str = "passage") -> list[list[float]]:
    """e5 models expect 'query: ' / 'passage: ' prefixes."""
    m = _load()
    prefixed = [f"{kind}: {t}" for t in texts]
    vecs = m.encode(prefixed, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


def unload_if_idle(idle_seconds: int) -> bool:
    global _model
    if _model is not None and time.time() - _last_used > idle_seconds:
        _model = None
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass
        log.info("embedding model unloaded (idle)")
        return True
    return False


def loaded() -> bool:
    return _model is not None


def passage_text(row) -> str:
    parts = []
    if row["sender_name"]:
        parts.append(f"{row['sender_name']}:")
    if row["text"]:
        parts.append(row["text"])
    if row["derived"]:
        parts.append(row["derived"])
    return " ".join(parts)[:2000]


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
