"""Both spellings of an ampersand, and the collisions that come with them.

A URL slug closes an ampersand up without leaving a gap, so "K&L Gates"
arrives as "kl-gates" and "A&O Shearman" as "ao-shearman". The gazetteer
indexed only the spaced form ("k l gates", "a o shearman"), so neither slug
resolved -- and an unresolved destination firm makes the extractor discard
the item without reading the body at all. Measured on the stored corpus, an
unresolved headline firm accounted for 656 of 1,278 rejected items.

The fix creates its own hazard, which is what most of this file is about.
"""

from __future__ import annotations

import pytest

from tracker.firms import FirmGazetteer, normalise, normalise_tight

GAZETTEER = FirmGazetteer.load()


@pytest.mark.parametrize(
    "surface",
    [
        "A&O Shearman", "ao shearman", "a o shearman",
        "Allen & Overy", "allen overy",
        "Gilbert + Tobin", "gilbert tobin",
        "Herbert Smith Freehills", "hsf kramer",
        "Rajah & Tann", "rajah tann",
    ],
)
def test_a_firm_resolves_whichever_way_the_ampersand_is_written(surface):
    assert GAZETTEER.resolve(surface) is not None


@pytest.mark.parametrize(
    "text",
    [
        # Closing up the ampersand turns "S&C" into `sc`, which is the Senior
        # Counsel post-nominal and appears throughout this corpus. Likewise
        # "A&G" -> `ag` (Attorney-General), "A&O" -> `ao` (Order of
        # Australia), "R&T" -> `rt` ("Rt Hon"). Word-boundary checking cannot
        # help here: these are standalone tokens, not substrings, so the guard
        # has to be that the surface is never indexed in the first place.
        "Michael Robertson SC has been appointed",
        "Ng Jern-Fei KC and Andrew Lee SC advised on the matter",
        "The AG referred the matter to the tribunal",
        "Jane Smith AO was awarded the honour",
        "She was appointed RT Hon last year",
    ],
)
def test_an_honorific_is_never_read_as_a_firm(text):
    assert GAZETTEER.find(text) == []


def test_a_real_firm_in_the_same_sentence_as_an_honorific_still_matches():
    """The guard must not cost a genuine mention."""
    found = {m.canonical_name for m in GAZETTEER.find("Wei Ming Tan SC joins Drew & Napier")}
    assert "Drew & Napier" in found
    assert "Sullivan & Cromwell" not in found


def test_no_new_two_letter_surface_was_introduced():
    """Pins the hazard. Two-letter surfaces already in the gazetteer are
    deliberate aliases; the ampersand-closing rule must not add more."""
    two_letter = {s for s in GAZETTEER._index if len(s) <= 2}
    assert two_letter == {"cc", "ey", "wp"}


@pytest.mark.parametrize(
    "surface, spaced, tight",
    [
        ("A&O Shearman", "a o shearman", "ao shearman"),
        ("K&L Gates", "k l gates", "kl gates"),
        ("Gilbert + Tobin", "gilbert tobin", "gilberttobin"),
    ],
)
def test_the_two_normalisations_differ_exactly_at_the_ampersand(surface, spaced, tight):
    assert normalise(surface) == spaced
    assert normalise_tight(surface) == tight
