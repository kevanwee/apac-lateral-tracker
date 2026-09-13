"""The migration runner itself."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from tracker import migrate


def test_every_migration_file_is_well_named():
    migrations = migrate.discover()
    assert migrations, "no migrations found"
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1)), (
        "migration versions must be contiguous from 0001"
    )


def test_migrations_apply_cleanly(migrated_db):
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        done, pending = migrate.status(conn)
    assert pending == []
    assert len(done) == len(migrate.discover())


def test_rerunning_migrate_is_a_no_op(migrated_db):
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        assert migrate.upgrade(conn) == []


def test_editing_an_applied_migration_is_rejected(migrated_db, monkeypatch):
    """The repo and the database cannot silently disagree about the schema."""
    real = migrate.discover()

    def tampered(*_args, **_kwargs):
        first = real[0]
        return [
            migrate.Migration(
                version=first.version,
                name=first.name,
                path=first.path,
                sql=first.sql + "\n-- edited after the fact\n",
            ),
            *real[1:],
        ]

    monkeypatch.setattr(migrate, "discover", tampered)
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        with pytest.raises(migrate.MigrationError, match="immutable"):
            migrate.status(conn)
