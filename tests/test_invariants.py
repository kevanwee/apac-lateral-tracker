"""Every schema invariant, asserted by trying to violate it.

A rule that is only documented is not a rule. Each test here fails if the
database would accept data the data model says is impossible.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from tests.conftest import (
    TAXONOMY_VERSION,
    check_deferred,
    classify,
    make_firm,
    make_move,
    make_person,
    make_raw_item,
    make_source,
    mark_classified,
    seed_taxonomy,
)

# ---------------------------------------------------------------------------
# Phase 1 stated invariants
# ---------------------------------------------------------------------------


def test_move_cannot_leave_and_join_the_same_firm(conn):
    firm = make_firm(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        make_move(conn, from_firm_id=firm, to_firm_id=firm, move_type="lateral")


def test_promotion_may_leave_and_join_the_same_firm(conn):
    firm = make_firm(conn)
    move_id = make_move(conn, from_firm_id=firm, to_firm_id=firm, move_type="promotion")
    assert move_id is not None


def test_promotion_must_be_within_one_firm(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        make_move(
            conn,
            from_firm_id=make_firm(conn),
            to_firm_id=make_firm(conn),
            move_type="promotion",
        )


def test_announced_date_cannot_be_null(conn):
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO moves (person_id, to_firm_id, move_type, announced_date, confidence) "
            "VALUES (%s, %s, 'lateral', NULL, 0.9)",
            (make_person(conn), make_firm(conn)),
        )


def test_effective_date_may_be_null_and_may_precede_announcement(conn):
    """Moves are often reported after the fact; only the absurd is rejected."""
    move_id = make_move(conn, announced_date=date(2025, 3, 1))
    conn.execute(
        "UPDATE moves SET effective_date = DATE '2025-01-15' WHERE id = %s", (move_id,)
    )
    row = conn.execute(
        "SELECT effective_date FROM moves WHERE id = %s", (move_id,)
    ).fetchone()
    assert row["effective_date"] == date(2025, 1, 15)


def test_destination_required_unless_retirement(conn):
    person = make_person(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO moves (person_id, to_firm_id, move_type, announced_date, confidence) "
            "VALUES (%s, NULL, 'lateral', DATE '2025-03-01', 0.9)",
            (person,),
        )


def test_retirement_needs_no_destination(conn):
    person = make_person(conn)
    row = conn.execute(
        "INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type, "
        "announced_date, confidence) "
        "VALUES (%s, %s, NULL, 'retirement', DATE '2025-03-01', 0.9) RETURNING id",
        (person, make_firm(conn)),
    ).fetchone()
    assert row["id"] is not None


# ---------------------------------------------------------------------------
# Exactly one primary practice group
# ---------------------------------------------------------------------------


def test_at_most_one_primary_practice_group(conn):
    nodes = seed_taxonomy(conn)
    move_id = make_move(conn)
    classify(conn, move_id, nodes["corporate.m_and_a"], primary=True)
    with pytest.raises(psycopg.errors.UniqueViolation):
        classify(conn, move_id, nodes["finance.banking"], primary=True)


def test_classified_move_must_have_a_primary_practice_group(conn):
    seed_taxonomy(conn)
    move_id = make_move(conn)
    mark_classified(conn, move_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        check_deferred(conn)


def test_classified_move_with_exactly_one_primary_is_accepted(conn):
    nodes = seed_taxonomy(conn)
    move_id = make_move(conn)
    classify(conn, move_id, nodes["corporate.m_and_a"], primary=True)
    classify(conn, move_id, nodes["finance.banking"], primary=False)
    mark_classified(conn, move_id)
    check_deferred(conn)  # must not raise


def test_unclassified_move_still_carries_the_sentinel_node(conn):
    """A move the classifier cannot place stays in the denominator."""
    nodes = seed_taxonomy(conn)
    move_id = make_move(conn)
    classify(conn, move_id, nodes["unclassified"], primary=True)
    mark_classified(conn, move_id)
    check_deferred(conn)


def test_at_most_two_secondary_practice_groups(conn):
    nodes = seed_taxonomy(conn)
    move_id = make_move(conn)
    classify(conn, move_id, nodes["corporate"], primary=True)
    classify(conn, move_id, nodes["corporate.m_and_a"], primary=False)
    classify(conn, move_id, nodes["finance.banking"], primary=False)
    classify(conn, move_id, nodes["disputes"], primary=False)
    mark_classified(conn, move_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        check_deferred(conn)


def test_assignments_must_come_from_the_recorded_taxonomy_version(conn):
    """Historical assignments keep their version; a move cannot mix versions."""
    nodes = seed_taxonomy(conn)
    conn.execute(
        "INSERT INTO taxonomy_versions (version, checksum) VALUES ('0.0.2', %s)",
        (b"\x01" * 32,),
    )
    move_id = make_move(conn)
    classify(conn, move_id, nodes["corporate"], primary=True, version=TAXONOMY_VERSION)
    mark_classified(conn, move_id, version="0.0.2")
    with pytest.raises(psycopg.errors.CheckViolation):
        check_deferred(conn)


def test_practice_group_child_cannot_parent_across_versions(conn):
    nodes = seed_taxonomy(conn)
    conn.execute(
        "INSERT INTO taxonomy_versions (version, checksum) VALUES ('0.0.2', %s)",
        (b"\x01" * 32,),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO practice_groups (taxonomy_version, code, name, level, parent_id) "
            "VALUES ('0.0.2', 'corporate.m_and_a', 'M&A', 2, %s)",
            (nodes["corporate"],),
        )


# ---------------------------------------------------------------------------
# Phase 0 constraints expressed in the schema
# ---------------------------------------------------------------------------


def test_provenance_excerpt_is_capped_at_25_words(conn):
    source = make_source(conn)
    item = make_raw_item(conn, source)
    move_id = make_move(conn)
    long_excerpt = " ".join(f"word{i}" for i in range(26))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO move_field_evidence (move_id, raw_item_id, field_name, "
            "span_start, span_end, extracted_value, excerpt) "
            "VALUES (%s, %s, 'to_firm', 0, 10, 'Firm X', %s)",
            (move_id, item, long_excerpt),
        )


def test_provenance_excerpt_of_25_words_is_accepted(conn):
    source = make_source(conn)
    item = make_raw_item(conn, source)
    move_id = make_move(conn)
    excerpt = " ".join(f"word{i}" for i in range(25))
    conn.execute(
        "INSERT INTO move_field_evidence (move_id, raw_item_id, field_name, "
        "span_start, span_end, extracted_value, excerpt) "
        "VALUES (%s, %s, 'to_firm', 0, 10, 'Firm X', %s)",
        (move_id, item, excerpt),
    )


def test_raw_items_have_no_column_for_article_text(conn):
    """Phase 0 section 2 is enforced by absence, so assert the absence."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'raw_items'"
    ).fetchall()
    columns = {r["column_name"] for r in rows}
    assert not columns & {"body", "content", "summary", "full_text", "article_text"}


