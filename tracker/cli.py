"""Command line entry point.

Pipeline stages land here as the phases are built. Each stage is idempotent:
re-running it on the same input must not create duplicate rows.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime

import click

from tracker import __version__, db, migrate


@click.group()
@click.version_option(__version__, prog_name="tracker")
def cli() -> None:
    """Partner lateral movement intelligence pipeline."""


@cli.command("doctor")
def doctor_cmd() -> None:
    """Check whether the pipeline can run, and say what is missing."""
    from tracker.preflight import blockers, run_checks

    checks = run_checks()
    symbol = {"ok": "  ok  ", "optional": " note ", "blocker": " BLOCK"}
    for check in checks:
        click.echo(f"{symbol[check.level]}  {check.message}")
        if check.remedy and check.level != "ok":
            click.echo(f"          -> {check.remedy}")

    failed = blockers(checks)
    click.echo("")
    if failed:
        click.echo(f"{len(failed)} blocker(s). See docs/SETUP.md.")
        raise SystemExit(1)
    click.echo("Ready. Next: tracker db migrate && tracker firms --sync && tracker sources sync")


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


@cli.command("taxonomy")
@click.option("--load", "do_load", is_flag=True, help="Load taxonomy/*.yaml into the database.")
def taxonomy_cmd(do_load: bool) -> None:
    """Show or load the practice group and sector taxonomy."""
    from tracker.taxonomy import Taxonomy

    tax = Taxonomy.load()
    if not do_load:
        click.echo(f"taxonomy {tax.version}: {len(tax.practice_groups)} practice "
                   f"groups, {len(tax.sectors)} sectors, {len(tax._mappings)} mappings")
        return

    from tracker.pipeline import ingest as ingest_stage

    with db.connect(direct=True) as conn:
        try:
            version, groups, sectors = ingest_stage.sync_taxonomy(conn)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"taxonomy {version} loaded: {groups} practice groups, {sectors} sectors")


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


@cli.command("collect")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), required=True,
              help="How far back to walk the archives.")
@click.option("--out", type=click.Path(dir_okay=False), default="collected.json",
              show_default=True, help="Where to write the results.")
@click.option("--source", "only", default=None, help="Limit to one source slug.")
@click.option("--fetch-articles", is_flag=True,
              help="Read article bodies for sources whose terms have been reviewed. "
                   "Slow (10s per request) but the only way to reach headlines "
                   "that name nobody.")
@click.option("--max-articles", type=int, default=None,
              help="Stop fetching bodies after this many. The run still completes.")
@click.option("--cache", type=click.Path(dir_okay=False), default="article_cache.json",
              show_default=True,
              help="Article bodies fetched so far, so an interrupted run resumes.")
@extractor_option
def collect_cmd(since, out: str, only: str | None, fetch_articles: bool,
                max_articles: int | None, cache: str, extractor: str) -> None:
    """Run the whole free pipeline with no database, and write the results to a file.

    Fetch, gate and extract, exactly as a real backfill would, but holding
    everything in memory and reporting what it found. Useful before committing
    to a database or to LLM spend: it shows the real yield of a window rather
    than an estimate of it.

    Stores the same fields the database would: URL, headline, date, outlet and
    the extracted record. No article text.
    """
    import json
    from collections import Counter
    from dataclasses import replace
    from datetime import UTC

    from tracker.gate import evaluate
    from tracker.net.client import PoliteClient, RobotsDisallowed
    from tracker.sources import article, registry

    since = since.replace(tzinfo=UTC)
    engine, description = _build_extractor(extractor)
    click.echo(f"collecting since {since:%Y-%m-%d} with {description}")
    click.echo("")

    per_source: dict[str, dict] = {}
    records: list[dict] = []
    cost = 0.0

    # Bodies are cached on disk so an interrupted run does not re-fetch what it
    # already has. This is the no-database stand-in for raw_items.
    cache_path = pathlib.Path(cache)
    bodies: dict[str, str] = {}
    if cache_path.exists():
        bodies = json.loads(cache_path.read_text(encoding="utf-8"))
        click.echo(f"  {len(bodies)} article bodies already cached")
    fetched_now = 0

    with PoliteClient() as client:
        for source in registry.load():
            cfg = source.config
            if not source.collectable or (only and cfg.slug != only):
                continue

            click.echo(f"  {cfg.slug} ... ", nl=False)
            try:
                items = list(
                    registry.build_adapter(source, client).fetch(since=since)
                )
            except Exception as exc:  # noqa: BLE001 - one source must not end the run
                click.echo(f"FAILED ({type(exc).__name__}: {exc})")
                per_source[cfg.slug] = {"error": str(exc)[:200]}
                continue

            passed = 0
            found = 0
            for item in items:
                decision = evaluate(
                    item.headline, item.body_text,
                    reliability_tier=cfg.reliability_tier,
                )
                if not decision.passed:
                    continue
                passed += 1

                # Read the article body where the source allows it and the
                # headline gave us nothing to work with.
                if (
                    fetch_articles
                    and cfg.html_access_reviewed_at
                    and not item.body_text
                ):
                    if item.url in bodies:
                        item = replace(item, body_text=bodies[item.url] or None)
                    elif max_articles is None or fetched_now < max_articles:
                        body = None
                        try:
                            page = client.fetch(item.url)
                            body = article.body_of(page.text)
                        except RobotsDisallowed:
                            body = None  # robots said no; that is a valid answer
                        except Exception as exc:  # noqa: BLE001
                            click.echo(f"    ! {item.url[:70]}: {exc}", err=True)
                        bodies[item.url] = body or ""
                        fetched_now += 1
                        if fetched_now % 10 == 0:
                            cache_path.write_text(
                                json.dumps(bodies, ensure_ascii=False), encoding="utf-8"
                            )
                            click.echo(f"[{fetched_now}]", nl=False)
                        item = replace(item, body_text=body)

                result = engine.extract(item, reliability_tier=cfg.reliability_tier)
                cost += result.cost_usd
                for move in result.moves:
                    found += 1
                    records.append({
                        "source": cfg.slug,
                        "tier": cfg.reliability_tier,
                        "url": item.url,
                        "headline": item.headline,
                        "headline_is_derived": item.headline_is_derived,
                        "published_at": item.published_at.date().isoformat(),
                        "date_estimated": item.published_at_is_estimated,
                        "person": move.value("person_name"),
                        "to_firm": move.value("to_firm"),
                        "from_firm": move.value("from_firm"),
                        "title_to": move.value("title_to"),
                        "practice": move.value("practice_text"),
                        "move_type": move.value("move_type"),
                        "confidence": move.confidence.total,
                        "needs_review": move.needs_review,
                        "extractor": result.model,
                    })
            per_source[cfg.slug] = {
                "fetched": len(items), "gate_passed": passed, "records": found,
            }
            click.echo(f"{len(items)} fetched, {passed} passed the gate, {found} records")

    if fetch_articles:
        cache_path.write_text(json.dumps(bodies, ensure_ascii=False), encoding="utf-8")
        click.echo("")
        click.echo(f"  {fetched_now} article(s) fetched this run, "
                   f"{len(bodies)} cached in {cache}")

    payload = {
        "since": since.date().isoformat(),
        "extractor": description,
        "cost_usd": round(cost, 4),
        "per_source": per_source,
        "records": records,
    }
    pathlib.Path(out).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    fetched = sum(v.get("fetched", 0) for v in per_source.values())
    passed = sum(v.get("gate_passed", 0) for v in per_source.values())
    click.echo("")
    click.echo(
        f"{fetched:,} items fetched, {passed:,} passed the gate, "
        f"{len(records):,} records extracted, cost ${cost:.4f}"
    )
    if records:
        years = Counter(r["published_at"][:4] for r in records)
        click.echo("records by year: " + ", ".join(
            f"{y} {n}" for y, n in sorted(years.items())
        ))
        firms = Counter(r["to_firm"] for r in records)
        click.echo("top destinations: " + ", ".join(
            f"{f} ({n})" for f, n in firms.most_common(6)
        ))
    click.echo(f"written to {out}")


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


@cli.command("reextract")
@click.option("--source", "slug", default=None,
              help="Limit to one source slug. Omit to cover every source.")
@click.option("--apply", "do_apply", is_flag=True,
              help="Actually delete. Without this, only report what would go.")
@click.option("--reason", required=True,
              help="Why these records are being discarded. Recorded in the run.")
def reextract_cmd(slug: str | None, do_apply: bool, reason: str) -> None:
    """Discard extracted moves and mark their items for extraction again.

    Needed when a defect in the extractor means the stored records are wrong
    rather than merely incomplete — a wrong record cannot be repaired in place,
    because every field on it was derived from the same bad parse.

    Deliberately not part of `extract`. Re-extraction throws away human-visible
    output and re-fetches article bodies over the network, so it is an explicit
    decision with a stated reason, and it reports before it deletes.
    """
    where, params = "", []
    if slug:
        where = "AND s.slug = %s"
        params = [slug]

    with db.connect(direct=True) as conn:
        affected = conn.execute(
            f"""
            SELECT s.slug, count(DISTINCT m.id) AS moves,
                   count(DISTINCT ri.id) AS items
            FROM raw_items ri
            JOIN sources s ON s.id = ri.source_id
            LEFT JOIN move_sources ms ON ms.raw_item_id = ri.id
            LEFT JOIN moves m ON m.id = ms.move_id
            WHERE ri.processing_state <> 'new' AND ri.gate_passed {where}
            GROUP BY s.slug ORDER BY moves DESC
            """,
            params,
        ).fetchall()

        total_moves = sum(r["moves"] for r in affected)
        total_items = sum(r["items"] for r in affected)
        for r in affected:
            click.echo(f"  {r['slug']:38} {r['moves']:5} moves  {r['items']:6} items")
        click.echo(f"  {'TOTAL':38} {total_moves:5} moves  {total_items:6} items")

        if not do_apply:
            click.echo("")
            click.echo("dry run. Re-run with --apply to discard these records.")
            return

        deleted = conn.execute(
            f"""
            DELETE FROM moves m USING move_sources ms, raw_items ri, sources s
            WHERE ms.move_id = m.id AND ri.id = ms.raw_item_id
              AND s.id = ri.source_id {where}
            """,
            params,
        ).rowcount
        reset = conn.execute(
            f"""
            UPDATE raw_items ri SET processing_state = 'new', reject_reason = NULL,
                   error_message = NULL, processed_at = NULL, body_read_at = NULL
            FROM sources s
            WHERE s.id = ri.source_id AND ri.processing_state <> 'new'
              -- Only items extraction looked at. A gate rejection is the
              -- gate's decision and its reason must survive a re-extraction.
              AND ri.gate_passed {where}
            """,
            params,
        ).rowcount
        conn.commit()

    click.echo("")
    click.echo(f"discarded {deleted} move(s); {reset} item(s) marked for extraction again")
    click.echo(f"reason: {reason}")


@cli.group("cache")
def cache_group() -> None:
    """The local article cache. See docs/constraints.md section 2."""


@cache_group.command("status")
def cache_status() -> None:
    from tracker.pipeline.extract import ARTICLE_CACHE, load_article_cache

    entries = load_article_cache()
    size = ARTICLE_CACHE.stat().st_size if ARTICLE_CACHE.exists() else 0
    click.echo(f"{ARTICLE_CACHE}: {len(entries)} article(s), {size / 1024:.0f} KiB")


@cache_group.command("purge")
@click.confirmation_option(
    prompt="Delete the local article cache? The next extraction re-fetches pages."
)
def cache_purge() -> None:
    """Delete the cache. Do this when a re-extraction campaign is finished.

    The cache is a working file for one campaign, not a corpus; the retention
    rule in docs/constraints.md is that it does not outlive the campaign.
    """
    from tracker.pipeline.extract import ARTICLE_CACHE

    if not ARTICLE_CACHE.exists():
        click.echo("no cache to purge")
        return
    entries = len(__import__("json").loads(ARTICLE_CACHE.read_text(encoding="utf-8")) or {})
    ARTICLE_CACHE.unlink()
    click.echo(f"purged {entries} cached article(s)")


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
