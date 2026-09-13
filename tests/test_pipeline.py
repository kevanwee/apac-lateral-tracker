"""Ingest and extract against a real database.

No network and no model: the adapter and the extractor are both stubbed, so
what is under test is the persistence — idempotency, evidence rows, review
routing, and the run bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest

from tracker.extract.extractor import ExtractionResult, Extractor
from tracker.gate import GATE_VERSION, evaluate
from tracker.pipeline import extract as extract_stage
from tracker.pipeline import ingest as ingest_stage
from tracker.pipeline import runs
from tracker.sources.base import RawItem

HEADLINE = "Rajah & Tann Singapore Welcomes Scott Tan as Partner in International Arbitration"
BODY = (
    "Scott Tan joins the firm's Singapore office from Drew & Napier, where he "
    "was a director, and will practise in International Arbitration."
)


@pytest.fixture
def synced(clean_conn: psycopg.Connection):
    """The register, loaded into an empty database."""
    upserted, _ = ingest_stage.sync_sources(clean_conn)
    assert upserted >= 3
    return clean_conn


def make_item(url: str = "https://example.test/scott-tan") -> RawItem:
    return RawItem(
        source_slug="rajah-tann-asia",
        url=url,
        headline=HEADLINE,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        access_level="full_public",
        body_text=BODY,
    )


@dataclass
class StubExtractor:
    """Returns a real, span-verified record without calling the API."""

    moves_per_item: int = 1
    model: str = "stub"

    def extract(self, item: RawItem, *, reliability_tier: int) -> ExtractionResult:
        text = item.extraction_text

        def spanned(value):
            start = text.index(value)
            return {"value": value, "span_start": start, "span_end": start + len(value)}

        raw = {
            "person_name": spanned("Scott Tan"),
            "to_firm": spanned("Rajah & Tann Singapore"),
            "from_firm": spanned("Drew & Napier"),
            "title_from": spanned("director"),
            "title_to": spanned("Partner"),
            "practice_text": spanned("International Arbitration"),
            "office_jurisdiction": {"value": "SG", "span_start": 0, "span_end": 22},
            "move_type": {"value": "lateral", "span_start": 0, "span_end": 22},
            "self_confidence": 0.95,
        }
        built = [
            Extractor._build_move(
                raw,
                text=text,
                reliability_tier=reliability_tier,
                access_level=item.access_level,
            )
            for _ in range(self.moves_per_item)
        ]
        return ExtractionResult(
            item=item.without_text(),
            is_movement=True,
            not_movement_reason=None,
            moves=[m for m in built if m],
            input_tokens=900,
            output_tokens=200,
            cost_usd=0.0095,
            latency_ms=1200,
            model=self.model,
        )


def insert_item(conn, item: RawItem, tier: int = 1) -> str:
    source = conn.execute(
        "SELECT id, reliability_tier FROM sources WHERE slug = %s", (item.source_slug,)
    ).fetchone()
    decision = evaluate(item.headline, item.body_text, reliability_tier=tier)
    ingest_stage._insert_item(conn, source["id"], item, decision)
    conn.commit()
    return conn.execute("SELECT id FROM raw_items WHERE url = %s", (item.url,)).fetchone()["id"]


# ---------------------------------------------------------------------------
# Source register
# ---------------------------------------------------------------------------


def test_sync_loads_the_register_and_creates_firms_for_tier_one(synced):
    row = synced.execute(
        "SELECT s.reliability_tier, f.canonical_name FROM sources s "
        "JOIN firms f ON f.id = s.firm_id WHERE s.slug = 'rajah-tann-asia'"
    ).fetchone()
    assert row["reliability_tier"] == 1
    assert row["canonical_name"] == "Rajah & Tann Asia"


def test_a_blocked_source_is_synced_but_never_active(synced):
    """ALB stays on the record with its reason, and is not collected."""
    row = synced.execute(
        "SELECT active FROM sources WHERE slug = 'asian-legal-business'"
    ).fetchone()
    assert row is not None
    assert row["active"] is False


def test_sync_is_idempotent(synced):
    before = synced.execute("SELECT count(*) AS n FROM sources").fetchone()["n"]
    ingest_stage.sync_sources(synced)
    after = synced.execute("SELECT count(*) AS n FROM sources").fetchone()["n"]
    assert before == after


def test_a_blocked_source_is_never_returned_as_due(synced):
    slugs = {r["slug"] for r in ingest_stage.due_sources(synced)}
    assert "asian-legal-business" not in slugs


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingested_items_record_the_gate_decision(synced):
    item_id = insert_item(synced, make_item())
    row = synced.execute(
        "SELECT gate_passed, gate_version, gate_score, gate_matched_terms, "
        "processing_state FROM raw_items WHERE id = %s",
        (item_id,),
    ).fetchone()
    assert row["gate_passed"] is True
    assert row["gate_version"] == GATE_VERSION
    assert row["processing_state"] == "new"
    assert row["gate_matched_terms"]


def test_gate_rejections_are_stored_with_a_reason_not_discarded(synced):
    """The gate's false-negative rate can only be measured if rejects are kept."""
    item = RawItem(
        source_slug="rajah-tann-asia",
        url="https://example.test/earth-day",
        headline="Rajah & Tann Singapore Marks Earth Day with Youth Clean-Up",
        published_at=datetime(2026, 4, 22, tzinfo=UTC),
        access_level="full_public",
    )
    item_id = insert_item(synced, item)
    row = synced.execute(
        "SELECT gate_passed, processing_state, reject_reason FROM raw_items WHERE id = %s",
        (item_id,),
    ).fetchone()
    assert row["gate_passed"] is False
    assert row["processing_state"] == "rejected"
    assert "relevance_gate" in row["reject_reason"]


