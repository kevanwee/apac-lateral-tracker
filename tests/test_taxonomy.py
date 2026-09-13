"""Taxonomy loading, classification, place and role parsing.

The classification cases are every practice phrase the live corpus actually
produced, so this doubles as a record of what the mapping file has to cover.
"""

from __future__ import annotations

import pytest
import yaml

from tracker.extract.roles import parse as parse_role
from tracker.geo import PlaceGazetteer
from tracker.taxonomy import MAPPINGS_PATH, PRACTICE_PATH, Taxonomy, TaxonomyError

TAXONOMY = Taxonomy.load()
PLACES = PlaceGazetteer.load()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_the_taxonomy_loads_and_is_two_levels():
    assert TAXONOMY.version == "1.0.0"
    assert {n.level for n in TAXONOMY.practice_groups} == {1, 2}
    for node in TAXONOMY.practice_groups:
        if node.level == 2:
            assert node.parent and node.code.startswith(f"{node.parent}.")
        else:
            assert node.parent is None


def test_the_unclassified_sentinel_exists_and_is_top_level():
    """The exactly-one-primary-group invariant depends on it."""
    sentinel = [n for n in TAXONOMY.practice_groups if n.is_unclassified]
    assert len(sentinel) == 1
    assert sentinel[0].code == "unclassified"
    assert sentinel[0].level == 1


def test_the_brief_s_starter_taxonomy_is_present():
    codes = TAXONOMY.codes()
    for expected in [
        "corporate.m_and_a", "finance.banking", "finance.restructuring",
        "disputes.international_arbitration", "disputes.competition",
        "regulatory.data_protection", "regulatory.esg",
        "intellectual_property.patents", "real_assets.projects",
        "employment.employment_and_labour", "tax.transfer_pricing",
        "specialist.shipping_aviation", "specialist.private_client",
    ]:
        assert expected in codes, expected


def test_practice_groups_and_sectors_share_no_codes():
    """Two axes, never merged. A sector must not be assignable as a practice."""
    assert not TAXONOMY.codes() & {n.code for n in TAXONOMY.sectors}


def test_every_mapping_names_a_node_that_exists():
    """Loading enforces this; assert it so a bad edit fails loudly."""
    for primary, secondary in TAXONOMY._mappings.values():
        assert primary in TAXONOMY.codes()
        for code in secondary:
            assert code in TAXONOMY.codes()
            assert code != primary


def test_a_mapping_to_an_unknown_node_is_refused(tmp_path):
    bad = tmp_path / "mappings.yaml"
    bad.write_text(
        "version: 1.0.0\ntaxonomy_version: 1.0.0\n"
        "mappings:\n  - {match: nonsense, primary: does.not.exist}\n",
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError, match="unknown node"):
        Taxonomy.load(mappings_path=bad)


def test_a_mapping_file_targeting_another_version_is_refused(tmp_path):
    bad = tmp_path / "mappings.yaml"
    bad.write_text(
        "version: 9.9.9\ntaxonomy_version: 9.9.9\nmappings: []\n", encoding="utf-8"
    )
    with pytest.raises(TaxonomyError, match="targets"):
        Taxonomy.load(mappings_path=bad)


def test_a_taxonomy_without_the_sentinel_is_refused(tmp_path):
    raw = yaml.safe_load(PRACTICE_PATH.read_text(encoding="utf-8"))
    raw["groups"] = [g for g in raw["groups"] if not g.get("unclassified")]
    path = tmp_path / "practice_groups.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TaxonomyError, match="unclassified"):
        Taxonomy.load(practice_path=path)


def test_secondary_groups_are_capped_at_two(tmp_path):
    bad = tmp_path / "mappings.yaml"
    bad.write_text(
        "version: 1.0.0\ntaxonomy_version: 1.0.0\nmappings:\n"
        "  - match: too many\n    primary: corporate\n"
        "    secondary: [finance, disputes, tax]\n",
        encoding="utf-8",
    )
    with pytest.raises(TaxonomyError, match="secondary"):
        Taxonomy.load(mappings_path=bad)


# ---------------------------------------------------------------------------
# Classification — every phrase the live corpus produced
# ---------------------------------------------------------------------------

CASES = [
    ("International Arbitration", "disputes.international_arbitration", ()),
    ("intellectual property", "intellectual_property", ()),
    ("white-collar defence and investigations", "disputes.white_collar", ()),
    ("antitrust and competition", "disputes.competition", ()),
    ("investment funds", "finance.funds", ()),
    ("transactional tax", "tax.corporate_tax", ()),
    ("projects and government commercial", "real_assets.projects", ()),
    ("banking and finance", "finance.banking", ()),
    ("employment and safety", "employment.employment_and_labour", ()),
    ("workplace advisory", "employment.employment_and_labour", ()),
    ("property", "real_assets.real_estate", ()),
    ("Fraud, Asset Recovery & Investigations", "disputes.white_collar", ()),
    ("licencing", "intellectual_property.tech_transactions", ()),
    ("private equity", "corporate.private_equity", ()),
    # Compounds that genuinely span two groups.
    ("finance and projects", "real_assets.projects", ("finance.banking",)),
    ("energy and infrastructure", "real_assets.energy", ("real_assets.projects",)),
    ("aviation and asset finance", "specialist.shipping_aviation", ("finance.banking",)),
    ("property litigation", "real_assets.real_estate", ("disputes.commercial_litigation",)),
]


