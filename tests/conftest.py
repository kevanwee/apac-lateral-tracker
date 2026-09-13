"""Test fixtures.

These tests run against a real Postgres, not a mock. An invariant that is only
asserted in Python is not an invariant, so the point of this suite is to prove
the database rejects what it is supposed to reject.

Locally:  docker run -e POSTGRES_PASSWORD=pg -p 5432:5432 postgres:16
          DATABASE_URL=postgresql://postgres:pg@localhost/postgres pytest
In CI:    a postgres service container, see .github/workflows/ci.yml
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

from tracker import migrate

# Load .env here rather than relying on some imported module having done it.
# Whether DATABASE_URL was visible used to depend on test collection order, so
# the guard below would sometimes skip instead of refusing — which is precisely
# how a production database got dropped.
load_dotenv(override=False)

TAXONOMY_VERSION = "0.0.1"


# Hosts a throwaway database is allowed to live on. Anything else has to opt
# in explicitly, because the session fixture below drops the schema.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "postgres", "db")


class DestructiveTestGuard(Exception):
    """Raised rather than dropping a schema that might not be disposable."""


def _is_disposable(dsn: str) -> bool:
    """Only local hosts are assumed safe to wipe."""
    host = (urlparse(dsn).hostname or "").lower()
    return host in _LOCAL_HOSTS


def _dsn() -> str:
    """The database these tests may destroy.

    Prefers TRACKER_TEST_DATABASE_URL. Falls back to DATABASE_URL **only** when
    it points somewhere local.

    This guard exists because it has already gone wrong: with a production
    DATABASE_URL in .env, running pytest dropped the live schema. The suite
    needs a disposable database, so it now refuses to guess which one that is.
    """
    test_dsn = os.environ.get("TRACKER_TEST_DATABASE_URL")
    if test_dsn:
        return test_dsn

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip(
            "No test database. Set TRACKER_TEST_DATABASE_URL, or run a local "
            "Postgres and point DATABASE_URL at it."
        )

    if _is_disposable(dsn) or os.environ.get("TRACKER_ALLOW_DESTRUCTIVE_TESTS") == "1":
        return dsn

    host = urlparse(dsn).hostname
    raise DestructiveTestGuard(
        f"Refusing to run destructive tests against {host!r}. "
        f"This suite drops and recreates the `public` schema, which would "
        f"erase everything in that database. "
        f"Set TRACKER_TEST_DATABASE_URL to a throwaway database, or "
        f"TRACKER_ALLOW_DESTRUCTIVE_TESTS=1 if you are certain."
    )


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Apply every migration to a clean schema once per session."""
    dsn = _dsn()
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        applied = migrate.upgrade(conn)
        assert applied, "no migrations were applied"
    return dsn


