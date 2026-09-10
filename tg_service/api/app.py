"""HTTP API. Read-only over the database plus job creation. The MCP server sits on top of this."""
import asyncio
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .. import outbox, repo
from ..config import settings
from ..db import get_pool


def _auth(authorization: str | None = Header(default=None)):
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(401, "bad token")


def _rows(rows):
    return [dict(r) for r in rows]


class SyncRequest(BaseModel):
    chat_id: int
    from_date: datetime | None = None
    to_date: datetime | None = None
    min_id: int | None = None
    max_id: int | None = None
    max_messages: int | None = None
    requested_by: str | None = "api"


class TrackRequest(BaseModel):
    is_tracked: bool


class MediaRequest(BaseModel):
    action: str = "auto"          # auto | download | transcribe | extract
    wait: int = 0                 # seconds to wait for the result (0 = return job immediately)
    force: bool = False           # redo even if the file / derived text already exists
    requested_by: str | None = "api"


class DraftRequest(BaseModel):
    chat_id: int
    text: str = ""
    action: str = "send"                      # send | edit | delete
    target_msg_id: int | None = None          # for edit / delete (owner's own message)
    attachments: list[str] | None = None      # local paths or "tg:<chat_id>:<msg_id>"
    schedule_at: str | None = None            # ISO 8601; naive = local time of the service machine
    reply_to_msg_id: int | None = None
    reason: str | None = None
    idempotency_key: str | None = None
    requested_by: str | None = "api"


class SendPolicyRequest(BaseModel):
    send_policy: str


class BacklogRequest(BaseModel):
    days: int = 30
    kinds: list[str] = ["voice", "video_note", "document", "photo"]
    limit: int = 5000


