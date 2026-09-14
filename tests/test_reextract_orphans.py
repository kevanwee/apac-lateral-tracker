"""What `reextract --apply` has to take with it.

Discarding moves leaves rows that only existed to point at them. The people
case was found and fixed earlier; the team-move case was found when the
invariant view went from 0 violations to 2 after a re-extraction that followed
a dedupe pass.

A team move whose members have been discarded is a lift-out with nobody in it:
the trigger recomputes partner_count to zero on the delete but cannot remove
the row, so it survives and trips `team_move_with_fewer_than_two_partners`.
"""

from __future__ import annotations

from datetime import date

from tests.conftest import make_firm, make_move, make_person


def _empty_team(conn, to_firm, from_firm):
    return conn.execute(
        """
        INSERT INTO team_moves (from_firm_id, to_firm_id, window_start, window_end)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (from_firm, to_firm, date(2026, 3, 1), date(2026, 3, 20)),
    ).fetchone()["id"]


def _sweep(conn) -> int:
    """The statement `reextract --apply` runs after discarding moves."""
    return conn.execute(
        """
        DELETE FROM team_moves tm
        WHERE NOT EXISTS (
            SELECT 1 FROM moves m
            WHERE m.team_move_id = tm.id AND m.superseded_by_move_id IS NULL
        )
        """
    ).rowcount


def test_a_team_move_left_with_no_members_is_removed(conn):
    to_firm, from_firm = make_firm(conn), make_firm(conn)
    team = _empty_team(conn, to_firm, from_firm)

    assert conn.execute(
        "SELECT partner_count FROM team_moves WHERE id = %s", (team,)
    ).fetchone()["partner_count"] == 0

    assert _sweep(conn) == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM team_moves WHERE id = %s", (team,)
    ).fetchone()["n"] == 0


def test_a_team_move_that_still_has_members_is_kept(conn):
    to_firm, from_firm = make_firm(conn), make_firm(conn)
    team = _empty_team(conn, to_firm, from_firm)
    for name, surname in [("Sarah Chen", "chen"), ("Michael Lim", "lim")]:
        move = make_move(
            conn, person_id=make_person(conn, name, surname),
            to_firm_id=to_firm, from_firm_id=from_firm,
            announced_date=date(2026, 3, 2),
        )
        conn.execute("UPDATE moves SET team_move_id = %s WHERE id = %s", (team, move))

    assert conn.execute(
        "SELECT partner_count FROM team_moves WHERE id = %s", (team,)
    ).fetchone()["partner_count"] == 2
    assert _sweep(conn) == 0


def test_the_sweep_leaves_no_invariant_violation_behind(conn):
    to_firm, from_firm = make_firm(conn), make_firm(conn)
    _empty_team(conn, to_firm, from_firm)
    assert conn.execute(
        "SELECT count(*) AS n FROM invariant_violations"
    ).fetchone()["n"] == 1

    _sweep(conn)
    assert conn.execute(
        "SELECT count(*) AS n FROM invariant_violations"
    ).fetchone()["n"] == 0
