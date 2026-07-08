import json
import logging
import os

import asyncpg

logger = logging.getLogger("uvicorn")

pool: asyncpg.Pool | None = None

CREATE_QUERIES_TABLE = """
    CREATE TABLE IF NOT EXISTS queries (
        id SERIAL PRIMARY KEY,
        question TEXT NOT NULL,
        answer TEXT,
        status TEXT NOT NULL CHECK (status IN ('success', 'max_iterations', 'error')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        iterations INTEGER,
        tool_calls INTEGER,
        duration_ms INTEGER,
        tools_used JSONB
    )
"""


async def init_pool() -> None:
    """Create the connection pool and ensure the schema exists. Called once
    from the app's lifespan on startup."""
    global pool
    pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"])
    logger.info("Postgres connection pool created")

    await pool.execute(CREATE_QUERIES_TABLE)
    logger.info("Ensured queries table exists")


async def close_pool() -> None:
    """Close the connection pool. Called once from the app's lifespan on
    shutdown."""
    global pool
    if pool is not None:
        await pool.close()
        logger.info("Postgres connection pool closed")


async def save_query(
    question: str,
    answer: str | None,
    status: str,
    iterations: int,
    tool_calls: int,
    duration_ms: int,
    tools_used: list[str],
) -> None:
    """Insert a record of one /ask exchange into the queries table.

    Deliberately swallows and logs any database error instead of raising —
    a logging/persistence failure here shouldn't turn an otherwise-successful
    /ask response into a 500 for the caller.
    """
    if pool is None:
        logger.error("save_query called but pool is not initialized — skipping insert")
        return

    try:
        await pool.execute(
            """
            INSERT INTO queries
                (question, answer, status, iterations, tool_calls, duration_ms, tools_used)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7)
            """,
            question,
            answer,
            status,
            iterations,
            tool_calls,
            duration_ms,
            json.dumps(tools_used),
        )
    except Exception as e:
        logger.error(f"save_query failed to insert record: {e}")


async def check_connection() -> None:
    """Run a trivial query to confirm the database is actually reachable.
    Raises if the pool isn't initialized or the query fails; callers (the
    /health route) are expected to catch and translate that into a 503."""
    if pool is None:
        raise RuntimeError("connection pool is not initialized")
    await pool.fetchval("SELECT 1")


async def fetch_history(limit: int, offset: int) -> list[asyncpg.Record]:
    """Return past /ask exchanges, most recent first."""
    if pool is None:
        logger.error("fetch_history called but pool is not initialized")
        return []

    return await pool.fetch(
        """
        SELECT id, question, answer, status, created_at, iterations,
               tool_calls, duration_ms, tools_used
        FROM queries
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
