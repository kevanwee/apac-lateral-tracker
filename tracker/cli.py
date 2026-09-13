"""Command line entry point.

Pipeline stages land here as the phases are built. Each stage is idempotent:
re-running it on the same input must not create duplicate rows.
"""

from __future__ import annotations

import sys

import click

from tracker import __version__, db, migrate


@click.group()
@click.version_option(__version__, prog_name="tracker")
def cli() -> None:
    """Partner lateral movement intelligence pipeline."""


@cli.group("db")
def db_group() -> None:
    """Schema management."""


@db_group.command("migrate")
@click.option("--target", type=int, default=None, help="Stop at this migration version.")
@click.option("--dry-run", is_flag=True, help="Show what would be applied and exit.")
def db_migrate(target: int | None, dry_run: bool) -> None:
    """Apply pending migrations."""
    with db.connect(direct=True) as conn:
        try:
            _, pending = migrate.status(conn)
        except migrate.MigrationError as exc:
            raise click.ClickException(str(exc)) from exc

        if target is not None:
            pending = [m for m in pending if m.version <= target]
        if not pending:
            click.echo("Schema is up to date.")
            return

        if dry_run:
            click.echo(f"{len(pending)} migration(s) would be applied:")
            for m in pending:
                click.echo(f"  {m.label}")
            return

        for m in migrate.upgrade(conn, target=target):
            click.echo(f"applied {m.label}")


@db_group.command("status")
def db_status() -> None:
    """Show applied and pending migrations."""
    with db.connect(direct=True) as conn:
        try:
            done, pending = migrate.status(conn)
        except migrate.MigrationError as exc:
            raise click.ClickException(str(exc)) from exc

    for m in done:
        click.echo(f"  applied  {m.label}")
    for m in pending:
        click.echo(f"  pending  {m.label}")
    if not done and not pending:
        click.echo("No migrations found.")


@db_group.command("erase-person")
@click.argument("person_id")
@click.option("--reason", required=True, help="Why this erasure was requested.")
@click.option("--actor", required=True, help="Who authorised it.")
@click.confirmation_option(prompt="Permanently erase this person and all their moves?")
def db_erase_person(person_id: str, reason: str, actor: str) -> None:
    """Erase a person and every record derived from them (Phase 0, section 4)."""
    with db.connect(direct=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM erase_person(%s, %s, %s)", (person_id, reason, actor))
        result = cur.fetchone()
        conn.commit()
    if result is None:
        raise click.ClickException(f"No person with id {person_id}.")
    click.echo(
        f"Erased person {person_id}: {result['moves_deleted']} move(s), "
        f"{result['evidence_deleted']} evidence row(s). "
        f"Name hash added to the ingestion suppression list."
    )


def main() -> int:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        return 1
    except click.Abort:
        click.echo("aborted", err=True)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
