"""The extract stage: raw_items -> people, firms, moves, evidence, review queue.

**Where the text comes from.** Article text is never stored, so a standalone
extract run has to get it again, in this order:

1. In-memory, when ingest and extract run in the same process.
2. The source *feed*, re-read and matched on URL.
3. The article page itself — but only for a source whose terms have been
   reviewed (`sources.html_access_reviewed_at`). That gate is a dated human
   decision; this stage never turns it on for itself.

Step 3 matters more than it sounds. Most trade press headlines name nobody —
"Holding Redlich welcomes IP partner" — so without the body there is no record
to make. An item with no text at any step is extracted from its headline alone,
with the confidence penalty that implies.

Firm and person resolution here is deliberately minimal — exact and alias
match only. Fuzzy matching, verein resolution and name normalisation are the
Phase 4 deliverable. Until then this under-merges, which produces duplicate
records rather than wrong ones.
"""

from __future__ import annotations

import hashlib
import logging

import psycopg

from tracker import names
from tracker.extract.extractor import ExtractedMove, Extractor
from tracker.net.client import PoliteClient, RobotsDisallowed
from tracker.pipeline.runs import RunRecorder
from tracker.sources import article, registry

log = logging.getLogger(__name__)


def pending_items(conn: psycopg.Connection, *, limit: int | None = None) -> list[dict]:
    return conn.execute(
        """
        SELECT r.id, r.url, r.headline, r.published_at, r.source_access,
               s.slug AS source_slug, s.reliability_tier, s.id AS source_id,
               s.html_access_reviewed_at
        FROM raw_items r
        JOIN sources s ON s.id = r.source_id
        WHERE r.processing_state = 'new' AND r.gate_passed
        ORDER BY r.published_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def feed_text_map(client: PoliteClient, slugs: set[str]) -> dict[str, str]:
    """url -> body text, re-read from the feeds. Best effort."""
    mapping: dict[str, str] = {}
    for source in registry.load():
        if source.config.slug not in slugs or not source.collectable:
            continue
        try:
            for item in registry.build_adapter(source, client).fetch():
                if item.body_text:
                    mapping[item.url] = item.body_text
        except (RobotsDisallowed, Exception) as exc:  # noqa: BLE001
            log.warning("%s: could not re-read feed for text: %s", source.config.slug, exc)
    return mapping


def _article_body(client: PoliteClient, url: str) -> str | None:
    """Fetch and reduce one article. Never raises; a miss is just less evidence."""
    try:
        return article.body_of(client.fetch(url).text)
    except RobotsDisallowed:
        return None  # robots said no, which is a valid answer
    except Exception as exc:  # noqa: BLE001
        log.info("could not read %s: %s", url, exc)
        return None


def fingerprint(raw_item_id, person_name: str, slot: int) -> str:
    """Deterministic per (item, person). Re-extracting an item is a no-op."""
    key = f"{raw_item_id}|{names.surname_key(person_name)}|{slot}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def run(
    conn: psycopg.Connection,
    recorder: RunRecorder,
    *,
    extractor: Extractor,
    text_by_url: dict[str, str] | None = None,
    client: PoliteClient | None = None,
    limit: int | None = None,
) -> None:
    from tracker.sources.base import RawItem

    items = pending_items(conn, limit=limit)
    if not items:
        return

    if text_by_url is None:
        text_by_url = (
            feed_text_map(client, {i["source_slug"] for i in items}) if client else {}
        )

    for row in items:
        body = text_by_url.get(row["url"])

        # No body from the feed, but this source's terms have been reviewed:
        # read the article. Most headlines name nobody, so without this the
        # database path yields far less than `tracker collect` does.
        if body is None and row["html_access_reviewed_at"] and client is not None:
            body = _article_body(client, row["url"])

        item = RawItem(
            source_slug=row["source_slug"],
            url=row["url"],
            headline=row["headline"],
            published_at=row["published_at"],
            access_level=row["source_access"],
            body_text=body,
        )
        try:
            result = extractor.extract(item, reliability_tier=row["reliability_tier"])
        except Exception as exc:  # noqa: BLE001 - the ledger raises through, see cli
            if type(exc).__name__ == "CostCeilingExceeded":
                raise
            log.warning("%s: extraction failed: %s", row["url"], exc)
            _mark(conn, row["id"], "error", error_message=f"{type(exc).__name__}: {exc}")
            recorder.fail(row["source_slug"], str(exc))
            conn.commit()
            continue

        recorder.llm_cost_usd += result.cost_usd
        _record_llm_call(conn, recorder, row, result)

        if result.error:
            _mark(conn, row["id"], "error", error_message=result.error)
            recorder.fail(row["source_slug"], result.error)
            conn.commit()
            continue

        if not result.is_movement or not result.moves:
            _mark(
                conn,
                row["id"],
                "rejected",
                reject_reason=result.not_movement_reason or "extraction found no named move",
            )
            conn.commit()
            continue

        for slot, move in enumerate(result.moves):
            _persist(conn, recorder, row, move, slot)

        _mark(conn, row["id"], "extracted")
        conn.commit()


# ---------------------------------------------------------------------------


def _mark(conn, raw_item_id, state, *, reject_reason=None, error_message=None) -> None:
    conn.execute(
        "UPDATE raw_items SET processing_state = %s, reject_reason = %s, "
        "error_message = %s, processed_at = now() WHERE id = %s",
        (state, reject_reason, error_message, raw_item_id),
    )


def _record_llm_call(conn, recorder: RunRecorder, row, result) -> None:
    if not result.model or (not result.input_tokens and not result.error):
        return
    conn.execute(
        """
        INSERT INTO llm_calls (run_id, raw_item_id, stage, purpose, model,
                               input_tokens, output_tokens, cost_usd,
                               latency_ms, succeeded, error)
        VALUES (%s, %s, 'extract', 'extract', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            recorder.run_id, row["id"], result.model, result.input_tokens,
            result.output_tokens, round(result.cost_usd, 6), result.latency_ms,
            result.error is None, result.error,
        ),
    )


