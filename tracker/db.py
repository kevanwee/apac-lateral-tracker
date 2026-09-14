"""Database connection helpers.

Thin wrapper over psycopg. There is no ORM and no model layer: the schema is
defined by the SQL in migrations/ and nothing else infers it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from tracker.config import Config, ConfigError

log = logging.getLogger(__name__)


def _dsn_for(direct: bool) -> str:
    cfg = Config.load()
    dsn = cfg.database_url_direct if direct else cfg.database_url
    if not dsn:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
            "or set it in the deployment environment."
        )
    return dsn


_KEEPALIVE = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


def live(conn: psycopg.Connection, *, direct: bool = False) -> psycopg.Connection:
    """Return a usable connection, reopening if the server dropped this one.

    The fetch phases of ingest and backfill run for minutes at the 10s
    politeness floor without touching the database — the archive walk is 33
    requests before the first write. Pooled Postgres closes an idle connection
    well inside that, and TCP keepalives do not help because the pooler drops
    it at the application layer.

    Rather than hold a connection across the fetch, callers fetch first and
    then call this before writing. Nothing is lost on a reconnect: every stage
    is idempotent and commits per source.
    """
    if not conn.closed:
        try:
            conn.execute("SELECT 1")
            return conn
        except psycopg.Error:
            pass  # dropped under us; fall through and reopen

    log.info("database connection was dropped during a fetch; reopening")
    return psycopg.connect(_dsn_for(direct), row_factory=dict_row, **_KEEPALIVE)


@contextmanager
def connect(*, direct: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection.

    direct=True selects the non-pooled URL, which migrations need because
    pooled connections on Neon/Supabase do not support session-level DDL
    reliably.
    """
    with psycopg.connect(
        _dsn_for(direct), row_factory=dict_row, autocommit=autocommit, **_KEEPALIVE
    ) as conn:
        yield conn
