import json
import logging
from pathlib import Path

import asyncpg

from .config import settings

log = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


async def _init_conn(conn: asyncpg.Connection):
    await conn.set_type_codec("jsonb", encoder=_dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=_dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, init=_init_conn, min_size=2, max_size=10)
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            log.info("applying migration %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations(version) VALUES($1)", path.name)
