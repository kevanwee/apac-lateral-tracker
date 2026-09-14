"""Database connection helpers.

Thin wrapper over psycopg. There is no ORM and no model layer: the schema is
defined by the SQL in migrations/ and nothing else infers it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from tracker.config import Config, ConfigError


@contextmanager
def connect(*, direct: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection.

    direct=True selects the non-pooled URL, which migrations need because
    pooled connections on Neon/Supabase do not support session-level DDL
    reliably.
    """
    cfg = Config.load()
    dsn = cfg.database_url_direct if direct else cfg.database_url
    if not dsn:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
            "or set it in the deployment environment."
        )
    # A long stage holds this connection open across minutes of HTTP fetching
    # at the 10s politeness floor — the archive walk is 33 requests before it
    # touches the database again. Pooled Postgres closes an idle connection
    # well inside that, so TCP keepalives are not optional here.
    with psycopg.connect(
        dsn,
        row_factory=dict_row,
        autocommit=autocommit,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    ) as conn:
        yield conn
