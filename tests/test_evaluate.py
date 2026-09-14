"""The Phase 7 evaluator.

These tests check the measuring instrument, not the pipeline. A metric that
silently mis-counts produces confident wrong numbers, which is worse than no
numbers at all — so the arithmetic, the matching rule and the firm comparison
are each pinned here.

The pipeline's own score is deliberately not asserted: that is what
`tracker evaluate` reports, and pinning it would turn every extraction change
into a test edit.
"""

from __future__ import annotations

from tracker import evaluate
from tracker.firms import FirmGazetteer

GAZETTEER = FirmGazetteer.load()


def test_the_evaluator_runs_over_the_shipped_gold_set():
    report = evaluate.run()
    assert report.records > 0
    assert report.expected > 0
    assert 0.0 <= report.precision <= 1.0
    assert 0.0 <= report.recall <= 1.0


def test_precision_and_recall_have_different_denominators():
    """The bug this guards: measuring recall against what was predicted.

    That always reads 1.0 and hides every miss, which is exactly the failure
    an output-sampled gold set would have baked in.
    """
    report = evaluate.run()
    assert report.predicted != report.expected, (
        "the shipped fixture should have more expected moves than predicted; "
        "if these are equal the test can no longer tell the denominators apart"
    )
    assert report.precision != report.recall


def test_the_gold_set_contains_records_that_report_no_move():
    """Without negatives, a false positive is invisible to the metric."""
    report = evaluate.run()
    assert report.negatives > 0


def test_matching_is_insensitive_to_name_ordering():
    """The match key is the one dedupe blocks on, so a surname-first spelling
    and its Western ordering are the same person."""
    assert evaluate._person_key("Tan Wei Ming") == evaluate._person_key("Wei Ming Tan")
    assert evaluate._person_key("Min-jun Kim") == evaluate._person_key("Minjun Kim")


def test_two_different_people_do_not_share_a_match_key():
    assert evaluate._person_key("Sarah Chen") != evaluate._person_key("Michael Chen")


def test_firms_are_compared_through_the_gazetteer():
    """The fixture stores the surface the article used; the extractor emits
    the canonical name. Scoring those as disagreement would understate every
    firm field."""
    assert evaluate._same_firm(GAZETTEER, "Allen & Overy", "Allen & Overy Shearman")
    assert evaluate._same_firm(GAZETTEER, "kl gates", "K&L Gates")
    assert not evaluate._same_firm(GAZETTEER, "Clyde & Co", "Mills Oakley")


def test_a_null_on_one_side_is_not_firm_agreement():
    assert not evaluate._same_firm(GAZETTEER, "Clyde & Co", None)
    assert evaluate._same_firm(GAZETTEER, None, None)


def test_a_field_the_extractor_left_null_counts_as_compared_not_agreed():
    """Silence is a miss, not a pass. Counting it as agreement would let an
    extractor that reads nothing score 1.0 on every field."""
    score = evaluate.FieldScore(agreed=3, compared=5, missed=2)
    assert score.accuracy == 0.6


def test_an_empty_field_score_does_not_divide_by_zero():
    assert evaluate.FieldScore().accuracy == 0.0


def test_the_report_renders_without_a_database():
    text = evaluate.render(evaluate.run())
    assert "precision" in text
    assert "recall" in text
