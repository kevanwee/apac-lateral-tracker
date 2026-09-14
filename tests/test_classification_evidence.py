"""Classification reads the best evidence available, and says which it used.

Measured motivation: with classification reading only the stated practice
clause, 195 of 285 stored moves (68%) were unclassified. A practice-group
trend over the remaining third is a chart of which articles happened to use
the word "practice". The headline names the practice far more often than it
names the person, and it is stored, so a headline-derived assignment is as
auditable as any other — it just deserves less confidence, and it gets less.
"""

from __future__ import annotations

import pytest

from tracker.extract import body_rules
from tracker.pipeline.extract import _jurisdiction_from_url
from tracker.taxonomy import EVIDENCE_WEIGHT, Taxonomy

TAXONOMY = Taxonomy.load()


def test_a_stated_practice_beats_the_headline():
    found = TAXONOMY.classify_from([
        ("stated", "white-collar defence and investigations"),
        ("headline", "Cooley grows capital markets with new partner"),
    ])
    assert found.primary == "disputes.white_collar"
    assert found.rule_key.startswith("stated:")
    assert found.confidence == pytest.approx(0.95 * EVIDENCE_WEIGHT["stated"])


def test_the_headline_is_used_when_nothing_was_stated():
    found = TAXONOMY.classify_from([
        ("stated", None),
        ("headline", "Cooley grows capital markets with new partner in Beijing"),
    ])
    assert found.primary == "finance.ecm"
    assert found.rule_key == "headline:mapping:capital markets"
    assert found.confidence < 0.95 * EVIDENCE_WEIGHT["stated"]


def test_body_sentences_are_the_last_resort_and_weakest():
    found = TAXONOMY.classify_from([
        ("stated", None),
        ("headline", "Rajah & Tann hires KNSAT founder Adirek Limsiriwong"),
        ("body", "Limsiriwong advises on equity capital markets transactions."),
    ])
    assert found.primary == "finance.ecm"
    assert found.rule_key.startswith("body:")
    assert found.confidence == pytest.approx(0.8 * EVIDENCE_WEIGHT["body"])


def test_no_evidence_is_honestly_unclassified():
    nothing_matched = TAXONOMY.classify_from([
        ("stated", None),
        ("headline", "Rajah & Tann hires KNSAT founder Adirek Limsiriwong"),
    ])
    assert nothing_matched.primary == "unclassified"
    assert nothing_matched.rule_key == "no_evidence_matched"

    nothing_at_all = TAXONOMY.classify_from([("stated", None), ("headline", None)])
    assert nothing_at_all.rule_key == "no_practice_text"
    # Both are rule decisions, not model output. No model runs here.
    assert nothing_matched.assigned_by == "rule"
    assert nothing_at_all.assigned_by == "rule"


def test_a_non_practice_word_in_the_headline_does_not_stop_the_search():
    """"Firm names head of pro bono" describes the firm's week, not the hire."""
    found = TAXONOMY.classify_from([
        ("stated", None),
        ("headline", "Firm names head of pro bono"),
        ("body", "She leads the disputes team."),
    ])
    assert found.primary == "disputes"
    assert found.rule_key.startswith("body:")


def test_a_non_practice_stated_clause_is_decisive():
    found = TAXONOMY.classify_from([
        ("stated", "pro bono"),
        ("headline", "Firm hires disputes partner"),
    ])
    assert found.primary == "unclassified"
    assert found.rule_key == "stated:not_a_practice:pro bono"


def test_bare_practice_words_classify_to_the_parent_not_a_guessed_child():
    """Under 1.0.0 every "corporate partner" was filed as Corporate Governance."""
    assert TAXONOMY.classify("corporate").primary == "corporate"
    assert TAXONOMY.classify("litigation").primary == "disputes"
    assert TAXONOMY.classify("tax").primary == "tax"
    assert TAXONOMY.classify("compliance").primary == "regulatory"
    # A specific phrase still reaches the child.
    assert TAXONOMY.classify("corporate governance").primary == "corporate.governance"
    assert TAXONOMY.classify("commercial litigation").primary == "disputes.commercial_litigation"


def test_the_matched_phrase_is_recoverable_for_provenance():
    assert TAXONOMY.matched_phrase("Cooley grows capital markets with new partner") == (
        "capital markets"
    )
    assert TAXONOMY.matched_phrase("Rajah & Tann hires KNSAT founder") is None


# ---------------------------------------------------------------------------
# Scoping body evidence to the person
# ---------------------------------------------------------------------------

BODY = (
    "Dorsey has hired Carlton Ng as of counsel. Rachel Han, a capital markets "
    "partner, said it was good. Ng began his career at Clifford Chance."
)


def test_body_evidence_is_only_the_sentences_about_this_person():
    about_ng = body_rules.sentences_about("Carlton Ng", BODY)
    assert "hired Carlton Ng" in about_ng
    assert "began his career" in about_ng  # surname-only mention counts
    assert "Rachel Han" not in about_ng


def test_a_person_the_body_never_mentions_yields_nothing():
    assert body_rules.sentences_about("Nobody Here", BODY) is None


def test_hired_as_is_a_body_template():
    """"has hired Carlton Ng as of counsel" produced no record before 2.1.0."""
    from tracker.firms import FirmGazetteer

    headline = "Dorsey hires corporate of counsel Carlton Ng"
    hits = body_rules.find(BODY, headline, FirmGazetteer.load())
    assert [h.person for h in hits] == ["Carlton Ng"]
    assert hits[0].title == "of counsel"


# ---------------------------------------------------------------------------
# Jurisdiction from the URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://law.asia/dorsey-carlton-ng-hong-kong/", "HK"),
        ("https://law.asia/dla-piper-jake-robson-singapore/", "SG"),
        ("https://law.asia/squire-patton-boggs-scott-crabb-perth/", "AU-WA"),
    ],
)
def test_an_entity_slug_names_the_office(url, code):
    found = _jurisdiction_from_url(url)
    assert found is not None
    assert found[0] == code
    # The slug itself is the recorded evidence.
    assert found[1]


def test_a_slug_without_a_place_yields_nothing():
    assert _jurisdiction_from_url("https://law.asia/rajah-tann-knsat-adirek-limsiriwong/") is None
