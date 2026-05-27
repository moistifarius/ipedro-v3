"""Apply database schema at startup.

The schema is idempotent (all CREATE statements use IF NOT EXISTS). The
embedding dimension is rewritten on the fly to match the configured model.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from ipedro.db.pool import Database

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _load_schema_sql(embedding_dim: int) -> str:
    try:
        sql = resources.files("ipedro.db").joinpath("schema.sql").read_text()
    except Exception:
        # Fallback for environments where the package is run from source.
        sql = Path(__file__).with_name("schema.sql").read_text()
    return sql.replace("vector(1536)", f"vector({embedding_dim})")


async def apply_schema(db: Database, embedding_dim: int = 1536) -> None:
    sql = _load_schema_sql(embedding_dim)
    has_vector = True
    try:
        await db.execute(sql)
    except Exception as exc:
        # Most common cause: pgvector extension is not installed. Retry without it.
        log.warning("Full schema failed (%s); retrying without vector extension.", exc)
        without_vector = "\n".join(
            line for line in sql.splitlines()
            if "EXTENSION IF NOT EXISTS vector" not in line
        )
        without_vector = without_vector.replace(f"vector({embedding_dim})", "TEXT")
        without_vector = without_vector.replace(
            "CREATE INDEX IF NOT EXISTS embeddings_vec_idx\n"
            "    ON embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);",
            "-- pgvector unavailable: semantic index disabled",
        )
        await db.execute(without_vector)
        has_vector = False

    await db.execute(
        "INSERT INTO schema_version (version) VALUES ($1) "
        "ON CONFLICT (version) DO NOTHING",
        SCHEMA_VERSION,
    )
    if has_vector:
        log.info("Schema v%s applied (pgvector enabled).", SCHEMA_VERSION)
    else:
        log.warning(
            "Schema v%s applied WITHOUT pgvector. Semantic memory disabled.",
            SCHEMA_VERSION,
        )


async def has_pgvector(db: Database) -> bool:
    val = await db.fetchval(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
    )
    return val is not None
