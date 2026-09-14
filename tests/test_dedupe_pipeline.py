"""Phase 4 against the database.

The scoring rules are tested in test_dedupe.py. What is tested here is what a
merge does to stored rows: that it creates rather than overwrites, that nothing
a losing row said is thrown away, and that the schema's own invariants still
hold afterwards.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tests.conftest import make_firm, make_raw_item, make_source
from tracker.pipeline import dedupe as stage

COMPONENTS = {
    "tier_base": 0.88, "access_factor": 1.0, "completeness": 0.5,
    "span_quality": 1.0, "self_reported": 0.9, "dropped_fields": 0,
    "dropped_penalty": 0.0, "corroboration_count": 1,
    "corroboration_bonus": 0.0, "total": 0.905, "forced_review_reason": None,
}


def person(conn, name: str, surname: str, variants=None):
    return conn.execute(
        "INSERT INTO people (canonical_name, surname_normalised, name_variants) "
        "VALUES (%s, %s, %s) RETURNING id",
        (name, surname, variants or []),
    ).fetchone()["id"]


def reported(conn, *, person_id, to_firm_id, source_id, announced,
             from_firm_id=None, jurisdiction="SG", title_to=None,
             confidence="0.905", tier_components=None):
    """One move as one outlet reported it, with its source attached."""
    move_id = conn.execute(
        """
        INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type,
                           announced_date, confidence, confidence_components,
                           office_jurisdiction, title_to, review_state,
                           extraction_fingerprint)
        VALUES (%s, %s, %s, 'lateral', %s, %s, %s, %s, %s, 'auto_accepted', %s)
        RETURNING id
        """,
        (person_id, from_firm_id, to_firm_id, announced, confidence,
         json.dumps(tier_components or COMPONENTS), jurisdiction, title_to,
         f"fp-{conn.execute('SELECT gen_random_uuid()').fetchone()['gen_random_uuid']}"),
    ).fetchone()["id"]
    item_id = make_raw_item(conn, source_id)
    conn.execute(
        "INSERT INTO move_sources (move_id, raw_item_id, is_primary) "
        "VALUES (%s, %s, true)",
        (move_id, item_id),
    )
    return move_id


@pytest.fixture
def two_reports(clean_conn):
    """The same hire, covered by two different outlets a week apart."""
    conn = clean_conn
    firm = make_firm(conn, "Allen & Gledhill")
    origin = make_firm(conn, "Rajah & Tann")
    p = person(conn, "Sarah Chen", "chen")
    a = reported(conn, person_id=p, to_firm_id=firm, from_firm_id=origin,
                 source_id=make_source(conn, slug="outlet-a", reliability_tier=2),
                 announced=date(2026, 3, 1), title_to="Partner")
    b = reported(conn, person_id=p, to_firm_id=firm, from_firm_id=None,
                 source_id=make_source(conn, slug="outlet-b", reliability_tier=2),
                 announced=date(2026, 3, 8))
    conn.commit()
    return conn, a, b, firm, origin


# --------------------------------------------------------------------------
# Reporting before writing
# --------------------------------------------------------------------------

def test_a_dry_run_writes_nothing(two_reports):
    conn, a, b, _, _ = two_reports
    stats = stage.run(conn, apply=False)

    assert stats["merged"] == 1
    assert stats["applied"] is False
    assert conn.execute("SELECT count(*) AS n FROM moves").fetchone()["n"] == 2
    assert conn.execute(
        "SELECT count(*) AS n FROM moves WHERE superseded_by_move_id IS NOT NULL"
    ).fetchone()["n"] == 0


def test_the_dry_run_reports_what_apply_does(two_reports):
    conn, _, _, _, _ = two_reports
    dry = stage.run(conn, apply=False)
    applied = stage.run(conn, apply=True)
    for key in ("merged", "queued", "moves_superseded"):
        assert dry[key] == applied[key], key


# --------------------------------------------------------------------------
# What a merge does
# --------------------------------------------------------------------------

def test_a_merge_creates_a_new_row_and_supersedes_the_inputs(two_reports):
    conn, a, b, _, _ = two_reports
    stage.run(conn, apply=True)

    rows = conn.execute(
        "SELECT id, review_state, superseded_by_move_id, merged_at, "
        "extraction_fingerprint FROM moves ORDER BY created_at"
    ).fetchall()
    assert len(rows) == 3, "the inputs are kept, not edited into each other"

    inputs = [r for r in rows if r["id"] in (a, b)]
    assert all(r["review_state"] == "superseded" for r in inputs)
    assert all(r["merged_at"] is not None for r in inputs)
    assert len({r["superseded_by_move_id"] for r in inputs}) == 1

    canonical = [r for r in rows if r["id"] not in (a, b)][0]
    assert canonical["superseded_by_move_id"] is None
    assert canonical["extraction_fingerprint"] is None, "a merge has no single item"


def test_only_the_merged_row_is_canonical_afterwards(two_reports):
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    assert conn.execute("SELECT count(*) AS n FROM canonical_moves").fetchone()["n"] == 1


def test_the_merged_row_keeps_the_earliest_announcement(two_reports):
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    got = conn.execute("SELECT announced_date FROM canonical_moves").fetchone()
    assert got["announced_date"] == date(2026, 3, 1)


def test_a_field_only_one_outlet_stated_survives_the_merge(two_reports):
    """The origin firm came from one report and null from the other. Null does
    not win a contest it never entered."""
    conn, _, _, _, origin = two_reports
    stage.run(conn, apply=True)
    got = conn.execute(
        "SELECT from_firm_id, title_to FROM canonical_moves").fetchone()
    assert got["from_firm_id"] == origin
    assert got["title_to"] == "Partner"


def test_every_source_of_every_input_becomes_a_source_of_the_merged_row(two_reports):
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    got = conn.execute(
        """
        SELECT count(*) AS n, count(*) FILTER (WHERE is_primary) AS primaries
        FROM move_sources ms JOIN canonical_moves m ON m.id = ms.move_id
        """
    ).fetchone()
    assert got["n"] == 2
    assert got["primaries"] == 1, "the unique index allows exactly one"


def test_corroboration_raises_the_confidence_of_the_merged_row(two_reports):
    conn, a, _, _, _ = two_reports
    before = conn.execute(
        "SELECT confidence FROM moves WHERE id = %s", (a,)).fetchone()["confidence"]
    stage.run(conn, apply=True)
    after = conn.execute("SELECT confidence, confidence_components "
                         "FROM canonical_moves").fetchone()
    assert after["confidence"] > before
    assert after["confidence_components"]["corroboration_count"] == 2


def test_a_second_spelling_is_added_to_the_person(clean_conn):
    conn = clean_conn
    firm = make_firm(conn)
    p = person(conn, "Wei Ming Tan", "tan", variants=["Wei Ming Tan"])
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 3, 1),
             source_id=make_source(conn, slug="outlet-a"))
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 3, 2),
             source_id=make_source(conn, slug="outlet-b"))
    conn.commit()
    conn.execute("UPDATE people SET name_variants = %s WHERE id = %s",
                 (["Tan Wei Ming"], p))
    conn.commit()

    stage.run(conn, apply=True)
    got = conn.execute(
        "SELECT name_variants FROM people WHERE id = %s", (p,)).fetchone()
    assert "Tan Wei Ming" in got["name_variants"]
    assert "Wei Ming Tan" in got["name_variants"]


# --------------------------------------------------------------------------
# Disagreement is kept, not settled
# --------------------------------------------------------------------------

def test_a_losing_value_is_kept_in_field_conflicts(clean_conn):
    """Two outlets disagree about the office. The pair still matches the same
    person, so it is not distinct; the merged row records both."""
    conn = clean_conn
    firm = make_firm(conn)
    p = person(conn, "Sarah Chen", "chen")
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 3, 1),
             jurisdiction="SG", title_to="Partner",
             source_id=make_source(conn, slug="tier1", reliability_tier=1,
                                   firm_id=firm))
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 3, 2),
             jurisdiction="SG", title_to="Managing Partner",
             source_id=make_source(conn, slug="tier2", reliability_tier=2))
    conn.commit()

    stage.run(conn, apply=True)
    got = conn.execute(
        "SELECT title_to, field_conflicts, review_state FROM canonical_moves"
    ).fetchone()

    assert got["title_to"] == "Partner", "tier 1 outranks tier 2"
    assert got["field_conflicts"]["title_to"][0]["value"] == "Managing Partner"
    assert got["review_state"] == "pending_review", "a contested field needs a human"


def test_an_undisclosed_partner_tier_is_not_treated_as_a_disagreement(two_reports):
    """partner_tier is NOT NULL with a default, so silence looks like a value."""
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    got = conn.execute("SELECT field_conflicts FROM canonical_moves").fetchone()
    assert "partner_tier" not in got["field_conflicts"]


# --------------------------------------------------------------------------
# What must not merge
# --------------------------------------------------------------------------

def test_two_partners_sharing_a_surname_are_not_merged(clean_conn):
    """The lift-out case. Same surname, same firm, same window, two people."""
    conn = clean_conn
    firm = make_firm(conn)
    origin = make_firm(conn)
    a = person(conn, "Sarah Chen", "chen")
    b = person(conn, "Michael Chen", "chen")
    reported(conn, person_id=a, to_firm_id=firm, from_firm_id=origin,
             announced=date(2026, 3, 1), source_id=make_source(conn, slug="outlet-a"))
    reported(conn, person_id=b, to_firm_id=firm, from_firm_id=origin,
             announced=date(2026, 3, 2), source_id=make_source(conn, slug="outlet-b"))
    conn.commit()

    stats = stage.run(conn, apply=True)
    assert stats["merged"] == 0
    assert conn.execute(
        "SELECT count(*) AS n FROM canonical_moves").fetchone()["n"] == 2


def test_moves_outside_the_window_are_never_compared(clean_conn):
    conn = clean_conn
    firm = make_firm(conn)
    p = person(conn, "Sarah Chen", "chen")
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 1, 1),
             source_id=make_source(conn, slug="outlet-a"))
    reported(conn, person_id=p, to_firm_id=firm, announced=date(2026, 9, 1),
             source_id=make_source(conn, slug="outlet-b"))
    conn.commit()

    stats = stage.run(conn, apply=True)
    assert stats["pairs_compared"] == 0
    assert stats["merged"] == 0


def test_an_ambiguous_pair_goes_to_the_queue_with_its_counterpart(clean_conn):
    """A bare surname in one report. Both rows stay; a human decides."""
    conn = clean_conn
    firm = make_firm(conn)
    named = person(conn, "Sarah Chen", "chen")
    bare = person(conn, "Chen", "chen")
    a = reported(conn, person_id=named, to_firm_id=firm, announced=date(2026, 3, 1),
                 source_id=make_source(conn, slug="outlet-a"))
    b = reported(conn, person_id=bare, to_firm_id=firm, announced=date(2026, 3, 2),
                 source_id=make_source(conn, slug="outlet-b"))
    conn.commit()

    stats = stage.run(conn, apply=True)
    assert stats["merged"] == 0
    assert stats["queued"] == 1

    item = conn.execute(
        "SELECT move_id, related_move_id, reason, detail FROM review_queue"
    ).fetchone()
    assert item["reason"] == "dedupe_ambiguous"
    assert {item["move_id"], item["related_move_id"]} == {a, b}
    assert item["detail"]["given"]["label"]
    assert conn.execute(
        "SELECT count(*) AS n FROM canonical_moves").fetchone()["n"] == 2


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------

def test_running_twice_changes_nothing_the_second_time(two_reports):
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    after_first = conn.execute("SELECT count(*) AS n FROM moves").fetchone()["n"]

    stats = stage.run(conn, apply=True)
    assert stats["merged"] == 0
    assert conn.execute("SELECT count(*) AS n FROM moves").fetchone()["n"] == after_first


def test_an_ambiguous_pair_does_not_stack_queue_items(clean_conn):
    conn = clean_conn
    firm = make_firm(conn)
    named = person(conn, "Sarah Chen", "chen")
    bare = person(conn, "Chen", "chen")
    reported(conn, person_id=named, to_firm_id=firm, announced=date(2026, 3, 1),
             source_id=make_source(conn, slug="outlet-a"))
    reported(conn, person_id=bare, to_firm_id=firm, announced=date(2026, 3, 2),
             source_id=make_source(conn, slug="outlet-b"))
    conn.commit()

    stage.run(conn, apply=True)
    stage.run(conn, apply=True)
    assert conn.execute(
        "SELECT count(*) AS n FROM review_queue WHERE resolved_at IS NULL"
    ).fetchone()["n"] == 1


# --------------------------------------------------------------------------
# Team moves
# --------------------------------------------------------------------------

def test_two_partners_moving_between_one_firm_pair_are_a_team_move(clean_conn):
    conn = clean_conn
    to_firm, from_firm = make_firm(conn, "Destination"), make_firm(conn, "Origin")
    for i, name in enumerate([("Sarah Chen", "chen"), ("Michael Lim", "lim")]):
        reported(conn, person_id=person(conn, *name), to_firm_id=to_firm,
                 from_firm_id=from_firm, announced=date(2026, 3, 1 + i),
                 source_id=make_source(conn, slug=f"s{i}"))
    conn.commit()

    stats = stage.run(conn, apply=True)
    assert stats["teams"] == 1

    team = conn.execute("SELECT * FROM team_moves").fetchone()
    assert team["partner_count"] == 2, "maintained by the trigger, not by us"
    assert team["from_firm_id"] == from_firm
    assert team["to_firm_id"] == to_firm


def test_one_person_reported_twice_is_not_a_team_move(clean_conn):
    """This is why team detection runs after merging and counts people."""
    conn = clean_conn
    to_firm, from_firm = make_firm(conn), make_firm(conn)
    p = person(conn, "Sarah Chen", "chen")
    reported(conn, person_id=p, to_firm_id=to_firm, from_firm_id=from_firm,
             announced=date(2026, 3, 1), source_id=make_source(conn, slug="outlet-a"))
    reported(conn, person_id=p, to_firm_id=to_firm, from_firm_id=from_firm,
             announced=date(2026, 3, 2), source_id=make_source(conn, slug="outlet-b"))
    conn.commit()

    stats = stage.run(conn, apply=True)
    assert stats["merged"] == 1
    assert stats["teams"] == 0


def test_partners_joining_the_same_firm_from_different_firms_are_not_a_team(clean_conn):
    conn = clean_conn
    to_firm = make_firm(conn)
    for i, name in enumerate([("Sarah Chen", "chen"), ("Michael Lim", "lim")]):
        reported(conn, person_id=person(conn, *name), to_firm_id=to_firm,
                 from_firm_id=make_firm(conn), announced=date(2026, 3, 1 + i),
                 source_id=make_source(conn, slug=f"s{i}"))
    conn.commit()
    assert stage.run(conn, apply=True)["teams"] == 0


def test_a_team_move_window_never_exceeds_sixty_days(clean_conn):
    """The schema rejects a wider one; the detector must not build one."""
    conn = clean_conn
    to_firm, from_firm = make_firm(conn), make_firm(conn)
    for i, (name, surname, day) in enumerate(
        [("Sarah Chen", "chen", date(2026, 3, 1)),
         ("Michael Lim", "lim", date(2026, 3, 20)),
         ("Anna Wong", "wong", date(2026, 7, 1))]
    ):
        reported(conn, person_id=person(conn, name, surname), to_firm_id=to_firm,
                 from_firm_id=from_firm, announced=day,
                 source_id=make_source(conn, slug=f"s{i}"))
    conn.commit()

    stage.run(conn, apply=True)
    teams = conn.execute(
        "SELECT window_start, window_end, partner_count FROM team_moves").fetchall()
    assert len(teams) == 1, "the July move is its own cluster and is not a team"
    assert (teams[0]["window_end"] - teams[0]["window_start"]).days <= 60
    assert teams[0]["partner_count"] == 2


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

def test_the_dataset_has_no_invariant_violations_after_a_merge(two_reports):
    conn, _, _, _, _ = two_reports
    stage.run(conn, apply=True)
    violations = conn.execute("SELECT * FROM invariant_violations").fetchall()
    assert violations == []