def test_reingesting_the_same_url_creates_nothing(synced):
    insert_item(synced, make_item())
    insert_item(synced, make_item())
    n = synced.execute(
        "SELECT count(*) AS n FROM raw_items WHERE url = %s",
        ("https://example.test/scott-tan",),
    ).fetchone()["n"]
    assert n == 1


def test_raw_items_hold_no_article_text(synced):
    insert_item(synced, make_item())
    row = synced.execute(
        "SELECT * FROM raw_items WHERE url = %s", ("https://example.test/scott-tan",)
    ).fetchone()
    assert BODY not in str(row)


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def extract_once(conn, **kw):
    with runs.record(conn, "extract") as recorder:
        extract_stage.run(
            conn, recorder, extractor=kw.pop("extractor", StubExtractor()), **kw
        )
    return recorder


def test_extract_creates_a_move_with_its_people_firms_and_evidence(synced):
    insert_item(synced, make_item())
    recorder = extract_once(synced, text_by_url={"https://example.test/scott-tan": BODY})

    assert recorder.moves_created == 1
    move = synced.execute(
        "SELECT m.*, p.canonical_name, ff.canonical_name AS from_name, "
        "tf.canonical_name AS to_name FROM moves m "
        "JOIN people p ON p.id = m.person_id "
        "LEFT JOIN firms ff ON ff.id = m.from_firm_id "
        "JOIN firms tf ON tf.id = m.to_firm_id"
    ).fetchone()
    assert move["canonical_name"] == "Scott Tan"
    assert move["to_name"] == "Rajah & Tann Singapore"
    assert move["from_name"] == "Drew & Napier"
    assert move["office_jurisdiction"] == "SG"
    assert move["move_type"] == "lateral"

    evidence = synced.execute(
        "SELECT field_name, excerpt FROM move_field_evidence WHERE move_id = %s",
        (move["id"],),
    ).fetchall()
    assert {e["field_name"] for e in evidence} >= {"person_name", "to_firm", "from_firm"}
    for e in evidence:
        assert len(e["excerpt"].split()) <= 25

    link = synced.execute(
        "SELECT is_primary FROM move_sources WHERE move_id = %s", (move["id"],)
    ).fetchone()
    assert link["is_primary"] is True


def test_rerunning_extract_creates_no_duplicate_moves(synced):
    insert_item(synced, make_item())
    text = {"https://example.test/scott-tan": BODY}
    extract_once(synced, text_by_url=text)

    # Re-arm the item as if the stage were run again over the same input.
    synced.execute("UPDATE raw_items SET processing_state = 'new'")
    synced.commit()
    second = extract_once(synced, text_by_url=text)

    assert second.moves_created == 0
    assert synced.execute("SELECT count(*) AS n FROM moves").fetchone()["n"] == 1


