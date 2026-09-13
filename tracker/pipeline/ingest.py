"""The ingest stage: config/sources.yaml -> sources -> raw_items.

Idempotent by construction. Re-running over the same window inserts nothing:
`raw_items.url` is unique, and a conflicting insert is a no-op rather than an
error, so a backfill can be interrupted and resumed at any point.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg

from tracker.gate import GATE_VERSION, evaluate
from tracker.net.client import PoliteClient, RobotsDisallowed
from tracker.pipeline.runs import RunRecorder
from tracker.sources import registry
from tracker.sources.base import RawItem
from tracker.sources.registry import RegisteredSource

log = logging.getLogger(__name__)


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
                (slug, name, base_url, feed_url, access_type, reliability_tier,
                 firm_id, jurisdiction_focus, default_access_level, active,
                 poll_interval, terms_url, terms_note,
                 html_access_reviewed_at, html_access_reviewed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    make_interval(hours => %s), %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                base_url = EXCLUDED.base_url,
                feed_url = EXCLUDED.feed_url,
                access_type = EXCLUDED.access_type,
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
                cfg.reliability_tier, firm_id, list(cfg.jurisdiction_focus),
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
    except RobotsDisallowed as exc:
        # Politeness, not failure. Recorded so a zero yield caused by robots
        # is never mistaken for a broken feed.
        log.info("%s: %s", source.config.slug, exc)
        recorder.source_yield(
            row["id"], fetched=0, new=0, gate_passed=0, skipped_reason=str(exc)[:200]
        )
        return
    except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
        log.warning("%s: fetch failed: %s", source.config.slug, exc)
        recorder.fail(source.config.slug, f"{type(exc).__name__}: {exc}")
        recorder.source_yield(
            row["id"], fetched=0, new=0, gate_passed=0, error=f"{type(exc).__name__}: {exc}"
        )
        return

    for item in items:
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
             content_hash, source_access, processing_state, reject_reason,
             gate_version, gate_passed, gate_score, gate_matched_terms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO NOTHING
        RETURNING id
        """,
        (
            source_id,
            item.url,
            item.headline,
            item.published_at,
            item.published_at_is_estimated,
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
