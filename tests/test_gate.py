"""Relevance gate, measured against a fixture of real headlines.

The suite reports recall and precision separately rather than a single pass
rate, because the two errors are not equally bad: a false negative is a move
the system never sees again, a false positive is one cheap model call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tracker.gate import evaluate

FIXTURE = Path(__file__).parent / "fixtures" / "gate_cases.yaml"

# Recall on movement news must be perfect on the fixture; precision may not be.
MIN_RECALL = 1.0
MIN_PRECISION = 0.90


def load_cases() -> list[dict]:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return data["cases"]


CASES = load_cases()


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[c["headline"].split("\n")[0][:50] for c in CASES],
)
def test_gate_decision_matches_fixture(case):
    decision = evaluate(case["headline"], reliability_tier=case.get("tier"))
    assert decision.passed is case["expect"], (
        f"{decision.reason}; matched {decision.matched_terms}"
    )


def test_gate_recall_and_precision_on_the_fixture():
    tp = fp = fn = tn = 0
    misses: list[str] = []
    for case in CASES:
        passed = evaluate(case["headline"], reliability_tier=case.get("tier")).passed
        if case["expect"] and passed:
            tp += 1
        elif case["expect"] and not passed:
            fn += 1
            misses.append(case["headline"])
        elif not case["expect"] and passed:
            fp += 1
        else:
            tn += 1

    recall = tp / (tp + fn) if tp + fn else 1.0
    precision = tp / (tp + fp) if tp + fp else 1.0

    assert recall >= MIN_RECALL, f"gate dropped movement news: {misses}"
    assert precision >= MIN_PRECISION, f"gate is letting too much noise through ({precision:.2f})"


def test_gate_version_is_recorded_on_every_decision():
    """Yield changes must be attributable to a rule change."""
    decision = evaluate("Firm welcomes new partner")
    assert decision.version.startswith("gate/")


def test_tier_one_lowers_the_bar_but_not_past_a_disqualifier():
    house_style = "Rajah & Tann (Thailand) Limited strengthens its Tax Practice"
    assert evaluate(house_style, reliability_tier=1).passed
    assert not evaluate(house_style, reliability_tier=2).passed

    association = (
        "Rajah & Tann Singapore Joins the Mergers & Acquisitions Association "
        "(Singapore) as a Platinum Corporate and Founding Member"
    )
    assert not evaluate(association, reliability_tier=1).passed


def test_rejected_items_carry_a_reason(conn=None):
    decision = evaluate("Full Federal Court rejects a bid for shares")
    assert not decision.passed
    assert decision.reason