def test_an_unknown_firm_routes_the_move_to_review_rather_than_being_trusted(synced):
    insert_item(synced, make_item())
    extract_once(synced, text_by_url={"https://example.test/scott-tan": BODY})

    move = synced.execute("SELECT id, review_state FROM moves").fetchone()
    assert move["review_state"] == "pending_review"
    queued = synced.execute(
        "SELECT reason, detail FROM review_queue WHERE move_id = %s", (move["id"],)
    ).fetchone()
    assert queued is not None
    assert queued["detail"]["new_firms"]


def test_an_item_the_extractor_finds_nothing_in_is_rejected_not_left_pending(synced):
    insert_item(synced, make_item())

    @dataclass
    class NothingFound:
        model = "stub"

        def extract(self, item, *, reliability_tier):
            return ExtractionResult(
                item=item.without_text(),
                is_movement=False,
                not_movement_reason="firm announcement naming no individual",
                model="stub",
                input_tokens=400,
                output_tokens=50,
            )

    extract_once(synced, extractor=NothingFound(), text_by_url={})
    row = synced.execute(
        "SELECT processing_state, reject_reason FROM raw_items"
    ).fetchone()
    assert row["processing_state"] == "rejected"
    assert "naming no individual" in row["reject_reason"]


def test_llm_spend_is_recorded_per_call(synced):
    insert_item(synced, make_item())
    recorder = extract_once(synced, text_by_url={"https://example.test/scott-tan": BODY})

    call = synced.execute("SELECT * FROM llm_calls").fetchone()
    assert call["stage"] == "extract"
    assert call["input_tokens"] == 900
    assert float(call["cost_usd"]) == pytest.approx(0.0095)
    assert recorder.llm_cost_usd == pytest.approx(0.0095)


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


def test_every_run_writes_a_record_even_when_it_fails(synced):
    with (  # noqa: PT012
        pytest.raises(RuntimeError),
        runs.record(synced, "ingest") as recorder,
    ):
        recorder.items_fetched = 7
        raise RuntimeError("feed exploded")

    row = synced.execute(
        "SELECT status, items_fetched, finished_at FROM pipeline_runs "
        "WHERE stage = 'ingest' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["items_fetched"] == 7
    assert row["finished_at"] is not None


def test_three_empty_runs_raise_a_source_alert(synced):
    source_id = synced.execute(
        "SELECT id FROM sources WHERE slug = 'global-legal-post'"
    ).fetchone()["id"]

    for _ in range(3):
        with runs.record(synced, "ingest") as recorder:
            recorder.source_yield(source_id, fetched=0, new=0, gate_passed=0)
        synced.commit()

    alerts = synced.execute("SELECT slug FROM source_yield_alerts").fetchall()
    assert "global-legal-post" in {a["slug"] for a in alerts}


def test_a_yielding_run_clears_the_alert_counter(synced):
    source_id = synced.execute(
        "SELECT id FROM sources WHERE slug = 'global-legal-post'"
    ).fetchone()["id"]
    with runs.record(synced, "ingest") as recorder:
        for _ in range(3):
            recorder.source_yield(source_id, fetched=0, new=0, gate_passed=0)
        recorder.source_yield(source_id, fetched=5, new=5, gate_passed=2)
    synced.commit()
    assert synced.execute("SELECT * FROM source_yield_alerts").fetchall() == []


def test_robots_skips_are_not_counted_as_a_broken_feed(synced):
    """A zero yield from politeness must not trip the feed-broken alarm."""
    source_id = synced.execute(
        "SELECT id FROM sources WHERE slug = 'global-legal-post'"
    ).fetchone()["id"]
    with runs.record(synced, "ingest") as recorder:
        for _ in range(4):
            recorder.source_yield(
                source_id, fetched=0, new=0, gate_passed=0,
                skipped_reason="disallowed by robots.txt",
            )
    synced.commit()
    assert synced.execute("SELECT * FROM source_yield_alerts").fetchall() == []


def test_the_dataset_has_no_invariant_violations_after_a_run(synced):
    insert_item(synced, make_item())
    extract_once(synced, text_by_url={"https://example.test/scott-tan": BODY})
    assert synced.execute("SELECT * FROM invariant_violations").fetchall() == []