def test_poll_interval_cannot_go_below_the_ten_second_floor(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO sources (slug, name, base_url, feed_url, access_type, "
            "reliability_tier, poll_interval) "
            "VALUES ('fast', 'Fast', 'https://e.test', 'https://e.test/f', 'feed', 2, "
            "interval '5 seconds')"
        )


def test_active_html_source_requires_a_dated_terms_review(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO sources (slug, name, base_url, access_type, adapter, "
            "reliability_tier, active) "
            "VALUES ('scrapey', 'Scrapey', 'https://e.test', 'html', 'html', 2, true)"
        )


def test_html_source_is_allowed_once_terms_are_reviewed(conn):
    conn.execute(
        "INSERT INTO sources (slug, name, base_url, access_type, adapter, "
        "reliability_tier, active, html_access_reviewed_at, html_access_reviewed_by) "
        "VALUES ('okhtml', 'Ok', 'https://e.test', 'html', 'html', 2, true, "
        "now(), 'kevan')"
    )


def test_only_feed_adapters_are_required_to_have_a_feed_url(conn):
    """A sitemap or mailbox source legitimately has no feed of its own."""
    conn.execute(
        "INSERT INTO sources (slug, name, base_url, access_type, adapter, "
        "reliability_tier) "
        "VALUES ('arch', 'Archive', 'https://e.test', 'feed', 'sitemap', 2)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO sources (slug, name, base_url, access_type, adapter, "
            "reliability_tier) "
            "VALUES ('nofeed', 'No feed', 'https://e.test', 'feed', 'feed', 2)"
        )


def test_tier_one_is_reserved_for_firm_newsrooms(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO sources (slug, name, base_url, feed_url, access_type, "
            "reliability_tier) "
            "VALUES ('pretend', 'Pretend', 'https://e.test', 'https://e.test/f', 'feed', 1)"
        )


def test_source_jurisdiction_focus_must_be_known(conn):
    with pytest.raises(psycopg.errors.RaiseException):
        make_source(conn, jurisdiction_focus=["SG", "ZZ"])


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_reingesting_the_same_url_is_rejected(conn):
    source = make_source(conn)
    make_raw_item(conn, source, url="https://example.test/a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        make_raw_item(conn, source, url="https://example.test/a")


def test_reextracting_the_same_item_cannot_create_a_second_move(conn):
    person = make_person(conn)
    firm = make_firm(conn)
    conn.execute(
        "INSERT INTO moves (person_id, to_firm_id, move_type, announced_date, "
        "confidence, extraction_fingerprint) "
        "VALUES (%s, %s, 'lateral', DATE '2025-03-01', 0.9, 'fp-1')",
        (person, firm),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO moves (person_id, to_firm_id, move_type, announced_date, "
            "confidence, extraction_fingerprint) "
            "VALUES (%s, %s, 'lateral', DATE '2025-03-01', 0.9, 'fp-1')",
            (person, firm),
        )


def test_review_queue_does_not_stack_duplicate_open_items(conn):
    move_id = make_move(conn)
    conn.execute(
        "INSERT INTO review_queue (move_id, reason) VALUES (%s, 'low_confidence')",
        (move_id,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO review_queue (move_id, reason) VALUES (%s, 'low_confidence')",
            (move_id,),
        )


def test_resolved_review_items_do_not_block_a_new_one(conn):
    move_id = make_move(conn)
    conn.execute(
        "INSERT INTO review_queue (move_id, reason, resolved_at, resolution, resolver) "
        "VALUES (%s, 'low_confidence', now(), 'accepted', 'kevan')",
        (move_id,),
    )
    conn.execute(
        "INSERT INTO review_queue (move_id, reason) VALUES (%s, 'low_confidence')",
        (move_id,),
    )


# ---------------------------------------------------------------------------
# Precision guards
# ---------------------------------------------------------------------------


def test_in_house_exit_must_point_at_an_in_house_destination(conn):
    make_move(conn, move_type="lateral")  # baseline sanity
    person = make_person(conn)
    conn.execute(
        "INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type, "
        "announced_date, confidence) "
        "VALUES (%s, %s, %s, 'in_house_exit', DATE '2025-03-01', 0.9)",
        (person, make_firm(conn), make_firm(conn, firm_type="global")),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        check_deferred(conn)


def test_in_house_exit_to_an_in_house_destination_is_accepted(conn):
    person = make_person(conn)
    conn.execute(
        "INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type, "
        "announced_date, confidence) "
        "VALUES (%s, %s, %s, 'in_house_exit', DATE '2025-03-01', 0.9)",
        (person, make_firm(conn), make_firm(conn, firm_type="in_house")),
    )
    check_deferred(conn)


def test_move_cannot_join_a_team_move_with_a_different_firm_pair(conn):
    from_firm, to_firm, other = make_firm(conn), make_firm(conn), make_firm(conn)
    team = conn.execute(
        "INSERT INTO team_moves (from_firm_id, to_firm_id, window_start, window_end) "
        "VALUES (%s, %s, DATE '2025-03-01', DATE '2025-04-01') RETURNING id",
        (from_firm, to_firm),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type, "
        "announced_date, confidence, team_move_id) "
        "VALUES (%s, %s, %s, 'lateral', DATE '2025-03-10', 0.9, %s)",
        (make_person(conn), from_firm, other, team),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        check_deferred(conn)


def test_team_move_partner_count_is_maintained_by_the_database(conn):
    from_firm, to_firm = make_firm(conn), make_firm(conn)
    team = conn.execute(
        "INSERT INTO team_moves (from_firm_id, to_firm_id, window_start, window_end) "
        "VALUES (%s, %s, DATE '2025-03-01', DATE '2025-04-01') RETURNING id",
        (from_firm, to_firm),
    ).fetchone()["id"]
    for surname in ("tan", "lim", "wong"):
        conn.execute(
            "INSERT INTO moves (person_id, from_firm_id, to_firm_id, move_type, "
            "announced_date, confidence, team_move_id) "
            "VALUES (%s, %s, %s, 'lateral', DATE '2025-03-10', 0.9, %s)",
            (make_person(conn, f"A {surname}", surname), from_firm, to_firm, team),
        )
    count = conn.execute(
        "SELECT partner_count FROM team_moves WHERE id = %s", (team,)
    ).fetchone()["partner_count"]
    assert count == 3


def test_a_superseded_move_is_kept_not_overwritten(conn):
    """Merges create a canonical row; the inputs stay readable."""
    firm = make_firm(conn)
    person = make_person(conn)
    reported = make_move(conn, person_id=person, to_firm_id=firm)
    canonical = make_move(conn, person_id=person, to_firm_id=firm)
    conn.execute(
        "UPDATE moves SET superseded_by_move_id = %s, merged_at = now(), "
        "review_state = 'superseded' WHERE id = %s",
        (canonical, reported),
    )
    still_there = conn.execute(
        "SELECT id FROM moves WHERE id = %s", (reported,)
    ).fetchone()
    assert still_there is not None

    visible = conn.execute(
        "SELECT id FROM canonical_moves WHERE id = ANY(%s)",
        ([reported, canonical],),
    ).fetchall()
    assert [r["id"] for r in visible] == [canonical]


def test_supersession_and_review_state_cannot_disagree(conn):
    firm = make_firm(conn)
    person = make_person(conn)
    a = make_move(conn, person_id=person, to_firm_id=firm)
    b = make_move(conn, person_id=person, to_firm_id=firm)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE moves SET superseded_by_move_id = %s, merged_at = now() WHERE id = %s",
            (b, a),
        )


# ---------------------------------------------------------------------------
# Erasure (Phase 0, section 4)
# ---------------------------------------------------------------------------


def test_erase_person_removes_the_person_and_every_derived_record(conn):
    nodes = seed_taxonomy(conn)
    source = make_source(conn)
    item = make_raw_item(conn, source)
    person = make_person(conn, "Wei Ming Tan", "tan")
    move_id = make_move(conn, person_id=person)
    conn.execute(
        "INSERT INTO move_sources (move_id, raw_item_id, is_primary) VALUES (%s, %s, true)",
        (move_id, item),
    )
    conn.execute(
        "INSERT INTO move_field_evidence (move_id, raw_item_id, field_name, "
        "span_start, span_end, extracted_value, excerpt) "
        "VALUES (%s, %s, 'person_name', 0, 12, 'Wei Ming Tan', 'Wei Ming Tan joins')",
        (move_id, item),
    )
    classify(conn, move_id, nodes["corporate"], primary=True)

    result = conn.execute(
        "SELECT * FROM erase_person(%s, 'subject request', 'kevan')", (person,)
    ).fetchone()
    assert result["moves_deleted"] == 1
    assert result["evidence_deleted"] == 1
    assert result["sources_unlinked"] == 1

    assert conn.execute("SELECT 1 FROM people WHERE id = %s", (person,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM moves WHERE id = %s", (move_id,)).fetchone() is None
    assert (
        conn.execute(
            "SELECT 1 FROM move_field_evidence WHERE move_id = %s", (move_id,)
        ).fetchone()
        is None
    )
    # The raw item survives: it is source metadata, not personal data.
    assert conn.execute("SELECT 1 FROM raw_items WHERE id = %s", (item,)).fetchone()


def test_erasure_survives_the_next_ingest_run(conn):
    person = make_person(conn, "Wei Ming Tan", "tan")
    conn.execute("SELECT * FROM erase_person(%s, 'subject request', 'kevan')", (person,))
    with pytest.raises(psycopg.errors.RestrictViolation):
        make_person(conn, "Wei Ming Tan", "tan")


def test_erasure_matches_across_honorifics_and_post_nominals(conn):
    person = make_person(conn, "Wei Ming Tan", "tan")
    conn.execute("SELECT * FROM erase_person(%s, 'subject request', 'kevan')", (person,))
    with pytest.raises(psycopg.errors.RestrictViolation):
        make_person(conn, "Mr Wei Ming Tan SC", "tan")


def test_suppression_list_holds_no_names(conn):
    person = make_person(conn, "Wei Ming Tan", "tan")
    conn.execute("SELECT * FROM erase_person(%s, 'subject request', 'kevan')", (person,))
    rows = conn.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'suppressed_people'").fetchall()
    columns = {r["column_name"] for r in rows}
    assert columns == {"name_hash", "reason", "suppressed_at", "actor"}
    stored = conn.execute("SELECT name_hash FROM suppressed_people").fetchall()
    assert all(b"Tan" not in bytes(r["name_hash"]) for r in stored)


def test_erasure_log_records_counts_without_personal_data(conn):
    person = make_person(conn, "Wei Ming Tan", "tan")
    make_move(conn, person_id=person)
    conn.execute("SELECT * FROM erase_person(%s, 'subject request', 'kevan')", (person,))
    row = conn.execute("SELECT * FROM erasure_log").fetchone()
    assert row["moves_deleted"] == 1
    assert row["actor"] == "kevan"
    assert "person_id" not in row and "name" not in row


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


def test_invariant_violations_view_is_empty_on_a_healthy_dataset(conn):
    nodes = seed_taxonomy(conn)
    source = make_source(conn)
    item = make_raw_item(conn, source)
    move_id = make_move(conn)
    conn.execute(
        "INSERT INTO move_sources (move_id, raw_item_id, is_primary) VALUES (%s, %s, true)",
        (move_id, item),
    )
    classify(conn, move_id, nodes["corporate.m_and_a"], primary=True)
    mark_classified(conn, move_id)
    check_deferred(conn)
    assert conn.execute("SELECT * FROM invariant_violations").fetchall() == []


def test_ambiguous_alias_view_surfaces_aliases_resolving_to_two_firms(conn):
    a, b = make_firm(conn, "Alpha LLP"), make_firm(conn, "Alpha Partners")
    for firm in (a, b):
        conn.execute(
            "INSERT INTO firm_aliases (firm_id, alias, alias_type) "
            "VALUES (%s, 'Alpha', 'abbreviation')",
            (firm,),
        )
    rows = conn.execute("SELECT * FROM ambiguous_firm_aliases").fetchall()
    assert len(rows) == 1
    assert rows[0]["firm_count"] == 2
