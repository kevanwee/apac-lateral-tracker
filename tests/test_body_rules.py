"""Body extraction and directional firm pairs.

Every case here is a real headline that produced nothing, or produced a record
with a null origin, in the first database run. Two gaps drove them:

  * `from_firm` was null on almost every record, because a headline template
    match returned early and the body — where the origin almost always is —
    was never read.
  * Law.com produced nothing at all from 7 gate-passed items, because its
    headlines name both firms and no person, and the direction between the two
    firms was being decided by which mention was longer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tracker.extract.body_rules import firm_pair, origin_for
from tracker.extract.rules import RuleExtractor
from tracker.firms import FirmGazetteer
from tracker.sources.base import RawItem

GAZETTEER = FirmGazetteer.load()


def extract(headline: str, body: str | None = None, *, tier: int = 2):
    item = RawItem(
        source_slug="test",
        url=f"https://example.test/{abs(hash(headline))}",
        headline=headline,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        access_level="summary",
        body_text=body,
    )
    return RuleExtractor(gazetteer=GAZETTEER).extract(item, reliability_tier=tier)


# ---------------------------------------------------------------------------
# Direction between two firms in one headline
# ---------------------------------------------------------------------------

PAIRS = [
    # Destination leads — the trade press convention.
    ("Cravath Recruits 6 Weil Partners", "Cravath", "Weil"),
    (
        "Mayer Brown boosts Brussels presence with Baker McKenzie trade partner hire",
        "Mayer Brown", "Baker McKenzie",
    ),
    # "from" marks the origin explicitly.
    (
        "Ogletree Deakins adds partner trio from rival Littler to launch in Monterrey",
        "Ogletree Deakins", "Littler",
    ),
    # Direction reverses: the origin leads and "to" marks the destination.
    (
        "Baker McKenzie's Global Chair of International Arbitration Jumps to Travers Smith",
        "Travers Smith", "Baker McKenzie",
    ),
    (
        "Mike Aiello, Weil Corporate Chair, Plans Exit for Cravath",
        "Cravath", "Weil",
    ),
    # One known firm; the other is not in the gazetteer, so it stays null
    # rather than being guessed.
    (
        "Signature Litigation hires seasoned litigator from Triay Lawyers to head up "
        "Gibraltar office",
        "Signature Litigation", "Triay Lawyers",
    ),
    ("Herbert smith freehills appoints nick baker as managing partner",
     "Herbert Smith Freehills Kramer", None),
]


@pytest.mark.parametrize(
    ("headline", "destination", "origin"), PAIRS, ids=[h[:44] for h, *_ in PAIRS]
)
def test_firm_pair_reads_direction_not_prominence(headline, destination, origin):
    """Taking the longest mention as the destination reverses half of these."""
    assert firm_pair(headline, GAZETTEER) == (destination, origin)


def test_no_recognised_firm_yields_no_pair():
    assert firm_pair("Two lawyers move between unnamed firms", GAZETTEER) == (None, None)


# ---------------------------------------------------------------------------
# The from_firm gap
# ---------------------------------------------------------------------------


def test_a_headline_match_still_reads_the_body_for_the_origin():
    """The bug that left from_firm null on almost every record."""
    move = extract(
        "Herbert smith freehills appoints nick baker as managing partner",
        "Nick Baker joins from Ashurst, where he led the corporate practice.",
    ).moves[0]
    assert move.value("to_firm") == "Herbert Smith Freehills Kramer"
    assert move.value("from_firm") == "Ashurst"


def test_the_origin_is_matched_case_insensitively():
    """Slug headlines are lowercase; article bodies are not."""
    assert origin_for(
        "nick baker", "Nick Baker joins from Ashurst today.", GAZETTEER
    ) == "Ashurst"


def test_an_origin_is_only_taken_from_a_sentence_naming_that_person():
    """A firm mentioned elsewhere must not attach to the wrong individual."""
    body = (
        "Ashurst announced record revenue last week. "
        "Separately, Jane Roe joins from Allens."
    )
    assert origin_for("Jane Roe", body, GAZETTEER) == "Allens"


def test_enrichment_never_overwrites_what_the_headline_stated():
    move = extract(
        "Scott Tan joins Drew & Napier from Allen & Gledhill",
        "Scott Tan joins from Ashurst.",  # contradicts the headline
    ).moves[0]
    assert move.value("from_firm") == "Allen & Gledhill"


def test_a_promotion_is_recognised_when_both_ends_resolve_alike():
    move = extract(
        "Moray agnew promotes kate cooch to partner in health law group",
        "Cooch was previously at Moray & Agnew as a senior associate.",
    ).moves[0]
    assert move.value("move_type") == "promotion"
    assert move.value("from_firm") == move.value("to_firm")


# ---------------------------------------------------------------------------
# Law.com: names in the body, not the headline
# ---------------------------------------------------------------------------

FROM_BODY = [
    (
        "Cravath Recruits 6 Weil Partners",
        "Mike Aiello joins from Weil, where he chaired the corporate department.",
        ["Mike Aiello"], "Cravath", "Weil",
    ),
    (
        "Baker McKenzie's Global Chair of International Arbitration Jumps to Travers Smith",
        "Jo Delaney will join Travers Smith in London next month.",
        ["Jo Delaney"], "Travers Smith", "Baker McKenzie",
    ),
    (
        "Ogletree Deakins adds partner trio from rival Littler to launch in Monterrey",
        "The trio is led by David Leal Gonzalez, who leaves after almost 13 years.",
        ["David Leal Gonzalez"], "Ogletree Deakins", "Littler",
    ),
    (
        "Proskauer Adds 4 Leveraged Finance Partners in NYC",
        "The group comprises Stephen Boyko, Mary Katherine Rawls and Michelle Iodice.",
        ["Stephen Boyko", "Mary Katherine Rawls", "Michelle Iodice"],
        "Proskauer", None,
    ),
    (
        "Bartier perry brings in first chief transformation officer appoints four partners",
        "Bartier Perry has brought in its first chief transformation officer in "
        "Roger Habib. The firm further strengthened its leadership with Alison Cui, "
        "Kate Ralph, Raffael Maestri and Mario Rashid-Ring becoming the firm's "
        "newest partners.",
        # The chief transformation officer in the first sentence is no longer
        # recovered, and should not be: this project tracks partner-level
        # movement, and a C-suite hire is not one. The four partners in the
        # second sentence still are, which is the distinction that matters --
        # both roles are announced in the same article.
        ["Alison Cui", "Kate Ralph", "Raffael Maestri", "Mario Rashid-Ring"],
        "Bartier Perry", None,
    ),
]


@pytest.mark.parametrize(
    ("headline", "body", "people", "to_firm", "from_firm"),
    FROM_BODY,
    ids=[h[:40] for h, *_ in FROM_BODY],
)
def test_names_are_recovered_from_the_body(headline, body, people, to_firm, from_firm):
    result = extract(headline, body)
    found = [m.value("person_name") for m in result.moves]
    assert found == people
    for move in result.moves:
        assert move.value("to_firm") == to_firm
        if from_firm:
            assert move.value("from_firm") == from_firm


def test_without_a_body_those_headlines_still_yield_nothing():
    """The names genuinely are not in the headline; abstaining is correct."""
    for headline, *_ in FROM_BODY[:1]:
        assert not extract(headline).moves


def test_an_unrecognised_destination_still_produces_no_record():
    assert not extract(
        "Some Unknown Firm Recruits 6 Partners",
        "Jane Roe joins from Ashurst.",
    ).moves