async def _media_state(conn, chat_id: int, msg_id: int) -> dict:
    f = await repo.get_media_file(conn, chat_id, msg_id)
    content = await repo.get_content(conn, chat_id, msg_id)
    jobs = await repo.media_jobs_for_message(conn, chat_id, msg_id)
    return {
        "file": dict(f) if f else None,
        "content": {c["kind"]: dict(c) for c in content},
        "jobs": _rows(jobs),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="tg-service", dependencies=[Depends(_auth)])

    @app.get("/health")
    async def health():
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT (SELECT count(*) FROM chats) AS chats, (SELECT count(*) FROM messages) AS messages, "
                "(SELECT max(date) FROM messages) AS newest_message"
            )
        return dict(row)

    # ---- chats ----
    @app.get("/chats")
    async def chats(
        q: str | None = None, type: str | None = None, tracked: bool | None = None,
        limit: int = Query(100, le=1000), offset: int = 0,
    ):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _rows(await repo.list_chats(conn, q=q, type_=type, tracked=tracked, limit=limit, offset=offset))

    @app.get("/chats/{chat_id}")
    async def chat(chat_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.get_chat(conn, chat_id)
        if row is None:
            raise HTTPException(404, "chat not found")
        return dict(row)

    @app.patch("/chats/{chat_id}/track")
    async def track(chat_id: int, body: TrackRequest):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.set_tracked(conn, chat_id, body.is_tracked)
        if row is None:
            raise HTTPException(404, "chat not found")
        return dict(row)

    @app.get("/users/{user_id}")
    async def user(user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.get_user(conn, user_id)
        if row is None:
            raise HTTPException(404, "user not found")
        return dict(row)

    # ---- messages ----
    @app.get("/chats/{chat_id}/messages")
    async def messages(
        chat_id: int,
        from_date: datetime | None = Query(None, alias="from"),
        to_date: datetime | None = Query(None, alias="to"),
        before_id: int | None = None, after_id: int | None = None,
        sender_id: int | None = None, topic_id: int | None = None,
        include_deleted: bool = False, order: str = Query("desc", pattern="^(asc|desc)$"),
        limit: int = Query(100, le=1000),
    ):
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await repo.list_messages(
                conn, chat_id, from_date=from_date, to_date=to_date, before_id=before_id, after_id=after_id,
                sender_id=sender_id, topic_id=topic_id, include_deleted=include_deleted, order=order, limit=limit,
            )
        return _rows(rows)

    @app.get("/chats/{chat_id}/messages/{msg_id}")
    async def message(chat_id: int, msg_id: int, raw: bool = False):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.get_message(conn, chat_id, msg_id)
            if row is None:
                raise HTTPException(404, "message not found")
            out = dict(row)
            out["edits"] = _rows(await repo.get_edits(conn, chat_id, msg_id))
            if raw:
                out["raw"] = await repo.get_raw(conn, chat_id, msg_id)
        return out

    @app.get("/chats/{chat_id}/messages/{msg_id}/context")
    async def context(chat_id: int, msg_id: int, before: int = Query(20, le=200), after: int = Query(20, le=200)):
        """The message, N neighbours each side, its reply chain, replies to it, and its album."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            target = await repo.get_message(conn, chat_id, msg_id)
            if target is None:
                raise HTTPException(404, "message not found")
            before_rows = await repo.list_messages(conn, chat_id, before_id=msg_id, limit=before, include_deleted=True)
            after_rows = await repo.list_messages(conn, chat_id, after_id=msg_id, limit=after, order="asc", include_deleted=True)
            chain, cur, seen = [], target, set()
            while cur["reply_to_msg_id"] and cur["reply_to_msg_id"] not in seen and len(chain) < 20:
                seen.add(cur["reply_to_msg_id"])
                parent = await repo.get_message(conn, chat_id, cur["reply_to_msg_id"])
                if parent is None:
                    break
                chain.append(dict(parent))
                cur = parent
            replies = await repo.get_replies(conn, chat_id, msg_id)
            album = await repo.get_album(conn, chat_id, target["grouped_id"]) if target["grouped_id"] else []
        return {
            "message": dict(target),
            "before": list(reversed(_rows(before_rows))),
            "after": _rows(after_rows),
            "reply_chain": chain,
            "replies": _rows(replies),
            "album": _rows(album),
        }

    @app.get("/semantic_search")
    async def semantic_search(
        q: str, chat_id: int | None = None,
        from_date: datetime | None = Query(None, alias="from"), to_date: datetime | None = Query(None, alias="to"),
        limit: int = Query(20, le=200),
    ):
        """Meaning-based search over messages, transcripts, document and image text (local embeddings)."""
        import httpx
        from ..media.embed import to_pgvector
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"http://127.0.0.1:{settings.media_worker_port}/embed", json={"texts": [q], "kind": "query"})
                r.raise_for_status()
            vec = to_pgvector(r.json()["vectors"][0])
        except Exception as e:
            raise HTTPException(503, f"embedding worker unavailable: {type(e).__name__}: {e}")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await repo.semantic_search(conn, vec, chat_id=chat_id, from_date=from_date, to_date=to_date, limit=limit)
        return _rows(rows)

    @app.get("/health/full")
    async def health_full():
        from ..health import run_checks
        checks = await run_checks()
        return {k: {"ok": ok, "detail": d} for k, (ok, d) in checks.items()}

    @app.get("/search")
    async def search(
        q: str, chat_id: int | None = None,
        from_date: datetime | None = Query(None, alias="from"), to_date: datetime | None = Query(None, alias="to"),
        sender_id: int | None = None, mode: str = Query("fts", pattern="^(fts|substring)$"),
        limit: int = Query(50, le=500), offset: int = 0,
    ):
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await repo.search_messages(
                conn, q, chat_id=chat_id, from_date=from_date, to_date=to_date, sender_id=sender_id,
                mode=mode, limit=limit, offset=offset,
            )
        return _rows(rows)

    # ---- backfill jobs ----
    @app.post("/sync", status_code=201)
    async def create_sync(body: SyncRequest):
        pool = await get_pool()
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM chats WHERE id = $1", body.chat_id) is None:
                raise HTTPException(404, "chat not found")
            row = await repo.create_job(conn, **body.model_dump())
        return dict(row)

    @app.get("/sync")
    async def list_sync(status: str | None = None, limit: int = Query(50, le=500)):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _rows(await repo.list_jobs(conn, status, limit))

    @app.get("/sync/{job_id}")
    async def get_sync(job_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.get_job(conn, job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        return dict(row)

    @app.post("/sync/{job_id}/cancel")
    async def cancel_sync(job_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.cancel_job(conn, job_id)
        if row is None:
            raise HTTPException(409, "job is not queued/running")
        return dict(row)

    # ---- media ----
    @app.get("/chats/{chat_id}/messages/{msg_id}/media")
    async def media_state(chat_id: int, msg_id: int):
        """Downloaded file, derived text (transcript / document) and recent jobs for a message."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM messages WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id) is None:
                raise HTTPException(404, "message not found")
            return await _media_state(conn, chat_id, msg_id)

    @app.post("/chats/{chat_id}/messages/{msg_id}/media")
    async def media_request(chat_id: int, msg_id: int, body: MediaRequest):
        """Queue download (+ transcribe/extract). action=auto picks the follow-up by media type.
        With wait>0 the call blocks until the derived text exists or the timeout passes."""
        if body.action not in ("auto", "download", "transcribe", "extract"):
            raise HTTPException(422, "bad action")
        pool = await get_pool()
        async with pool.acquire() as conn:
            msg = await conn.fetchrow(
                "SELECT media_type, media FROM messages WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id
            )
            if msg is None:
                raise HTTPException(404, "message not found")
            if not msg["media_type"]:
                raise HTTPException(409, "message has no media")
            then = None if body.action == "download" else body.action
            f = await repo.get_media_file(conn, chat_id, msg_id)
            if f and not body.force:
                have = {c["kind"] for c in await repo.get_content(conn, chat_id, msg_id)}
                want = {"transcribe": "transcript", "extract": "document"}.get(body.action)
                if body.action == "download" or (want and want in have) or (body.action == "auto" and have):
                    state = await _media_state(conn, chat_id, msg_id)
                    state.update(job=None, complete=True)
                    return state
            if f and then and then != "auto":
                job = await repo.enqueue_media_job(conn, chat_id, msg_id, then, requested_by=body.requested_by)
            else:
                job = await repo.enqueue_media_job(conn, chat_id, msg_id, "download", then=then,
                                                   requested_by=body.requested_by)
        want_kind = {"transcribe": "transcript", "extract": "document"}.get(body.action)
        deadline = asyncio.get_event_loop().time() + min(body.wait, 600)
        while True:
            async with pool.acquire() as conn:
                state = await _media_state(conn, chat_id, msg_id)
            active = [j for j in state["jobs"] if j["status"] in ("queued", "running")]
            done = (state["file"] is not None) and (
                want_kind in state["content"] if want_kind else
                (body.action == "download" or bool(state["content"]) or not active)
            )
            if done or not active or asyncio.get_event_loop().time() >= deadline:
                state["job"] = dict(job)
                state["complete"] = done
                return state
            await asyncio.sleep(1)

    @app.get("/media/jobs")
    async def media_jobs(status: str | None = None, limit: int = Query(50, le=500)):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _rows(await repo.list_media_jobs(conn, status, limit))

    @app.post("/media/backlog")
    async def media_backlog(body: BacklogRequest):
        """Queue automatic processing for older messages that have media but no derived text yet."""
        from ..media.policy import auto_action_for
        pool = await get_pool()
        since = datetime.now().astimezone() - __import__("datetime").timedelta(days=body.days)
        queued = 0
        async with pool.acquire() as conn:
            rows = await repo.media_candidates(conn, since=since, kinds=body.kinds,
                                               max_mb=settings.media_auto_extract_max_mb, limit=body.limit)
            types = {}
            for r in rows:
                ct = types.get(r["chat_id"])
                if ct is None:
                    ct = types[r["chat_id"]] = await repo.chat_type(conn, r["chat_id"])
                action = auto_action_for(r["media_type"], r["media"], ct)
                if action:
                    await repo.enqueue_media_job(conn, r["chat_id"], r["msg_id"], "download", then=action,
                                                 requested_by="backlog")
                    queued += 1
        return {"candidates": len(rows), "queued": queued}

    # ---- outbox (drafts; sending happens only after the owner confirms in Saved Messages) ----
    @app.post("/outbox", status_code=201)
    async def create_outbox(body: DraftRequest):
        try:
            return await outbox.create_draft(**body.model_dump())
        except outbox.DraftRejected as e:
            raise HTTPException(403, str(e))

    @app.get("/outbox")
    async def list_outbox(status: str | None = None, limit: int = Query(50, le=500)):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _rows(await repo.list_drafts(conn, status, limit))

    @app.get("/outbox/digest")
    async def outbox_digest(hours: int = 24):
        from ..digest import build_digest
        return {"text": await build_digest(hours)}

    @app.get("/outbox/{draft_id}")
    async def get_outbox(draft_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.get_draft(conn, draft_id)
        if row is None:
            raise HTTPException(404, "draft not found")
        return dict(row)

    @app.post("/outbox/{draft_id}/cancel")
    async def cancel_outbox(draft_id: int, requested_by: str | None = None):
        try:
            return await outbox.cancel_by_agent(draft_id, requested_by)
        except outbox.DraftRejected as e:
            raise HTTPException(409, str(e))

    @app.patch("/chats/{chat_id}/send_policy")
    async def send_policy(chat_id: int, body: SendPolicyRequest):
        if body.send_policy not in ("deny", "confirm"):
            raise HTTPException(422, "send_policy must be deny or confirm")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await repo.set_send_policy(conn, chat_id, body.send_policy)
        if row is None:
            raise HTTPException(404, "chat not found")
        return dict(row)

    return app