@pytest.fixture
def conn(migrated_db: str):
    """A connection whose transaction is rolled back after each test."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as c:
        yield c
        c.rollback()


def check_deferred(conn: psycopg.Connection) -> None:
    """Force deferred constraint triggers to fire without committing."""
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


# Tables holding data rather than schema. schema_migrations and
# instance_secrets are deliberately absent: wiping either breaks the session.
DATA_TABLES = [
    "llm_calls", "pipeline_run_sources", "pipeline_runs", "review_queue",
    "move_sectors", "move_practice_groups", "move_field_evidence",
    "move_sources", "moves", "team_moves", "people", "firm_aliases",
    "sources", "firms", "practice_groups", "sectors", "taxonomy_versions",
    "suppressed_people", "erasure_log", "raw_items",
]


@pytest.fixture
def clean_conn(migrated_db: str):
    """A committing connection over an empty dataset.

    Pipeline tests commit, so they cannot rely on the rollback isolation the
    `conn` fixture gives. They get a truncated database instead.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as c:
        c.execute(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE")
        c.commit()
        yield c
        c.rollback()


# ---------------------------------------------------------------------------
# Fixture builders — small helpers so each test states only what it is about.
# ---------------------------------------------------------------------------


def make_firm(conn, name: str | None = None, firm_type: str = "global", **kw) -> uuid.UUID:
    name = name or f"Firm {uuid.uuid4().hex[:8]}"
    row = conn.execute(
        "INSERT INTO firms (canonical_name, firm_type, hq_jurisdiction) "
        "VALUES (%s, %s, %s) RETURNING id",
        (name, firm_type, kw.get("hq_jurisdiction", "SG")),
    ).fetchone()
    return row["id"]


def make_person(conn, name: str = "Wei Ming Tan", surname: str = "tan") -> uuid.UUID:
    row = conn.execute(
        "INSERT INTO people (canonical_name, surname_normalised, name_variants) "
        "VALUES (%s, %s, %s) RETURNING id",
        (name, surname, []),
    ).fetchone()
    return row["id"]


def make_source(conn, **kw) -> uuid.UUID:
    row = conn.execute(
        """
        INSERT INTO sources (slug, name, base_url, feed_url, access_type,
                             reliability_tier, firm_id, jurisdiction_focus)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            kw.get("slug", f"src-{uuid.uuid4().hex[:8]}"),
            kw.get("name", "Test Outlet"),
            kw.get("base_url", "https://example.test"),
            kw.get("feed_url", "https://example.test/feed"),
            kw.get("access_type", "feed"),
            kw.get("reliability_tier", 2),
            kw.get("firm_id"),
            kw.get("jurisdiction_focus", ["SG"]),
        ),
    ).fetchone()
    return row["id"]


def make_raw_item(conn, source_id: uuid.UUID, **kw) -> uuid.UUID:
    row = conn.execute(
        """
        INSERT INTO raw_items (source_id, url, headline, published_at,
                               content_hash, source_access)
        VALUES (%s, %s, %s, now(), %s, %s) RETURNING id
        """,
        (
            source_id,
            kw.get("url", f"https://example.test/{uuid.uuid4().hex}"),
            kw.get("headline", "Partner joins firm"),
            b"\x00" * 32,
            kw.get("source_access", "summary"),
        ),
    ).fetchone()
    return row["id"]


def make_move(conn, **kw) -> uuid.UUID:
    person_id = kw.get("person_id") or make_person(conn)
    to_firm_id = kw["to_firm_id"] if "to_firm_id" in kw else make_firm(conn)
    row = conn.execute(
        """
        INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type,
                           announced_date, confidence, office_jurisdiction)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            person_id,
            kw.get("from_firm_id"),
            to_firm_id,
            kw.get("move_type", "lateral"),
            kw.get("announced_date", date(2025, 3, 1)),
            kw.get("confidence", "0.900"),
            kw.get("office_jurisdiction", "SG"),
        ),
    ).fetchone()
    return row["id"]


def seed_taxonomy(conn, version: str = TAXONOMY_VERSION) -> dict[str, uuid.UUID]:
    """A minimal two-axis taxonomy: enough to exercise the classification rules."""
    conn.execute(
        "INSERT INTO taxonomy_versions (version, checksum, is_current) "
        "VALUES (%s, %s, true) ON CONFLICT (version) DO NOTHING",
        (version, b"\x00" * 32),
    )
    nodes: dict[str, uuid.UUID] = {}
    for code, name, level, parent in [
        ("corporate", "Corporate", 1, None),
        ("corporate.m_and_a", "M&A", 2, "corporate"),
        ("finance", "Finance", 1, None),
        ("finance.banking", "Banking and Leveraged Finance", 2, "finance"),
        ("disputes", "Disputes", 1, None),
    ]:
        row = conn.execute(
            "INSERT INTO practice_groups (taxonomy_version, code, name, level, parent_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (version, code, name, level, nodes.get(parent)),
        ).fetchone()
        nodes[code] = row["id"]

    row = conn.execute(
        "INSERT INTO practice_groups "
        "(taxonomy_version, code, name, level, is_unclassified) "
        "VALUES (%s, 'unclassified', 'Unclassified', 1, true) RETURNING id",
        (version,),
    ).fetchone()
    nodes["unclassified"] = row["id"]
    return nodes


def classify(conn, move_id, pg_id, *, primary: bool, version: str = TAXONOMY_VERSION) -> None:
    conn.execute(
        """
        INSERT INTO move_practice_groups
            (move_id, practice_group_id, taxonomy_version, is_primary,
             confidence, assigned_by)
        VALUES (%s, %s, %s, %s, 0.900, 'model')
        """,
        (move_id, pg_id, version, primary),
    )


def mark_classified(conn, move_id, version: str = TAXONOMY_VERSION) -> None:
    conn.execute(
        "UPDATE moves SET classification_state = 'classified', "
        "classified_taxonomy_version = %s WHERE id = %s",
        (version, move_id),
    )