@pytest.mark.parametrize(
    ("text", "primary", "secondary"), CASES, ids=[c[0][:36] for c in CASES]
)
def test_real_practice_text_classifies_by_rule(text, primary, secondary):
    """No model involved: these all resolve deterministically and for free."""
    assignment = TAXONOMY.classify(text)
    assert assignment.primary == primary
    assert assignment.secondary == secondary
    assert assignment.assigned_by == "rule"


def test_a_longer_phrase_beats_a_shorter_one_inside_it():
    assert TAXONOMY.classify("private equity").primary == "corporate.private_equity"
    assert TAXONOMY.classify("capital markets").primary == "finance.ecm"


def test_an_unknown_practice_lands_on_the_sentinel_not_on_nothing():
    """Unclassified records must stay in the denominator of every chart."""
    for text in ["something entirely unknown", "", None]:
        assert TAXONOMY.classify(text).primary == "unclassified"


def test_a_firm_wide_role_is_not_a_practice_area():
    for text in ["pro bono", "diversity and inclusion", "business development"]:
        assignment = TAXONOMY.classify(text)
        assert assignment.primary == "unclassified"
        # Deciding it is not a practice is a decision, not a failure.
        assert assignment.rule_key and assignment.rule_key.startswith("not_a_practice")


def test_the_mapping_file_is_the_growth_surface():
    """It should be substantial, and every entry should be reachable."""
    raw = yaml.safe_load(MAPPINGS_PATH.read_text(encoding="utf-8"))
    assert len(raw["mappings"]) > 100
    for entry in raw["mappings"]:
        assert TAXONOMY.classify(entry["match"]).primary == entry["primary"], entry


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

PLACE_CASES = [
    ("a partner in its Sydney office", "AU-NSW"),
    ("joins in Perth", "AU-WA"),
    ("a partner in hong kong", "HK"),
    ("managing partner in vietnam", "VN"),
    ("a partner in Bangkok", "TH"),
    ("Rajah & Tann Singapore welcomes a partner", "SG"),
    ("the firm's New York office", "US"),
    ("partner in Dusseldorf", "DE"),
    ("head of the Brisbane team", "AU-QLD"),
]


@pytest.mark.parametrize(("text", "code"), PLACE_CASES, ids=[c[0][:34] for c in PLACE_CASES])
def test_places_resolve_to_jurisdiction_codes(text, code):
    assert PLACES.find(text) == code


def test_a_region_is_not_mistaken_for_a_jurisdiction():
    """"Asia Pacific" and "EMEA" are regions, not markets we can filter on."""
    for text in ["head of Asia Pacific", "the EMEA practice", "across Southeast Asia"]:
        assert PLACES.find(text) is None


def test_hong_kong_is_never_read_as_a_shorter_match():
    assert PLACES.find("Hong Kong") == "HK"


# ---------------------------------------------------------------------------
# Role phrases — the title cleanup
# ---------------------------------------------------------------------------

ROLE_CASES = [
    ("a partner in its white-collar defence and investigations practice",
     "partner", "white-collar defence and investigations", None),
    ("the firm's New York office as a partner in the investment funds group",
     "partner", "investment funds", "US"),
    ("a partner on its antitrust and competition team in Sydney",
     "partner", "antitrust and competition", "AU-NSW"),
    ("new Managing Partner in planned leadership succession",
     "Managing Partner", None, None),
    ("a consulting principal in the firm's Sydney office",
     "consulting principal", None, "AU-NSW"),
    ("co-heads of Fraud, Asset Recovery & Investigations",
     "co-heads", "Fraud, Asset Recovery & Investigations", None),
    ("managing partner in vietnam", "managing partner", None, "VN"),
]


@pytest.mark.parametrize(
    ("phrase", "title", "practice", "jurisdiction"),
    ROLE_CASES,
    ids=[c[0][:40] for c in ROLE_CASES],
)
def test_a_role_clause_splits_into_three_facts(phrase, title, practice, jurisdiction):
    """Stored whole it is a bad title and two missing fields."""
    parts = parse_role(phrase, PLACES)
    assert parts.title == title
    assert parts.practice == practice
    assert parts.jurisdiction == jurisdiction


def test_a_region_wearing_the_word_practice_is_not_a_practice():
    assert parse_role("co-head of South Asia Practice", PLACES).practice is None


def test_every_parsed_practice_classifies_or_is_honestly_unclassified():
    """The two layers have to agree: a parsed practice must be mappable."""
    for phrase, _title, practice, _juris in ROLE_CASES:
        if practice:
            assignment = TAXONOMY.classify(practice)
            assert assignment.primary, phrase
