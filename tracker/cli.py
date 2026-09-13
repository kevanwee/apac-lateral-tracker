"""Command line entry point.

Pipeline stages land here as the phases are built. Each stage is idempotent:
re-running it on the same input must not create duplicate rows.
"""

from __future__ import annotations

import sys
from datetime import datetime

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


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


@cli.group("sources")
def sources_group() -> None:
    """Source register."""


@sources_group.command("sync")
def sources_sync() -> None:
    """Reconcile config/sources.yaml into the database."""
    from tracker.pipeline import ingest as ingest_stage

    with db.connect(direct=True) as conn:
        upserted, deactivated = ingest_stage.sync_sources(conn)
    click.echo(f"{upserted} source(s) synced, {deactivated} deactivated")


@cli.command("firms")
@click.option("--sync", "do_sync", is_flag=True, help="Seed firms and aliases from config.")
def firms_cmd(do_sync: bool) -> None:
    """Show or seed the firm gazetteer."""
    from tracker.firms import FirmGazetteer

    gazetteer = FirmGazetteer.load()
    if not do_sync:
        click.echo(f"{len(gazetteer)} firms, "
                   f"{sum(len(e.aliases) for e in gazetteer.entries)} aliases in config")
        return

    from tracker.pipeline import ingest as ingest_stage

    with db.connect(direct=True) as conn:
        firms, aliases = ingest_stage.sync_firms(conn)
    click.echo(f"seeded {firms} firm(s) and {aliases} alias(es)")


@sources_group.command("list")
def sources_list() -> None:
    """Show the register, including sources we are not collecting."""
    from tracker.sources import registry

    for source in registry.load():
        cfg = source.config
        if source.blocked_reason:
            state = "BLOCKED"
        elif not cfg.active:
            state = "off"
        else:
            state = "active"
        click.echo(f"  {state:8} t{cfg.reliability_tier}  {cfg.slug:22} {cfg.name}")
        if source.blocked_reason:
            click.echo(f"           {' '.join(source.blocked_reason.split())[:96]}")


@cli.command("ingest")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Backfill from this date. Rate limited and resumable.")
@click.option("--source", "only", default=None, help="Limit to one source slug.")
def ingest_cmd(since, only: str | None) -> None:
    """Fetch feeds and store item metadata."""
    from datetime import UTC

    from tracker.net.client import PoliteClient
    from tracker.pipeline import ingest as ingest_stage
    from tracker.pipeline import runs

    if since is not None:
        since = since.replace(tzinfo=UTC)

    with db.connect(direct=True) as conn, PoliteClient() as client:
        params = {"since": since.isoformat() if since else None, "source": only}
        with runs.record(conn, "ingest", params=params) as recorder:
            ingest_stage.ingest(conn, recorder, client=client, since=since, only=only)
        click.echo(
            f"fetched {recorder.items_fetched}, "
            f"gate rejected {recorder.items_gate_rejected}, "
            f"errors {recorder.errors}"
        )


def _build_extractor(kind: str):
    """rules = free and deterministic; llm = costs money; auto = rules first.

    Returns (extractor, description).
    """
    from tracker.config import Config
    from tracker.extract.extractor import CostLedger, Extractor
    from tracker.extract.rules import RuleExtractor
    from tracker.firms import FirmGazetteer

    if kind == "rules":
        return RuleExtractor(gazetteer=FirmGazetteer.load()), "rules (free)"

    cfg = Config.load()
    ledger = CostLedger(ceiling_usd=cfg.llm_cost_ceiling_usd_per_run)
    llm = Extractor(model=cfg.extraction_model, ledger=ledger)
    if kind == "llm":
        return llm, f"{cfg.extraction_model} (ceiling ${ledger.ceiling_usd:.2f})"

    from tracker.extract.rules import CascadingExtractor

    return (
        CascadingExtractor(
            rules=RuleExtractor(gazetteer=FirmGazetteer.load()), fallback=llm
        ),
        f"rules, falling back to {cfg.extraction_model}",
    )


extractor_option = click.option(
    "--extractor",
    type=click.Choice(["rules", "llm", "auto"]),
    default="rules",
    show_default=True,
    help="rules costs nothing and abstains often; llm costs money; "
         "auto tries rules first and only pays for what they miss.",
)


@cli.command("backfill")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), required=True,
              help="Walk archives back to this date.")
@click.option("--source", "only", default=None, help="Limit to one source slug.")
def backfill_cmd(since, only: str | None) -> None:
    """Load historical items from source archives.

    Rate limited and resumable: re-running the same command continues where an
    interrupted run stopped, because every insert is a no-op on a URL we
    already hold.
    """
    from datetime import UTC

    from tracker.net.client import PoliteClient
    from tracker.pipeline import ingest as ingest_stage
    from tracker.pipeline import runs

    since = since.replace(tzinfo=UTC)
    with db.connect(direct=True) as conn, PoliteClient() as client:
        with runs.record(
            conn, "backfill", params={"since": since.isoformat(), "source": only}
        ) as recorder:
            ingest_stage.backfill(
                conn, recorder, client=client, since=since, only=only
            )
        click.echo(
            f"fetched {recorder.items_fetched}, "
            f"gate rejected {recorder.items_gate_rejected}, "
            f"errors {recorder.errors}"
        )
        for row in conn.execute(
            "SELECT slug, backfilled_to FROM sources "
            "WHERE backfilled_to IS NOT NULL ORDER BY slug"
        ).fetchall():
            click.echo(f"  {row['slug']:32} back to {row['backfilled_to']}")