def _resolve_firm(conn, name: str | None):
    """Exact name, then alias. No fuzzy matching until Phase 4."""
    if not name:
        return None, False
    row = conn.execute(
        "SELECT id FROM firms WHERE lower(btrim(canonical_name)) = lower(btrim(%s))",
        (name,),
    ).fetchone()
    if row:
        return row["id"], False

    row = conn.execute(
        """
        SELECT a.firm_id FROM firm_aliases a
        WHERE lower(btrim(a.alias)) = lower(btrim(%s))
          AND NOT EXISTS (
              SELECT 1 FROM ambiguous_firm_aliases amb
              WHERE amb.alias_key = lower(btrim(%s))
          )
        LIMIT 1
        """,
        (name, name),
    ).fetchone()
    if row:
        return row["firm_id"], False

    # Unknown firm. Created as 'other' and flagged, rather than guessed at.
    row = conn.execute(
        "INSERT INTO firms (canonical_name, firm_type) VALUES (%s, 'other') RETURNING id",
        (name.strip(),),
    ).fetchone()
    return row["id"], True


def _resolve_person(conn, name: str):
    surname = names.surname_key(name)
    row = conn.execute(
        "SELECT id FROM people WHERE surname_normalised = %s "
        "AND (lower(canonical_name) = lower(%s) OR %s = ANY(name_variants)) LIMIT 1",
        (surname, name, name),
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "INSERT INTO people (canonical_name, surname_normalised, given_normalised, "
        "name_variants) VALUES (%s, %s, %s, %s) RETURNING id",
        (name.strip(), surname or name.strip().lower(), names.given_key(name),
         names.variants(name)),
    ).fetchone()
    return row["id"]


def _persist(conn, recorder: RunRecorder, row, move: ExtractedMove, slot: int) -> None:
    person_name = move.person_name
    if not person_name:
        return

    fp = fingerprint(row["id"], person_name, slot)
    if conn.execute(
        "SELECT 1 FROM moves WHERE extraction_fingerprint = %s", (fp,)
    ).fetchone():
        return  # already extracted; re-running is a no-op

    person_id = _resolve_person(conn, person_name)
    to_firm_id, to_is_new = _resolve_firm(conn, move.value("to_firm"))
    from_firm_id, from_is_new = _resolve_firm(conn, move.value("from_firm"))
    move_type = move.value("move_type") or "lateral"

    # The schema rejects these outright; catching them here keeps one bad
    # extraction from aborting the batch.
    if move_type == "promotion" and from_firm_id != to_firm_id:
        from_firm_id = to_firm_id
    if to_firm_id is not None and to_firm_id == from_firm_id and move_type != "promotion":
        from_firm_id = None

    jurisdiction = move.value("office_jurisdiction")
    if jurisdiction and not conn.execute(
        "SELECT 1 FROM jurisdictions WHERE code = %s", (jurisdiction,)
    ).fetchone():
        jurisdiction = None  # unknown market; surfaced via review below

    needs_review = move.needs_review or to_is_new or from_is_new

    created = conn.execute(
        """
        INSERT INTO moves
            (person_id, from_firm_id, to_firm_id, title_from, title_to,
             partner_tier, office_jurisdiction, announced_date, move_type,
             confidence, confidence_components, review_state, extraction_fingerprint)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            person_id, from_firm_id, to_firm_id,
            move.value("title_from"), move.value("title_to"),
            move.value("partner_tier") or "undisclosed", jurisdiction,
            row["published_at"], move_type,
            move.confidence.total, _json(move.confidence.as_dict()),
            "pending_review" if needs_review else "auto_accepted",
            fp,
        ),
    ).fetchone()
    move_id = created["id"]
    recorder.moves_created += 1

    conn.execute(
        "INSERT INTO move_sources (move_id, raw_item_id, is_primary) VALUES (%s, %s, true)",
        (move_id, row["id"]),
    )

    for field_name, verified in move.fields.items():
        if field_name in {"practice_text", "sector_text", "effective_date_text"}:
            evidence_name = {"practice_text": "practice_group",
                             "sector_text": "sector",
                             "effective_date_text": "effective_date"}[field_name]
        elif field_name == "to_firm":
            evidence_name = "to_firm"
        elif field_name == "from_firm":
            evidence_name = "from_firm"
        else:
            evidence_name = field_name
        conn.execute(
            """
            INSERT INTO move_field_evidence
                (move_id, raw_item_id, field_name, span_start, span_end,
                 extracted_value, excerpt)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (move_id, raw_item_id, field_name) DO NOTHING
            """,
            (move_id, row["id"], evidence_name, verified.span_start,
             verified.span_end, verified.value, verified.excerpt or verified.value),
        )

    if needs_review:
        reason = "low_confidence"
        if to_is_new or from_is_new:
            reason = "conflicting_sources" if not move.needs_review else "low_confidence"
        conn.execute(
            "INSERT INTO review_queue (move_id, reason, detail) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (move_id, reason, _json({
                "confidence": move.confidence.as_dict(),
                "dropped_fields": move.dropped,
                "new_firms": [n for n, new in
                              ((move.value("to_firm"), to_is_new),
                               (move.value("from_firm"), from_is_new)) if new and n],
            })),
        )
        recorder.moves_queued_for_review += 1


def _json(value) -> str:
    import json

    return json.dumps(value, default=str)
