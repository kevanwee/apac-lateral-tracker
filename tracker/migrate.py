"""Forward-only SQL migration runner.

Migrations are plain .sql files in migrations/, named NNNN_description.sql and
applied in filename order. Each runs in its own transaction: a failing migration
leaves the database on the last good version rather than half-applied.

Applied migrations are checksummed. Editing a file that has already been applied
is an error, because it means the database and the repo disagree about what the
schema is.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text         NOT NULL,
    applied_at  timestamptz  NOT NULL DEFAULT now(),
    duration_ms integer      NOT NULL
);
COMMENT ON TABLE schema_migrations IS
  'Applied migrations. Managed by tracker.migrate; do not edit by hand.';
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read migrations off disk, ordered, rejecting malformed names and collisions."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"{path.name} does not match NNNN_lower_snake_case.sql")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                # Normalise line endings so a Windows checkout and a Linux CI
                # runner agree on the checksum.
                sql=path.read_text(encoding="utf-8").replace("\r\n", "\n"),
            )
        )

    seen: dict[int, str] = {}
    for m in migrations:
        if m.version in seen:
            raise MigrationError(
                f"duplicate migration version {m.version:04d}: {seen[m.version]} and {m.name}"
            )
        seen[m.version] = m.name
    return migrations


def applied(conn: psycopg.Connection) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        return {row["version"]: row for row in cur.fetchall()}


def _verify_unchanged(migrations: list[Migration], done: dict[int, dict]) -> None:
    for m in migrations:
        record = done.get(m.version)
        if record and record["checksum"] != m.checksum:
            raise MigrationError(
                f"{m.label} has changed since it was applied. Migrations are immutable "
                f"once applied; add a new migration instead of editing this one."
            )


def status(conn: psycopg.Connection) -> tuple[list[Migration], list[Migration]]:
    """Return (applied, pending), verifying that applied files are unchanged."""
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
    conn.commit()

    migrations = discover()
    done = applied(conn)
    _verify_unchanged(migrations, done)
    return (
        [m for m in migrations if m.version in done],
        [m for m in migrations if m.version not in done],
    )


def upgrade(conn: psycopg.Connection, *, target: int | None = None) -> list[Migration]:
    """Apply pending migrations in order. Returns what was applied."""
    _, pending = status(conn)
    if target is not None:
        pending = [m for m in pending if m.version <= target]

    run: list[Migration] = []
    for m in pending:
        with conn.cursor() as cur:
            cur.execute("SELECT clock_timestamp() AS t")
            started = cur.fetchone()["t"]
            cur.execute(m.sql)
            cur.execute("SELECT clock_timestamp() AS t")
            duration_ms = int((cur.fetchone()["t"] - started).total_seconds() * 1000)
            cur.execute(
                "INSERT INTO schema_migrations (version, name, checksum, duration_ms) "
                "VALUES (%s, %s, %s, %s)",
                (m.version, m.name, m.checksum, duration_ms),
            )
        conn.commit()
        run.append(m)
    return run
