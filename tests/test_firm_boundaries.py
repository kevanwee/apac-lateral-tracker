"""Firm matching must respect word boundaries on *every* surface.

These are regression tests for a single missing non-capturing group in
`FirmGazetteer.build`. The pattern was assembled as

    (?<!\\w)surface1|surface2|...|surfaceN(?!\\w)

and alternation binds looser than concatenation, so Python read that as

    ((?<!\\w)surface1) | (surface2) | ... | (surfaceN(?!\\w))

Only the first and last surface were boundary-checked. The other ~1,700 —
including every short alias — matched anywhere they appeared as a substring.

The damage was concentrated in the two-letter aliases, because normalisation
folds "&" and "+" to a space: "A&G" becomes "a g", which then matched inside
"Mide-a G-roup". An audit of the Asia Business Law Journal backfill found this
to be the largest single cause of wrong destination firms.
"""

from __future__ import annotations

import pytest

from tracker.extract import body_rules
from tracker.firms import FirmGazetteer


@pytest.fixture(scope="module")
def gazetteer() -> FirmGazetteer:
    return FirmGazetteer.load()


# (text, alias that used to match inside it)
SUBSTRING_TRAPS = [
    ("Cooley grows capital markets with new partner", "EY"),
    ("PDLegal adds partner in Sydney", "EY"),
    ("Dorsey hires corporate of counsel Carlton Ng in HK office", "EY"),
    ("Midea Group appoints Gao Huandong as general counsel", "Allen & Gledhill"),
    ("Chang Tsi & Partners adds IP partner", "Gilbert + Tobin"),
    ("Cooley grows capital markets with new partner", "Sullivan & Cromwell"),
]


@pytest.mark.parametrize("text,phantom", SUBSTRING_TRAPS)
def test_alias_does_not_match_inside_a_word(text, phantom, gazetteer):
    found = {m.canonical_name for m in gazetteer.find(text)}
    assert phantom not in found, (
        f"{phantom!r} matched as a substring of {text!r}; the alternation in "
        f"FirmGazetteer.build has lost its (?:...) group"
    )


def test_every_surface_is_boundary_checked(gazetteer):
    """Not just the ends of the alternation — all of it.

    Pads each known surface into the middle of a longer word and asserts it
    does not match. This is the general form of the bug, so it catches a
    regression on any alias, not only the ones already observed failing.
    """
    offenders = []
    for surface in gazetteer._index:
        if not surface.isalpha() or len(surface) > 6:
            continue  # multi-word and long surfaces cannot hide inside a word
        padded = f"zz{surface}zz"
        if gazetteer.find(padded):
            offenders.append(surface)
    assert not offenders, f"matched inside a word: {sorted(offenders)[:20]}"


def test_real_firm_names_still_match(gazetteer):
    """The boundary fix must not cost recall on the names that matter."""
    cases = [
        ("Rajah & Tann hires KNSAT founder Adirek Limsiriwong", "Rajah & Tann Asia"),
        ("White & Case hires two former Broadfield disputes partners", "White & Case"),
        ("Cooley grows capital markets with new partner", "Cooley"),
        ("Dorsey hires corporate of counsel Carlton Ng", "Dorsey & Whitney"),
        ("EY Law expands its Singapore practice", "EY"),
    ]
    for text, expected in cases:
        found = {m.canonical_name for m in gazetteer.find(text)}
        assert expected in found, f"{expected!r} no longer found in {text!r}"


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def test_former_is_an_origin_cue(gazetteer):
    """"Former <Firm> ... rejoins <Firm2>" was recorded backwards.

    The headline contains no "from" and `\\bjoins` does not match inside
    "rejoins", so neither firm carried a cue and the leads-with-the-hirer
    fallback picked the firm the person was *leaving*.
    """
    destination, origin = body_rules.firm_pair(
        "Former Norton Rose maritime practice head rejoins WFW", gazetteer
    )
    assert destination == "Watson Farley & Williams"
    assert origin == "Norton Rose Fulbright"


def test_hiring_firm_still_leads_by_default(gazetteer):
    destination, origin = body_rules.firm_pair(
        "Ogletree Deakins adds partner trio from rival Littler", gazetteer
    )
    assert destination == "Ogletree Deakins"
    assert origin == "Littler"


def test_unknown_firm_abstains_rather_than_guessing(gazetteer):
    """No recognised firm means no record, never a firm borrowed from nearby."""
    assert body_rules.firm_pair(
        "Midea Group appoints Gao Huandong as general counsel", gazetteer
    ) == (None, None)
