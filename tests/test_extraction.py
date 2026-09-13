"""Extraction logic, tested without spending money.

The parts worth testing here are the ones that decide whether to believe the
model: span verification, the required-field floor, and the confidence
function. None of them need an API call, and all of them are where a wrong
record would come from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from tracker.extract import confidence
from tracker.extract.extractor import (
    CostCeilingExceeded,
    CostLedger,
    Extractor,
    cost_of,
)
from tracker.extract.spans import verify
from tracker.sources.base import RawItem

TEXT = (
    "Rajah & Tann Singapore Welcomes Scott Tan as Partner in International "
    "Arbitration\n\nScott Tan joins the firm's Singapore office from Drew & "
    "Napier, where he was a director. He advises on construction disputes."
)


def spanned(value: str, text: str = TEXT) -> dict:
    start = text.index(value)
    return {"value": value, "span_start": start, "span_end": start + len(value)}


# ---------------------------------------------------------------------------
# Span verification
# ---------------------------------------------------------------------------


def test_a_correct_span_verifies_exactly():
    result = verify("to_firm", spanned("Rajah & Tann Singapore"), TEXT)
    assert result.quality == "exact"
    assert result.value == "Rajah & Tann Singapore"


def test_a_wrong_span_is_repaired_when_the_value_is_really_in_the_text():
    """Models quote well and count characters badly. Repair, do not discard."""
    field = {"value": "Drew & Napier", "span_start": 0, "span_end": 13}
    result = verify("from_firm", field, TEXT)
    assert result.quality == "recovered"
    assert TEXT[result.span_start : result.span_end] == "Drew & Napier"


def test_a_value_absent_from_the_text_is_dropped():
    """The failure this whole layer exists to catch."""
    field = {"value": "Allen & Gledhill", "span_start": 0, "span_end": 16}
    result = verify("from_firm", field, TEXT)
    assert result.quality == "dropped"
    assert not result.kept


def test_a_plausible_practice_area_the_text_never_states_is_dropped():
    field = {"value": "Private Equity", "span_start": 10, "span_end": 24}
    assert verify("practice_text", field, TEXT).quality == "dropped"


def test_coded_fields_are_not_string_matched_but_still_need_a_real_span():
    """office_jurisdiction returns HK for "Hong Kong"; the code is not in the text."""
    ok = verify(
        "office_jurisdiction",
        {"value": "SG", "span_start": 0, "span_end": 22},
        TEXT,
        value_must_appear=False,
    )
    assert ok.quality == "exact"

    nonsense = verify(
        "office_jurisdiction",
        {"value": "SG", "span_start": 9000, "span_end": 9010},
        TEXT,
        value_must_appear=False,
    )
    assert nonsense.quality == "dropped"


def test_span_verification_tolerates_case_and_whitespace_differences():
    field = {"value": "scott  tan", "span_start": 0, "span_end": 10}
    assert verify("person_name", field, TEXT).kept


def test_excerpts_are_capped_at_25_words():
    """Matches the database constraint, so evidence rows never bounce."""
    result = verify("person_name", spanned("Scott Tan"), TEXT)
    assert len(result.excerpt.split()) <= 25


def test_a_null_field_yields_nothing_rather_than_an_empty_record():
    assert verify("from_firm", None, TEXT) is None
    assert verify("from_firm", {"value": "  ", "span_start": 0, "span_end": 2}, TEXT) is None


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def build(raw: dict, *, tier: int = 1, access: str = "full_public"):
    return Extractor._build_move(
        raw, text=TEXT, reliability_tier=tier, access_level=access
    )


def test_a_move_without_a_verifiable_person_is_not_stored():
    assert build({"to_firm": spanned("Rajah & Tann Singapore"), "self_confidence": 0.9}) is None


def test_a_move_without_a_verifiable_destination_is_not_stored():
    assert build({"person_name": spanned("Scott Tan"), "self_confidence": 0.9}) is None


def test_a_retirement_needs_no_destination():
    move = build(
        {
            "person_name": spanned("Scott Tan"),
            "move_type": {"value": "retirement", "span_start": 0, "span_end": 10},
            "self_confidence": 0.9,
        }
    )
    assert move is not None


def test_unsupported_fields_are_dropped_and_recorded():
    move = build(
        {
            "person_name": spanned("Scott Tan"),
            "to_firm": spanned("Rajah & Tann Singapore"),
            "practice_text": {"value": "Private Equity", "span_start": 0, "span_end": 14},
            "self_confidence": 0.95,
        }
    )
    assert "practice_text" not in move.fields
    assert "practice_text" in move.dropped


def test_dropping_a_field_lowers_confidence_rather_than_being_silent():
    complete = build(
        {
            "person_name": spanned("Scott Tan"),
            "to_firm": spanned("Rajah & Tann Singapore"),
            "from_firm": spanned("Drew & Napier"),
            "title_to": spanned("Partner"),
            "practice_text": spanned("International Arbitration"),
            "office_jurisdiction": {"value": "SG", "span_start": 0, "span_end": 22},
            "self_confidence": 0.95,
        }
    )
    thin = build(
        {
            "person_name": spanned("Scott Tan"),
            "to_firm": spanned("Rajah & Tann Singapore"),
            "self_confidence": 0.95,
        }
    )
    assert complete.confidence.total > thin.confidence.total


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def test_a_firm_announcement_outranks_the_same_record_from_an_aggregator():
    common = {
        "present_fields": {"from_firm", "title_to", "practice_text"},
        "span_qualities": ["exact", "exact", "exact"],
        "self_reported": 0.9,
        "access_level": "full_public",
    }
    tier1 = confidence.score(reliability_tier=1, **common)
    tier3 = confidence.score(reliability_tier=3, **common)
    assert tier1.total > tier3.total


def test_a_headline_only_record_is_penalised():
    common = {
        "reliability_tier": 2,
        "present_fields": {"from_firm"},
        "span_qualities": ["exact"],
        "self_reported": 0.9,
    }
    full = confidence.score(access_level="full_public", **common)
    headline = confidence.score(access_level="headline_only", **common)
    assert headline.total < full.total


def test_the_model_cannot_talk_a_thin_record_over_the_line():
    """Self-reported confidence is weighted at 10% for exactly this reason."""
    boastful = confidence.score(
        reliability_tier=3,
        access_level="headline_only",
        present_fields=set(),
        span_qualities=["recovered"],
        self_reported=1.0,
    )
    assert boastful.total < confidence.AUTO_ACCEPT_THRESHOLD
    assert confidence.needs_review(boastful.total)


def test_corroboration_raises_confidence_but_is_capped():
    def at(n: int) -> float:
        return confidence.score(
            reliability_tier=2,
            access_level="summary",
            present_fields={"from_firm", "practice_text"},
            span_qualities=["exact", "exact"],
            self_reported=0.8,
            corroboration_count=n,
        ).total

    assert at(1) < at(2) < at(4)
    assert at(10) == at(4)


def test_confidence_components_are_kept_so_a_score_can_be_explained():
    components = confidence.score(
        reliability_tier=1,
        access_level="full_public",
        present_fields={"from_firm"},
        span_qualities=["exact"],
        self_reported=0.8,
    ).as_dict()
    assert set(components) >= {
        "tier_base",
        "access_factor",
        "completeness",
        "span_quality",
        "self_reported",
        "corroboration_count",
        "total",
    }


# ---------------------------------------------------------------------------
# Cost ceiling
# ---------------------------------------------------------------------------


def test_cost_is_priced_per_model():
    assert cost_of("claude-opus-5", 1_000_000, 0) == pytest.approx(5.00)
    assert cost_of("claude-opus-5", 0, 1_000_000) == pytest.approx(25.00)


def test_a_run_that_would_breach_the_ceiling_fails_loudly():
    ledger = CostLedger(ceiling_usd=0.10)
    ledger.record(0.09)
    with pytest.raises(CostCeilingExceeded, match="ceiling"):
        ledger.check_before(0.05)


def test_the_ceiling_is_checked_before_the_call_not_after():
    """Spending past the ceiling and reporting it afterwards is not a ceiling."""

    @dataclass
    class ExplodingClient:
        def __post_init__(self):
            self.messages = self

        def create(self, **_kw):  # pragma: no cover - must never run
            raise AssertionError("the API was called despite the ceiling")

    ledger = CostLedger(ceiling_usd=0.0001)
    extractor = Extractor(
        model="claude-opus-5", client=ExplodingClient(), ledger=ledger
    )
    item = RawItem(
        source_slug="test",
        url="https://example.test/a",
        headline="Firm welcomes new partner",
        published_at=datetime.now(UTC),
        access_level="summary",
        body_text="x" * 5000,
    )
    with pytest.raises(CostCeilingExceeded):
        extractor.extract(item, reliability_tier=2)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_extraction_results_do_not_carry_article_text_forward():
    """Phase 0 section 2: the text dies with the call that used it."""
    item = RawItem(
        source_slug="test",
        url="https://example.test/a",
        headline="Firm welcomes new partner",
        published_at=datetime.now(UTC),
        access_level="summary",
        body_text="the full article body",
    )
    assert item.without_text().body_text is None
