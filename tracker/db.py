"""Database connection helpers.

Thin wrapper over psycopg. There is no ORM and no model layer: the schema is
defined by the SQL in migrations/ and nothing else infers it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from tracker.config import Config


@contextmanager
def connect(*, direct: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection.

    direct=True selects the non-pooled URL, which migrations need because
    pooled connections on Neon/Supabase do not support session-level DDL
    reliably.
    """
    cfg = Config.load()
    dsn = cfg.database_url_direct if direct else cfg.database_url
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=autocommit) as conn:
        yield conn
