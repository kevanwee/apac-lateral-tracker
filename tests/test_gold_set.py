"""Gold set integrity.

These tests do not measure the pipeline — they check that the fixture the
metrics are computed from is internally consistent and honestly labelled. A
gold set with a silent error produces confident wrong numbers, which is worse
than no numbers.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from tracker.extract.schema import MOVE_TYPES, PARTNER_TIERS

FIXTURE = Path(__file__).parent / "fixtures" / "gold_set.yaml"
GOLD = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
RECORDS = GOLD["records"]

MOVE_FIELDS = {
    "person_name",
    "from_firm",
    "to_firm",
    "title_from",
    "title_to",
    "partner_tier",
    "office_jurisdiction",
    "practice_text",
    "sector_text",
    "move_type",
    "team_size",
    "primary_practice_group",
    "secondary_practice_groups",
    "sectors",
}


def all_moves():
    for record in RECORDS:
        for move in record["expect"]["moves"]:
            yield record, move


def test_record_ids_are_unique():
    ids = [r["id"] for r in RECORDS]
    assert len(ids) == len(set(ids))


def test_every_record_declares_its_provenance():
    for record in RECORDS:
        assert record["provenance"] in {"live_feed", "synthetic"}, record["id"]


def test_live_records_carry_a_source_url():
    """A live record without a URL cannot be re-verified, so it is not live."""
    for record in RECORDS:
        if record["provenance"] == "live_feed":
            assert record["url"], record["id"]
            assert record["url"].startswith("https://"), record["id"]


def test_synthetic_records_are_never_given_a_url():
    for record in RECORDS:
        if record["provenance"] == "synthetic":
            assert record["url"] is None, record["id"]
            assert record["source_slug"] == "synthetic", record["id"]


def test_body_text_respects_the_25_word_cap():
    """Same cap the database puts on evidence excerpts. This is not a corpus."""
    cap = GOLD["text_word_cap"]
    for record in RECORDS:
        text = record.get("text")
        if text:
            assert len(text.split()) <= cap, f"{record['id']}: {len(text.split())} words"


def test_every_move_uses_the_full_field_set():
    for record, move in all_moves():
        assert set(move) == MOVE_FIELDS, (
            f"{record['id']}: {set(move) ^ MOVE_FIELDS}"
        )


def test_move_types_and_tiers_match_the_database_vocabularies():
    for record, move in all_moves():
        assert move["move_type"] in MOVE_TYPES, record["id"]
        assert move["partner_tier"] in PARTNER_TIERS, record["id"]


def test_expected_moves_satisfy_the_schema_invariants():
    """The gold set cannot expect something the database would refuse to store."""
    for record, move in all_moves():
        same_firm = (
            move["from_firm"] is not None
            and move["from_firm"] == move["to_firm"]
        )
        if same_firm:
            assert move["move_type"] == "promotion", (
                f"{record['id']}: same firm at both ends but not a promotion"
            )
        if move["move_type"] == "promotion":
            assert same_firm, f"{record['id']}: promotion across two firms"
        if move["to_firm"] is None:
            assert move["move_type"] == "retirement", (
                f"{record['id']}: no destination but not a retirement"
            )


def test_every_move_has_exactly_one_primary_practice_group():
    for record, move in all_moves():
        assert move["primary_practice_group"], record["id"]
        assert len(move["secondary_practice_groups"]) <= 2, record["id"]
        assert move["primary_practice_group"] not in move["secondary_practice_groups"], (
            f"{record['id']}: primary group repeated as a secondary"
        )


def test_practice_groups_and_sectors_are_kept_apart():
    """A sector code must never appear in a practice group field."""
    sector_codes = {s for _, m in all_moves() for s in m["sectors"]}
    group_codes = {m["primary_practice_group"] for _, m in all_moves()}
    group_codes |= {g for _, m in all_moves() for g in m["secondary_practice_groups"]}
    assert not (sector_codes & group_codes)


def test_a_record_with_no_moves_says_why():
    for record in RECORDS:
        if not record["expect"]["moves"]:
            has_reason = record["expect"].get("not_movement_reason") or record.get("note")
            assert has_reason, f"{record['id']}: empty moves list with no explanation"


# ---------------------------------------------------------------------------
# Coverage — reported, not asserted, except where a gap would invalidate a metric
# ---------------------------------------------------------------------------


def test_coverage_report(capsys):
    """Prints the shape of the set. Read this before trusting any metric."""
    live = [r for r in RECORDS if r["provenance"] == "live_feed"]
    synthetic = [r for r in RECORDS if r["provenance"] == "synthetic"]
    moves = [m for _, m in all_moves()]
    tiers = Counter(r["reliability_tier"] for r in RECORDS)
    top_level = Counter(
        m["primary_practice_group"].split(".")[0] for m in moves
    )
    jurisdictions = Counter(
        m["office_jurisdiction"] or "unstated" for m in moves
    )

    with capsys.disabled():
        print(f"\n  records      {len(RECORDS)} ({len(live)} live, {len(synthetic)} synthetic)")
        print(f"  moves        {len(moves)}")
        print(f"  tiers        {dict(sorted(tiers.items()))}")
        print(f"  practice     {len(top_level)} top-level groups {dict(top_level)}")
        print(f"  jurisdiction {dict(jurisdictions)}")


def test_all_three_source_tiers_are_represented():
    tiers = {r["reliability_tier"] for r in RECORDS}
    assert tiers == {1, 2, 3}, f"missing tier(s): {({1, 2, 3}) - tiers}"


def test_at_least_eight_practice_groups_are_represented():
    groups = {m["primary_practice_group"] for _, m in all_moves()}
    groups |= {g for _, m in all_moves() for g in m["secondary_practice_groups"]}
    groups.discard("unclassified")
    assert len(groups) >= 8, f"only {len(groups)} distinct practice groups: {sorted(groups)}"


@pytest.mark.xfail(
    reason="Target is 100 verified moves. Blocked on ALB access and a database-backed "
    "backfill; see the header of gold_set.yaml. Failing on purpose so the gap "
    "stays visible instead of being quietly accepted.",
    strict=True,
)
def test_gold_set_has_reached_its_target_size():
    live_moves = [m for r, m in all_moves() if r["provenance"] == "live_feed"]
    assert len(live_moves) >= 100


# ---------------------------------------------------------------------------
# Threshold calibration against the gold set
# ---------------------------------------------------------------------------
# These need no API key and no model. They ask a question the model cannot
# affect: if extraction were flawless, what would the review queue look like?
# If a perfect record still needs a human, the threshold is wrong whatever the
# model does.

SPANNED_FIELDS = [
    "from_firm", "to_firm", "title_from", "title_to", "practice_text", "sector_text",
]
MAX_REVIEW_RATE = 0.15


def perfect_confidence(record: dict, move: dict):
    """What a flawless extraction of this gold record would score."""
    from tracker.extract import confidence

    present = {
        f for f in confidence.COMPLETENESS_WEIGHTS
        if move.get(f) is not None
        and not (f == "partner_tier" and move[f] == "undisclosed")
    }
    spans = sum(
        1 for f in [*SPANNED_FIELDS, "person_name", "office_jurisdiction", "move_type"]
        if move.get(f) is not None
    )
    return confidence.score(
        reliability_tier=record["reliability_tier"],
        access_level=record["access_level"],
        present_fields=present,
        span_qualities=["exact"] * max(spans, 1),
        self_reported=0.9,
    )


def test_every_gold_expectation_is_supported_by_the_available_text():
    """A gold answer the text cannot support is asking the model to infer."""
    from tracker.extract.spans import _find_verbatim

    unsupported = []
    for record in RECORDS:
        text = record["headline"]
        if record.get("text"):
            text += "\n\n" + record["text"]
        for move in record["expect"]["moves"]:
            for field in ["person_name", *SPANNED_FIELDS]:
                value = move.get(field)
                if value and _find_verbatim(text, str(value)) is None:
                    unsupported.append(f"{record['id']}.{field}={value!r}")
    assert not unsupported, f"gold expects values absent from the text: {unsupported}"


def test_a_perfect_extraction_stays_under_the_review_ceiling():
    """Regression guard on a real miscalibration.

    Completeness used to be 25% of the score, which penalised a record for
    correctly returning null on a field the article never stated. That put 79%
    of perfect extractions into the queue against a 15% ceiling.
    """
    from tracker.extract import confidence

    scores = [perfect_confidence(r, m) for r, m in all_moves()]
    reviewed = [c for c in scores if confidence.needs_review(c)]
    rate = len(reviewed) / len(scores)
    assert rate <= MAX_REVIEW_RATE, (
        f"{rate:.0%} of flawless extractions would need a human "
        f"(ceiling {MAX_REVIEW_RATE:.0%}); the thresholds are wrong, not the model"
    )


def test_thin_evidence_still_reaches_a_human():
    """The ceiling must not have been met by accepting everything."""
    from tracker.extract import confidence

    for tier, access in [(3, "summary"), (2, "headline_only"), (3, "headline_only")]:
        best_case = confidence.score(
            reliability_tier=tier,
            access_level=access,
            present_fields=set(confidence.COMPLETENESS_WEIGHTS),
            span_qualities=["exact"] * 6,
            self_reported=1.0,
        )
        assert confidence.needs_review(best_case), (
            f"tier {tier} / {access} auto-accepts even at its best case"
        )