@cli.command("catch-up")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Override the window. Defaults to the last successful run.")
@click.option("--extract/--no-extract", "do_extract", default=True,
              help="Also run extraction over whatever the gate passed.")
@click.option("--limit", type=int, default=None, help="Cap items extracted this run.")
@extractor_option
def catch_up_cmd(since, do_extract: bool, limit: int | None, extractor: str) -> None:
    """Bring the record up to date. Run this every few months.

    Live feeds plus source archives, then extraction. Every stage is
    idempotent, so running it twice costs time and nothing else.
    """
    from datetime import UTC, timedelta

    from tracker.extract.extractor import CostCeilingExceeded
    from tracker.net.client import PoliteClient
    from tracker.pipeline import extract as extract_stage
    from tracker.pipeline import ingest as ingest_stage
    from tracker.pipeline import runs

    with db.connect(direct=True) as conn, PoliteClient() as client:
        if since is None:
            last = conn.execute(
                "SELECT max(started_at) AS t FROM pipeline_runs "
                "WHERE stage IN ('ingest', 'backfill') AND status = 'succeeded'"
            ).fetchone()["t"]
            # Overlap the window deliberately: outlets publish late and the
            # url constraint makes re-reading free.
            window = (last - timedelta(days=14)) if last else None
            since = window or (datetime.now(UTC) - timedelta(days=90))
        else:
            since = since.replace(tzinfo=UTC)
        click.echo(f"catching up since {since:%Y-%m-%d}")

        with runs.record(conn, "ingest", params={"since": since.isoformat()}) as rec:
            ingest_stage.ingest(conn, rec, client=client, since=since)
        click.echo(f"  live feeds: fetched {rec.items_fetched}, errors {rec.errors}")

        with runs.record(conn, "backfill", params={"since": since.isoformat()}) as rec:
            ingest_stage.backfill(conn, rec, client=client, since=since)
        click.echo(f"  archives:   fetched {rec.items_fetched}, errors {rec.errors}")

        if not do_extract:
            click.echo("  extraction skipped (--no-extract)")
            return

        engine, description = _build_extractor(extractor)
        click.echo(f"  extracting with {description}")
        with runs.record(
            conn, "extract", params={"limit": limit, "extractor": extractor}
        ) as rec:
            try:
                extract_stage.run(
                    conn, rec, extractor=engine, client=client, limit=limit
                )
            except CostCeilingExceeded as exc:
                rec.fail("extract", str(exc))
                raise click.ClickException(str(exc)) from exc
        click.echo(
            f"  extraction: {rec.moves_created} move(s), "
            f"{rec.moves_queued_for_review} to review, ${rec.llm_cost_usd:.4f}"
        )


@cli.command("extract")
@click.option("--limit", type=int, default=None, help="Stop after this many items.")
@extractor_option
def extract_cmd(limit: int | None, extractor: str) -> None:
    """Extract movement records from ingested items."""
    from tracker.extract.extractor import CostCeilingExceeded
    from tracker.net.client import PoliteClient
    from tracker.pipeline import extract as extract_stage
    from tracker.pipeline import runs

    engine, description = _build_extractor(extractor)
    click.echo(f"extracting with {description}")

    with db.connect(direct=True) as conn, PoliteClient() as client:
        with runs.record(
            conn, "extract", params={"limit": limit, "extractor": extractor}
        ) as recorder:
            try:
                extract_stage.run(
                    conn, recorder, extractor=engine, client=client, limit=limit
                )
            except CostCeilingExceeded as exc:
                # Fail loudly. Never silently process a truncated set.
                recorder.fail("extract", str(exc))
                raise click.ClickException(str(exc)) from exc
        click.echo(
            f"created {recorder.moves_created} move(s), "
            f"{recorder.moves_queued_for_review} queued for review, "
            f"spent ${recorder.llm_cost_usd:.4f}"
        )


@cli.command("status")
def status_cmd() -> None:
    """Recent runs, queue depth and any source that has gone quiet."""
    with db.connect() as conn:
        runs_ = conn.execute(
            "SELECT stage, status, started_at, items_fetched, moves_created, "
            "moves_queued_for_review, errors, llm_cost_usd FROM pipeline_runs "
            "ORDER BY started_at DESC LIMIT 8"
        ).fetchall()
        queue = conn.execute(
            "SELECT reason, count(*) AS n FROM review_queue "
            "WHERE resolved_at IS NULL GROUP BY reason ORDER BY n DESC"
        ).fetchall()
        alerts = conn.execute("SELECT * FROM source_yield_alerts").fetchall()
        violations = conn.execute(
            "SELECT check_name, count(*) AS n FROM invariant_violations "
            "GROUP BY check_name"
        ).fetchall()

    click.echo("recent runs")
    for r in runs_:
        click.echo(
            f"  {r['started_at']:%Y-%m-%d %H:%M}  {r['stage']:13} {r['status']:10} "
            f"fetched={r['items_fetched']:4} moves={r['moves_created']:3} "
            f"review={r['moves_queued_for_review']:3} errors={r['errors']} "
            f"${r['llm_cost_usd']}"
        )
    click.echo("")
    click.echo("open review queue")
    for q in queue:
        click.echo(f"  {q['n']:4}  {q['reason']}")
    if not queue:
        click.echo("  empty")
    if alerts:
        click.echo("")
        click.echo("sources with three or more consecutive empty runs")
        for a in alerts:
            click.echo(f"  {a['slug']}: {a['consecutive_zero_yield_runs']} runs")
    if violations:
        click.echo("")
        click.echo("INVARIANT VIOLATIONS")
        for v in violations:
            click.echo(f"  {v['n']:4}  {v['check_name']}")


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
