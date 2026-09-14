"""The ingest stage: config/sources.yaml -> sources -> raw_items.

Idempotent by construction. Re-running over the same window inserts nothing:
`raw_items.url` is unique, and a conflicting insert is a no-op rather than an
error, so a backfill can be interrupted and resumed at any point.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg

from tracker import db
from tracker.firms import FirmGazetteer
from tracker.gate import GATE_VERSION, evaluate, evaluate_entity_slug
from tracker.geo import PlaceGazetteer
from tracker.net.client import PoliteClient, RobotsDisallowed
from tracker.pipeline.runs import RunRecorder
from tracker.sources import registry
from tracker.sources.base import RawItem
from tracker.sources.registry import BACKFILL_ADAPTERS, RegisteredSource

log = logging.getLogger(__name__)


def sync_firms(conn: psycopg.Connection) -> tuple[int, int]:
    """Seed firms and their aliases from config/firms.yaml.

    Worth doing before any extraction: a firm the database does not know gets
    created as `other` and its move routed to review, so an unseeded database
    sends effectively everything to a human. Seeding the gazetteer is what
    makes the review queue converge on the items that actually need judgement.
    """
    gazetteer = FirmGazetteer.load()
    firms = aliases = 0

    for entry in gazetteer.entries:
        row = conn.execute(
            """
            INSERT INTO firms (canonical_name, firm_type, hq_jurisdiction)
            VALUES (%s, %s, %s)
            ON CONFLICT ((lower(btrim(canonical_name)))) DO UPDATE SET
                firm_type = EXCLUDED.firm_type,
                hq_jurisdiction = coalesce(
                    EXCLUDED.hq_jurisdiction, firms.hq_jurisdiction
                )
            RETURNING id
            """,
            (entry.canonical_name, entry.firm_type, entry.hq_jurisdiction),
        ).fetchone()
        firms += 1

        for alias in entry.aliases:
            conn.execute(
                """
                INSERT INTO firm_aliases (firm_id, alias, alias_type, provenance)
                VALUES (%s, %s, %s, 'config/firms.yaml')
                ON CONFLICT (firm_id, (lower(btrim(alias)))) DO NOTHING
                """,
                (row["id"], alias, _alias_type(entry.canonical_name, alias)),
            )
            aliases += 1

    conn.commit()
    return firms, aliases


def _alias_type(canonical: str, alias: str) -> str:
    """Best guess at why an alias exists. Reviewers can correct it."""
    if len(alias) <= 5 and alias.isupper():
        return "abbreviation"
    if any(word in canonical for word in alias.split()[:1]):
        return "abbreviation"
    return "legacy_name"


def sync_sources(conn: psycopg.Connection) -> tuple[int, int]:
    """Reconcile the YAML register into the database. Returns (upserted, deactivated)."""
    registered = registry.load()
    slugs = [s.config.slug for s in registered]
    upserted = 0

    for source in registered:
        cfg = source.config
        firm_id = None
        if cfg.firm_name:
            firm_id = _ensure_firm(conn, cfg.firm_name, newsroom_url=cfg.base_url)

        conn.execute(
            """
            INSERT INTO sources
                (slug, name, base_url, feed_url, access_type, adapter,
                 reliability_tier, firm_id, jurisdiction_focus,
                 default_access_level, active, poll_interval, terms_url,
                 terms_note, html_access_reviewed_at, html_access_reviewed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    make_interval(hours => %s), %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                base_url = EXCLUDED.base_url,
                feed_url = EXCLUDED.feed_url,
                access_type = EXCLUDED.access_type,
                adapter = EXCLUDED.adapter,
                reliability_tier = EXCLUDED.reliability_tier,
                firm_id = EXCLUDED.firm_id,
                jurisdiction_focus = EXCLUDED.jurisdiction_focus,
                default_access_level = EXCLUDED.default_access_level,
                active = EXCLUDED.active,
                poll_interval = EXCLUDED.poll_interval,
                terms_url = EXCLUDED.terms_url,
                terms_note = EXCLUDED.terms_note,
                html_access_reviewed_at = EXCLUDED.html_access_reviewed_at,
                html_access_reviewed_by = EXCLUDED.html_access_reviewed_by
            """,
            (
                cfg.slug, cfg.name, cfg.base_url, cfg.feed_url, cfg.access_type,
                cfg.adapter, cfg.reliability_tier, firm_id,
                list(cfg.jurisdiction_focus),
                cfg.default_access_level,
                # A blocked source is not active however the YAML labels it.
                cfg.active and source.blocked_reason is None,
                cfg.poll_interval_hours, cfg.terms_url, cfg.terms_note,
                cfg.html_access_reviewed_at, cfg.html_access_reviewed_by,
            ),
        )
        upserted += 1

    # A source removed from the register is deactivated, never deleted: its
    # raw_items and the moves derived from them stay attributable.
    deactivated = conn.execute(
        "UPDATE sources SET active = false WHERE NOT (slug = ANY(%s)) AND active "
        "RETURNING id",
        (slugs,),
    ).rowcount
    conn.commit()
    return upserted, deactivated


def _ensure_firm(conn: psycopg.Connection, name: str, *, newsroom_url: str | None = None):
    row = conn.execute(
        "SELECT id FROM firms WHERE lower(btrim(canonical_name)) = lower(btrim(%s))",
        (name,),
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "INSERT INTO firms (canonical_name, firm_type, newsroom_url) "
        "VALUES (%s, 'regional', %s) RETURNING id",
        (name, newsroom_url),
    ).fetchone()
    return row["id"]


def due_sources(conn: psycopg.Connection, *, only: str | None = None) -> list[dict]:
    """Active sources whose poll interval has elapsed."""
    return conn.execute(
        """
        SELECT id, slug, reliability_tier, default_access_level
        FROM sources
        WHERE active
          -- Casts are required: Postgres cannot infer a bare parameter's type
          -- from `IS NULL` alone.
          AND (%s::text IS NULL OR slug = %s::text)
          AND (last_polled_at IS NULL OR last_polled_at + poll_interval <= now())
        ORDER BY last_polled_at NULLS FIRST
        """,
        (only, only),
    ).fetchall()


def backfill(
    conn: psycopg.Connection,
    recorder: RunRecorder,
    *,
    client: PoliteClient,
    since: datetime,
    only: str | None = None,
) -> None:
    """Walk archives back to `since`.

    Only adapters that can actually read an archive take part — running a live
    feed adapter here would just re-read the same recent window and report a
    misleadingly small yield.

    Resumable by construction: every item insert is a no-op on the url unique
    constraint, so an interrupted backfill is continued by re-running the same
    command. `sources.backfilled_to` records progress for reporting, not for
    correctness.
    """
    by_slug = {s.config.slug: s for s in registry.load()}

    rows = conn.execute(
        """
        SELECT id, slug, reliability_tier, default_access_level, backfilled_to
        FROM sources
        WHERE active AND (%s::text IS NULL OR slug = %s::text)
        ORDER BY reliability_tier, slug
        """,
        (only, only),
    ).fetchall()

    for row in rows:
        source = by_slug.get(row["slug"])
        if source is None or not source.collectable:
            continue
        if source.config.adapter not in BACKFILL_ADAPTERS:
            log.info(
                "%s: %s adapter reads a live window only, skipping in backfill",
                row["slug"], source.config.adapter,
            )
            continue

        conn.execute(
            "UPDATE sources SET backfill_started_at = coalesce(backfill_started_at, now()) "
            "WHERE id = %s",
            (row["id"],),
        )
        _ingest_one(conn, recorder, source, row, client=client, since=since)

        oldest = conn.execute(
            "SELECT min(published_at)::date AS d FROM raw_items WHERE source_id = %s",
            (row["id"],),
        ).fetchone()["d"]
        conn.execute(
            "UPDATE sources SET backfilled_to = %s, backfill_completed_at = now() "
            "WHERE id = %s",
            (oldest, row["id"]),
        )
        conn.commit()


def ingest(
    conn: psycopg.Connection,
    recorder: RunRecorder,
    *,
    client: PoliteClient,
    since: datetime | None = None,
    only: str | None = None,
) -> None:
    by_slug = {s.config.slug: s for s in registry.load()}
    rows = due_sources(conn, only=only)
    if only and not rows:
        log.warning("no active source due with slug %r", only)

    for row in rows:
        source = by_slug.get(row["slug"])
        if source is None or not source.collectable:
            recorder.source_yield(
                row["id"], fetched=0, new=0, gate_passed=0,
                skipped_reason="not collectable in the current register",
            )
            continue
        _ingest_one(conn, recorder, source, row, client=client, since=since)

    conn.commit()


def _ingest_one(
    conn: psycopg.Connection,
    recorder: RunRecorder,
    source: RegisteredSource,
    row: dict,
    *,
    client: PoliteClient,
    since: datetime | None,
) -> None:
    adapter = registry.build_adapter(source, client)
    fetched = new = gate_passed = 0

    try:
        items = list(adapter.fetch(since=since))
        fetched = len(items)
        # The fetch above can run for minutes at the 10s floor. Reclaim a live
        # connection before writing rather than discovering it died.
        conn = db.live(conn, direct=True)
        recorder.conn = conn
    except RobotsDisallowed as exc:
        # Politeness, not failure. Recorded so a zero yield caused by robots
        # is never mistaken for a broken feed.
        conn = db.live(conn, direct=True)
        recorder.conn = conn
        log.info("%s: %s", source.config.slug, exc)
        recorder.source_yield(
            row["id"], fetched=0, new=0, gate_passed=0, skipped_reason=str(exc)[:200]
        )
        return
    except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
        conn = db.live(conn, direct=True)
        recorder.conn = conn
        log.warning("%s: fetch failed: %s", source.config.slug, exc)
        recorder.fail(source.config.slug, f"{type(exc).__name__}: {exc}")
        recorder.source_yield(
            row["id"], fetched=0, new=0, gate_passed=0, error=f"{type(exc).__name__}: {exc}"
        )
        return

    # Some outlets slug entities rather than the headline, so the language
    # gate rejects every item. See gate.evaluate_entity_slug.
    entity_mode = source.config.options.get("gate_mode") == "slug_entities"
    gazetteers = (FirmGazetteer.load(), PlaceGazetteer.load()) if entity_mode else None

    for item in items:
        if entity_mode:
            decision = evaluate_entity_slug(
                item.headline, *gazetteers, reliability_tier=row["reliability_tier"]
            )
        else:
            decision = evaluate(
                item.headline, item.body_text, reliability_tier=row["reliability_tier"]
            )
        if decision.passed:
            gate_passed += 1
        if _insert_item(conn, row["id"], item, decision):
            new += 1

    conn.execute("UPDATE sources SET last_polled_at = now() WHERE id = %s", (row["id"],))
    recorder.items_fetched += fetched
    recorder.items_gate_rejected += fetched - gate_passed
    recorder.source_yield(
        row["id"], fetched=fetched, new=new, gate_passed=gate_passed
    )
    conn.commit()


def _insert_item(
    conn: psycopg.Connection, source_id, item: RawItem, decision
) -> bool:
    """Insert one item. Returns False if we already had it.

    Nothing here writes article text: headline, URL, dates, a content hash and
    the gate's decision. See docs/constraints.md section 2.
    """
    result = conn.execute(
        """
        INSERT INTO raw_items
            (source_id, url, headline, published_at, published_at_is_estimated,
             headline_is_derived, content_hash, source_access, processing_state,
             reject_reason, gate_version, gate_passed, gate_score,
             gate_matched_terms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO NOTHING
        RETURNING id
        """,
        (
            source_id,
            item.url,
            item.headline,
            item.published_at,
            item.published_at_is_estimated,
            item.headline_is_derived,
            item.content_hash,
            item.access_level,
            "new" if decision.passed else "rejected",
            None if decision.passed else f"relevance_gate: {decision.reason}",
            GATE_VERSION,
            decision.passed,
            round(decision.score, 3),
            list(decision.matched_terms),
        ),
    ).fetchone()
    return result is not None


def sync_taxonomy(conn: psycopg.Connection) -> tuple[str, int, int]:
    """Load taxonomy/*.yaml into the database. Returns (version, groups, sectors).

    A version is immutable once loaded: if the files have changed, the checksum
    will not match and the load is refused. Cut a new version instead — every
    historical assignment keeps the version that made it, which is what stops a
    retaxonomy silently rewriting past trend lines.
    """
    from tracker.taxonomy import Taxonomy

    tax = Taxonomy.load()

    existing = conn.execute(
        "SELECT checksum FROM taxonomy_versions WHERE version = %s", (tax.version,)
    ).fetchone()
    if existing:
        if bytes(existing["checksum"]) != tax.checksum:
            raise ValueError(
                f"taxonomy {tax.version} is already loaded with different content. "
                f"A released version is immutable — bump the version in "
                f"taxonomy/practice_groups.yaml, sectors.yaml and "
                f"taxonomy_mappings.yaml instead of editing it in place."
            )
        return tax.version, len(tax.practice_groups), len(tax.sectors)

    conn.execute(
        "INSERT INTO taxonomy_versions (version, checksum, notes, is_current) "
        "VALUES (%s, %s, %s, true)",
        (tax.version, tax.checksum, "loaded by tracker taxonomy load"),
    )
    conn.execute(
        "UPDATE taxonomy_versions SET is_current = false WHERE version <> %s",
        (tax.version,),
    )

    # Parents first, so a child's composite FK to its parent resolves.
    for table, nodes in (("practice_groups", tax.practice_groups), ("sectors", tax.sectors)):
        ids: dict[str, str] = {}
        for node in sorted(nodes, key=lambda n: n.level):
            extra = ", is_unclassified" if table == "practice_groups" else ""
            values = ", %s" if table == "practice_groups" else ""
            row = conn.execute(
                f"""
                INSERT INTO {table}
                    (taxonomy_version, code, name, level, parent_id, sort_order{extra})
                VALUES (%s, %s, %s, %s, %s, %s{values})
                RETURNING id
                """,
                (
                    tax.version, node.code, node.name, node.level,
                    ids.get(node.parent) if node.parent else None,
                    len(ids),
                    *((node.is_unclassified,) if table == "practice_groups" else ()),
                ),
            ).fetchone()
            ids[node.code] = row["id"]

    conn.commit()
    return tax.version, len(tax.practice_groups), len(tax.sectors)
