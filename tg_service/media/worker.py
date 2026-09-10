"""tg-media-worker: transcribe / describe / extract jobs plus the embedding loop.
Separate process from the Telethon service. Stays up; models are unloaded after MEDIA_MODEL_IDLE seconds
without work. Serves a tiny local HTTP API (query embeddings, health) on MEDIA_WORKER_PORT."""
import asyncio
import logging
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from .. import repo
from ..config import settings
from ..db import close_pool, get_pool
from . import embed
from .extract import extract
from .policy import doc_kind, is_image
from .transcribe import transcribe
from .vision import describe_image, describe_pdf_pages

log = logging.getLogger("tg_service.media")
ACTIONS = ("transcribe", "extract", "describe")
_last_work = time.time()
_whisper_used = 0.0


def _unload_whisper():
    try:
        from mlx_whisper.transcribe import ModelHolder
        if getattr(ModelHolder, "model", None) is not None:
            ModelHolder.model = None
            ModelHolder.model_path = None
            import mlx.core as mx
            (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
            log.info("whisper model unloaded (idle)")
    except Exception as e:
        log.debug("whisper unload: %s", e)


async def _run(job) -> None:
    global _whisper_used
    pool = await get_pool()
    async with pool.acquire() as conn:
        f = await repo.get_media_file(conn, job["chat_id"], job["msg_id"])
        msg = await conn.fetchrow(
            "SELECT media_type, media FROM messages WHERE chat_id = $1 AND msg_id = $2", job["chat_id"], job["msg_id"]
        )
    if f is None or not Path(f["path"]).exists():
        async with pool.acquire() as conn:
            await repo.enqueue_media_job(conn, job["chat_id"], job["msg_id"], "download", then=job["action"],
                                         requested_by=job["requested_by"])
        raise RuntimeError("file not downloaded yet, re-queued via download")
    path = Path(f["path"])
    media = (msg["media"] if msg else None) or {}
    media_type = msg["media_type"] if msg else None

    if job["action"] == "transcribe":
        t0 = time.time()
        text, meta = await asyncio.to_thread(transcribe, path)
        _whisper_used = time.time()
        log.info("transcribed %s/%s: %.0fs audio in %.1fs, lang=%s, %d chars", job["chat_id"], job["msg_id"],
                 meta.get("duration") or 0, time.time() - t0, meta.get("language"), len(text))
        async with pool.acquire() as conn:
            await repo.upsert_content(conn, job["chat_id"], job["msg_id"], "transcript", text,
                                      language=meta.get("language"), model=meta.get("model"),
                                      duration=meta.get("duration"), meta={"segments": meta.get("segments")})
            await repo.drop_embedding(conn, job["chat_id"], job["msg_id"])  # re-embed with the transcript

    elif job["action"] == "describe":
        if path.suffix.lower() == ".pdf":
            text, meta = await describe_pdf_pages(path)
            kind = "document"
        elif is_image(media_type, media) or path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            text, meta = await describe_image(path)
            kind = "image"
        else:
            raise RuntimeError(f"describe: not an image ({media_type}, {path.name})")
        log.info("described %s/%s: %d chars, cost $%s", job["chat_id"], job["msg_id"], len(text), meta.get("cost"))
        async with pool.acquire() as conn:
            await repo.upsert_content(conn, job["chat_id"], job["msg_id"], kind, text, model=meta.get("model"), meta=meta)
            await repo.drop_embedding(conn, job["chat_id"], job["msg_id"])

    else:  # extract
        kind = doc_kind(f["mime"] or media.get("mime"), media.get("name") or path.name)
        if kind is None and path.suffix.lower() == ".zip":
            kind = "zip"
        if kind is None:
            raise RuntimeError(f"unsupported document type: {f['mime']} {path.name}")
        text, meta = await asyncio.to_thread(extract, path, kind)
        if meta.get("scanned") and settings.openrouter_api_key:
            log.info("%s/%s: scanned PDF, sending %d page(s) to the vision model", job["chat_id"], job["msg_id"], settings.vision_pdf_pages)
            try:
                text, vmeta = await describe_pdf_pages(path)
                meta.update(vmeta, ocr="vision")
            except Exception as e:
                meta["ocr_error"] = str(e)
        log.info("extracted %s/%s (%s): %d chars", job["chat_id"], job["msg_id"], kind, len(text))
        async with pool.acquire() as conn:
            await repo.upsert_content(conn, job["chat_id"], job["msg_id"], "document", text, model=kind, meta=meta)
            await repo.drop_embedding(conn, job["chat_id"], job["msg_id"])


async def _embed_batch() -> int:
    if not settings.embed_enabled:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await repo.messages_to_embed(conn, include_channels=settings.embed_channels)
    if not rows:
        return 0
    texts = [embed.passage_text(r) for r in rows]
    vecs = await asyncio.to_thread(embed.encode, texts, "passage")
    async with pool.acquire() as conn:
        await repo.upsert_embeddings(conn, [(r["chat_id"], r["msg_id"], settings.embed_model, embed.to_pgvector(v))
                                            for r, v in zip(rows, vecs)])
    return len(rows)


async def job_loop():
    global _last_work
    pool = await get_pool()
    async with pool.acquire() as conn:
        await repo.reset_running_media_jobs(conn, ACTIONS)
    embedded_total = 0
    while True:
        async with pool.acquire() as conn:
            job = await repo.claim_media_job(conn, ACTIONS)
        if job is not None:
            _last_work = time.time()
            try:
                await _run(job)
                status, error = "done", None
            except Exception as e:
                log.warning("media job %s (%s) failed: %s", job["id"], job["action"], e)
                status, error = "failed", f"{type(e).__name__}: {e}"
            async with pool.acquire() as conn:
                await repo.finish_media_job(conn, job["id"], status, error)
            continue
        try:
            n = await _embed_batch()
        except Exception:
            log.exception("embedding batch failed")
            n = 0
            await asyncio.sleep(30)
        if n:
            _last_work = time.time()
            embedded_total += n
            if embedded_total % 2048 < n:
                log.info("embedded %d messages so far", embedded_total)
            continue
        # idle
        idle = time.time() - _last_work
        if idle > settings.media_model_idle:
            embed.unload_if_idle(settings.media_model_idle)
            if time.time() - _whisper_used > settings.media_model_idle:
                _unload_whisper()
        if settings.media_worker_idle_exit and idle > settings.media_worker_idle_exit:
            log.info("idle for %ss, exiting", settings.media_worker_idle_exit)
            return
        await asyncio.sleep(2)


# ---- local HTTP: query embeddings + health ----
class EmbedRequest(BaseModel):
    texts: list[str]
    kind: str = "query"


def create_worker_app() -> FastAPI:
    app = FastAPI(title="tg-media-worker")

    @app.get("/health")
    async def health():
        pool = await get_pool()
        async with pool.acquire() as conn:
            st = await repo.embedding_stats(conn)
        return {"ok": True, "embed_loaded": embed.loaded(), "embedded": st["embedded"], "eligible": st["eligible"],
                "idle_seconds": int(time.time() - _last_work)}

    @app.post("/embed")
    async def embed_texts(body: EmbedRequest):
        global _last_work
        _last_work = time.time()
        vecs = await asyncio.to_thread(embed.encode, body.texts, body.kind)
        return {"vectors": vecs, "model": settings.embed_model}

    return app


async def main():
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("media worker started (whisper=%s, vision=%s, embed=%s)", settings.whisper_model, settings.vision_model,
             settings.embed_model if settings.embed_enabled else "off")
    server = uvicorn.Server(uvicorn.Config(create_worker_app(), host="127.0.0.1", port=settings.media_worker_port, log_level="warning"))
    async def jobs_then_exit():
        await job_loop()
        server.should_exit = True

    jobs = asyncio.create_task(jobs_then_exit(), name="media_jobs")
    try:
        await server.serve()
    finally:
        jobs.cancel()
        await asyncio.gather(jobs, return_exceptions=True)
        await close_pool()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
