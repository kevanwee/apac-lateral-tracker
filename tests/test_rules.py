"""Rule-based extraction.

Every headline in the "must abstain" set produced a wrong record at some point
during development. They are kept as regressions because the whole value of
this extractor is that it says nothing when it is unsure — a template that
starts guessing is worse than no template at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tracker.extract.rules import CascadingExtractor, RuleExtractor
from tracker.firms import FirmGazetteer, normalise
from tracker.sources.base import RawItem

GAZETTEER = FirmGazetteer.load()


def item(headline: str, *, access: str = "headline_only", body: str | None = None) -> RawItem:
    return RawItem(
        source_slug="test",
        url=f"https://example.test/{abs(hash(headline))}",
        headline=headline,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        access_level=access,
        body_text=body,
    )


def extract(headline: str, *, tier: int = 2, **kw):
    return RuleExtractor(gazetteer=GAZETTEER).extract(
        item(headline, **kw), reliability_tier=tier
    )


# ---------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("surface", "canonical"),
    [
        ("Herbert Smith Freehills", "Herbert Smith Freehills Kramer"),
        ("herbert smith freehills", "Herbert Smith Freehills Kramer"),
        ("HSF", "Herbert Smith Freehills Kramer"),
        ("dla pipers", "DLA Piper"),
        ("minterellison", "MinterEllison"),
        ("Minter Ellison", "MinterEllison"),
        ("drew and napier", "Drew & Napier"),
        ("Drew & Napier", "Drew & Napier"),
        ("AHP", "Assegaf Hamzah & Partners"),
    ],
)
def test_the_gazetteer_resolves_real_surface_forms(surface, canonical):
    """Slugs lose capitalisation and ampersands; aliases carry the rest."""
    assert GAZETTEER.resolve(surface) == canonical


def test_an_unknown_firm_resolves_to_nothing_rather_than_a_guess():
    assert GAZETTEER.resolve("Some Firm That Does Not Exist") is None


def test_normalisation_folds_ampersands_and_punctuation():
    assert normalise("Drew & Napier") == normalise("drew and napier")
    assert normalise("Clyde & Co.") == normalise("clyde co")


def test_a_longer_firm_name_wins_over_a_shorter_prefix():
    assert GAZETTEER.resolve("Rajah & Tann Singapore") == "Rajah & Tann Singapore"
    assert GAZETTEER.resolve("Rajah & Tann") == "Rajah & Tann Asia"


# ---------------------------------------------------------------------------
# Records the rules should produce
# ---------------------------------------------------------------------------

SHOULD_EXTRACT = [
    (
        "Herbert smith freehills appoints nick baker as managing partner",
        "nick baker", "Herbert Smith Freehills Kramer", None,
    ),
    (
        "Jenny thornton rejoins clyde co as managing partner in perth office",
        "Jenny thornton", "Clyde & Co", None,
    ),
    (
        "Kirkland ellis welcomes laura vartain horn as partner in intellectual "
        "property practice group",
        "laura vartain horn", "Kirkland & Ellis", None,
    ),
    (
        "Baker mckenzie appoints oanh nguyen as managing partner in vietnam",
        "oanh nguyen", "Baker McKenzie", None,
    ),
    (
        "Moray agnew promotes kate cooch to partner in health law group",
        "kate cooch", "Moray & Agnew", "Moray & Agnew",
    ),
    (
        "Gavin rakoczy joins allens as banking and finance partner",
        "Gavin rakoczy", "Allens", None,
    ),
    (
        "AHP Welcomes New Intellectual Property Partner Wiku Anindito",
        "Wiku Anindito", "Assegaf Hamzah & Partners", None,
    ),
    (
        "Scott Tan joins Drew & Napier from Allen & Gledhill",
        "Scott Tan", "Drew & Napier", "Allen & Gledhill",
    ),
]


@pytest.mark.parametrize(
    ("headline", "person", "to_firm", "from_firm"),
    SHOULD_EXTRACT,
    ids=[h[:44] for h, *_ in SHOULD_EXTRACT],
)
def test_clear_headlines_produce_the_right_record(headline, person, to_firm, from_firm):
    result = extract(headline)
    assert result.moves, "expected a record"
    move = result.moves[0]
    assert move.value("person_name") == person
    assert move.value("to_firm") == to_firm
    assert move.value("from_firm") == from_firm


# ---------------------------------------------------------------------------
# Headlines the rules must refuse
# ---------------------------------------------------------------------------

MUST_ABSTAIN = [
    # Each of these produced a wrong record during development.
    ("Qic gc joins hsf as executive counsel", "a role abbreviation read as a name"),
    (
        "Squire patton boggs welcomes funds private equity partner in london",
        "a prepositional phrase read as a name",
    ),
    (
        "Minterellison promotes even dozen to partner in massive round",
        "a quantity read as a name",
    ),
    # Epithets standing in for a name. All three came out of the first real
    # collection run over 8,083 live items.
    (
        "Jws appoints corrs ip star as new partner",
        "a practice-area epithet read as a name",
    ),
    (
        "Holding redlich appoints seasoned investment funds star as new partner",
        "a descriptive epithet read as a name",
    ),
    (
        "Private equity experts re joins clifford chance as partner in london",
        "a practice description read as a name",
    ),
    (
        "Ex safework nsw prosecutor joins macpherson kelley as new principal lawyer",
        "a regulator epithet read as a name; came out of the first database run",
    ),
    # Not a partner-level appointment.
    (
        "Rajah & Tann appoints Clarisse Girot as advisor to data privacy practice",
        "an advisory appointment, not a partner move",
    ),
    # No person is named at all.
    ("Seven new partners join minterellison", "no individual named"),
    (
        "Pinsent Masons adds IP partner duo in Germany from Vossius",
        "a duo, neither named",
    ),
    (
        "Linklaters lures Wachtell dealmaker to become Americas managing partner",
        "the person is only in the standfirst",
    ),
    # The destination firm is not one we know.
    (
        "Jane Doe joins Some Unknown Firm as partner",
        "an unrecognised firm would have to be guessed",
    ),
    # Not movement at all.
    ("Full federal court rejects ex directors bid for total tools shares", "a case report"),
    ("Rajah & Tann marks 50 years with donations to two law schools", "not movement"),
]


@pytest.mark.parametrize(
    ("headline", "reason"), MUST_ABSTAIN, ids=[h[:44] for h, _ in MUST_ABSTAIN]
)
def test_ambiguous_headlines_produce_nothing(headline, reason):
    result = extract(headline)
    assert not result.moves, f"should have abstained: {reason}"


# ---------------------------------------------------------------------------
# Properties that hold whatever the template
# ---------------------------------------------------------------------------


def _accepts(raw: str) -> bool:
    """The person slot exactly as the extractor composes it."""
    from tracker.extract.rules import (
        _dedupe_repeated_name,
        _looks_like_a_person,
        _strip_honorific,
        strip_leading_noise,
    )

    candidate = _dedupe_repeated_name(
        strip_leading_noise(_strip_honorific(raw))
    )
    return _looks_like_a_person(candidate, FirmGazetteer.load())


NOT_PEOPLE = [
    # Site furniture read out of an article body, observed in the ABLJ backfill.
    ("Most Popular Malaysia", "a section heading, not a name"),
    ("Deal Highlights Firms", "a section heading, not a name"),
    # An organisation that the person slot swallowed.
    ("HP India", "a company, not a person"),
    ("Kennedys Hong Kong", "a firm and its office"),
    ("KPMG Australia", "a firm and its country"),
    ("OpenAI secondee", "a role, not a name"),
]


@pytest.mark.parametrize(
    ("candidate", "reason"), NOT_PEOPLE, ids=[c for c, _ in NOT_PEOPLE]
)
def test_things_that_are_not_people_are_rejected(candidate, reason):
    assert not _accepts(candidate), reason


RECOVERABLE = [
    # A real person with a stray token in front. Trimming beats rejecting:
    # these are correct records that the first version of the nav filter lost.
    ("Share David Nisbet", "David Nisbet"),
    ("Share Emma Liu", "Emma Liu"),
    ("Brisbane Helen Clarke", "Helen Clarke"),
    ("Hiral Motta Hiral Motta", "Hiral Motta"),
    ("Dr Clarisse Girot", "Clarisse Girot"),
    ("Nick Baker", "Nick Baker"),
]


@pytest.mark.parametrize(("raw", "expected"), RECOVERABLE, ids=[r for r, _ in RECOVERABLE])
def test_a_name_behind_noise_is_recovered_not_discarded(raw, expected):
    from tracker.extract.rules import (
        _dedupe_repeated_name,
        _strip_honorific,
        strip_leading_noise,
    )

    cleaned = _dedupe_repeated_name(strip_leading_noise(_strip_honorific(raw)))
    assert cleaned == expected
    assert _accepts(raw)


def test_a_genuine_four_part_name_is_not_halved():
    from tracker.extract.rules import _dedupe_repeated_name

    assert _dedupe_repeated_name("Maria Elena Santos Cruz") == "Maria Elena Santos Cruz"


@pytest.mark.xfail(
    reason="a surname that is also a country is indistinguishable from an "
           "organisation without a given-name gazetteer (Phase 4)",
    strict=True,
)
def test_a_surname_that_is_a_country_is_a_known_false_negative():
    assert _accepts("matt spain")


def test_an_honorific_is_not_part_of_the_name():
    move = extract(
        "Dentons appoints Dr Clarisse Girot as global head of data privacy"
    ).moves[0]
    assert move.value("person_name") == "Clarisse Girot"


def test_extraction_is_free():
    result = extract("Herbert smith freehills appoints nick baker as managing partner")
    assert result.cost_usd == 0.0
    assert result.input_tokens == 0
    assert result.model.startswith("rules/")


def test_every_field_carries_a_span_into_the_headline():
    headline = "Scott Tan joins Drew & Napier from Allen & Gledhill"
    move = extract(headline).moves[0]
    for name, field in move.fields.items():
        assert 0 <= field.span_start < field.span_end <= len(headline), name
        assert field.excerpt
        assert len(field.excerpt.split()) <= 25, name


def test_a_promotion_keeps_both_ends_at_the_same_firm():
    """The schema rejects a promotion across two firms."""
    move = extract("Moray agnew promotes kate cooch to partner in health law group").moves[0]
    assert move.value("move_type") == "promotion"
    assert move.value("from_firm") == move.value("to_firm")


def test_rule_records_still_need_review_when_the_evidence_is_thin():
    """A headline-only record never auto-accepts, however clean the parse."""
    move = extract(
        "Herbert smith freehills appoints nick baker as managing partner",
        access="headline_only",
    ).moves[0]
    assert move.needs_review


def test_results_are_deterministic():
    headline = "Baker mckenzie appoints oanh nguyen as managing partner in vietnam"
    first, second = extract(headline), extract(headline)
    assert first.moves[0].value("person_name") == second.moves[0].value("person_name")
    assert first.moves[0].confidence.total == second.moves[0].confidence.total


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------


class SpyFallback:
    model = "spy"

    def __init__(self):
        self.calls = 0

    def extract(self, item, *, reliability_tier):
        from tracker.extract.extractor import ExtractionResult

        self.calls += 1
        return ExtractionResult(
            item=item.without_text(), is_movement=False,
            not_movement_reason="spy", model="spy",
        )


def test_the_model_is_never_asked_about_an_item_the_rules_answered():
    spy = SpyFallback()
    cascade = CascadingExtractor(
        rules=RuleExtractor(gazetteer=GAZETTEER), fallback=spy
    )
    result = cascade.extract(
        item("Herbert smith freehills appoints nick baker as managing partner"),
        reliability_tier=2,
    )
    assert result.moves
    assert spy.calls == 0


def test_the_model_is_asked_about_everything_the_rules_declined():
    spy = SpyFallback()
    cascade = CascadingExtractor(
        rules=RuleExtractor(gazetteer=GAZETTEER), fallback=spy
    )
    cascade.extract(item("Seven new partners join minterellison"), reliability_tier=2)
    assert spy.calls == 1


# ---------------------------------------------------------------------------
# Source hygiene
# ---------------------------------------------------------------------------


def test_no_source_file_contains_a_stray_control_character():
    """A regex `\b` written through a shell heredoc can land as \x08.

    That is a literal backspace, not a word boundary. It is invisible in an
    editor and in grep output, and it silently disables the anchor — which has
    already happened three times in this file and in body_rules.py, each time
    producing wrong records rather than an error.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in [*(root / "tracker").rglob("*.py"), *(root / "tests").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        bad = {c for c in text if ord(c) < 32 and c not in "\n\t"}
        if bad:
            offenders.append(f"{path.name}: {sorted(hex(ord(c)) for c in bad)}")
    assert not offenders, f"control characters in source: {offenders}"
