"""Migration 0012 and the classification evidence chain, against a real database.

Runs in CI (and locally with TRACKER_TEST_DATABASE_URL). Stubbed extractor,
no network, no model — what is under test is that a persisted move carries
its extractor version, reaches the analysis views with the right joins, and
that a classification derived from the headline leaves an evidence row.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest

from tracker.extract.extractor import ExtractionResult, Extractor
from tracker.pipeline import extract as extract_stage
from tracker.pipeline import ingest as ingest_stage
from tracker.pipeline import runs
from tracker.sources.base import RawItem
from tracker.taxonomy import Taxonomy

from .test_pipeline import insert_item

# A headline that names the practice but no practice clause in the body, so the
# only classification evidence is the headline.
HEADLINE = "Rajah & Tann Singapore Welcomes Scott Tan as Capital Markets Partner"
BODY = "Scott Tan joins the firm's Singapore office from Drew & Napier."


@pytest.fixture
def ready(clean_conn: psycopg.Connection):
    ingest_stage.sync_sources(clean_conn)
    ingest_stage.sync_firms(clean_conn)
    ingest_stage.sync_taxonomy(clean_conn)
    return clean_conn


@dataclass
class HeadlineOnlyStub:
    """A verified record with no stated practice: classification must look further."""

    model: str = "stub/9.9.9"

    def extract(self, item: RawItem, *, reliability_tier: int) -> ExtractionResult:
        text = item.extraction_text

        def spanned(value):
            start = text.index(value)
            return {"value": value, "span_start": start, "span_end": start + len(value)}

        raw = {
            "person_name": spanned("Scott Tan"),
            "to_firm": spanned("Rajah & Tann Singapore"),
            "from_firm": spanned("Drew & Napier"),
            "title_to": spanned("Partner"),
            "move_type": {"value": "lateral", "span_start": 0, "span_end": 22},
            "self_confidence": 0.95,
        }
        built = Extractor._build_move(
            raw, text=text, reliability_tier=reliability_tier, access_level=item.access_level
        )
        return ExtractionResult(
            item=item.without_text(), is_movement=True, not_movement_reason=None,
            moves=[built], model=self.model,
        )


def _run_extract(conn, stub) -> None:
    item = RawItem(
        source_slug="rajah-tann-asia", url="https://example.test/scott-tan-ecm",
        headline=HEADLINE, published_at=datetime(2026, 7, 20, tzinfo=UTC),
        access_level="full_public", body_text=BODY,
    )
    insert_item(conn, item)
    with runs.record(conn, "extract") as recorder:
        extract_stage.run(conn, recorder, extractor=stub, text_by_url={item.url: BODY})


def test_a_persisted_move_records_which_extractor_wrote_it(ready):
    _run_extract(ready, HeadlineOnlyStub())
    row = ready.execute("SELECT extractor_version FROM moves").fetchone()
    assert row["extractor_version"] == "stub/9.9.9"


def test_a_headline_derived_classification_is_weighted_and_evidenced(ready):
    _run_extract(ready, HeadlineOnlyStub())
    row = ready.execute(
        "SELECT practice_group_code, practice_group_top, classification_evidence, "
        "classification_confidence, classification_rule_key FROM analysis_moves"
    ).fetchone()
    assert row["practice_group_code"] == "finance.ecm"
    assert row["practice_group_top"] == "finance"
    assert row["classification_evidence"] == "headline"
    assert row["classification_rule_key"] == "headline:mapping:capital markets"
    # Discounted for coming from the headline rather than a stated clause.
    assert float(row["classification_confidence"]) < 0.8

    evidence = ready.execute(
        "SELECT extracted_value, excerpt FROM move_field_evidence "
        "WHERE field_name = 'practice_group'"
    ).fetchone()
    assert evidence is not None
    assert evidence["extracted_value"].lower() == "capital markets"


def test_the_analysis_surface_joins_source_and_date_quality(ready):
    _run_extract(ready, HeadlineOnlyStub())
    row = ready.execute(
        "SELECT source_slug, reliability_tier, date_is_estimated, period_quarter, "
        "review_state FROM analysis_moves"
    ).fetchone()
    assert row["source_slug"] == "rajah-tann-asia"
    assert row["reliability_tier"] == 1
    assert row["date_is_estimated"] is False
    assert str(row["period_quarter"]) == "2026-07-01"

    coverage = ready.execute(
        "SELECT items, gate_passed FROM source_period_coverage "
        "WHERE source_slug = 'rajah-tann-asia'"
    ).fetchone()
    assert coverage["items"] == 1 and coverage["gate_passed"] == 1

    trend = ready.execute(
        "SELECT practice_group_top, moves, share_of_classified FROM practice_group_trend"
    ).fetchall()
    assert [(t["practice_group_top"], t["moves"]) for t in trend] == [("finance", 1)]
    assert float(trend[0]["share_of_classified"]) == 1.0


def test_loading_a_second_taxonomy_version_moves_the_current_flag(ready, monkeypatch):
    """The first real version bump hit the one-current unique index."""
    current = Taxonomy.load()
    later = dataclasses.replace(current, version="9.9.9", checksum=b"\x01" * 32)
    monkeypatch.setattr(Taxonomy, "load", classmethod(lambda cls, *a, **k: later))

    version, groups, _ = ingest_stage.sync_taxonomy(ready)
    assert version == "9.9.9" and groups == len(current.practice_groups)

    rows = ready.execute(
        "SELECT version, is_current FROM taxonomy_versions ORDER BY version"
    ).fetchall()
    assert {r["version"]: r["is_current"] for r in rows} == {
        current.version: False, "9.9.9": True,
    }
