"""Phase 4 — turn reported moves into events.

Until this stage runs, one hire covered by three outlets is three rows, and
methodology section 1 forbids any cross-source trend on that basis. What this
stage does is narrow: it finds rows that are the same event, replaces them with
one canonical row, and keeps everything the losing rows said.

## A merge never overwrites

The merged row is a **new** row. The inputs are not deleted and not edited into
each other; they are marked `superseded` and keep pointing at the canonical row
that replaced them. Two consequences that the schema relies on:

- Provenance survives. Every original row still carries its own extraction
  fingerprint, its own source and its own field evidence, so a merge can be
  read backwards and `erase_person` can unwind one.
- Disagreement survives. A value that lost a field-level contest is written to
  `field_conflicts` rather than dropped. Two outlets disagreeing about which
  firm someone left is a fact about the reporting, and the dashboard is
  entitled to see it.

## Why components rather than pairs

A block can contain three reports of one move. Merging pairwise would chain
supersessions — B superseded by AB, then AB superseded by ABC — and leave the
intermediate row as a canonical-looking artefact of the order rows happened to
come back in. So merge decisions are unioned into connected components and each
component collapses once, in one transaction.

Ambiguous pairs are not components. They stay as they are and go to the review
queue, which is what `review_queue.related_move_id` exists for.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import timedelta

from tracker import dedupe as scoring
from tracker.extract import confidence

log = logging.getLogger(__name__)

# Fields whose value is contested when two rows disagree. Destination firm and
# person are not here: blocking already required them to agree.
CONTESTED_FIELDS = (
    "from_firm_id",
    "title_from",
    "title_to",
    "partner_tier",
    "office_jurisdiction",
    "effective_date",
    "move_type",
)

# A merged row keeps the strongest reporting it was built from. Tier 1 is a
# firm's own newsroom; ties break on the record's own confidence.
def _authority(row: dict) -> tuple:
    return (int(row["reliability_tier"] or 9), -float(row["confidence"] or 0))


_CANDIDATE_SQL = """
SELECT m.id, m.person_id, m.from_firm_id, m.to_firm_id,
       m.title_from, m.title_to, m.partner_tier, m.office_jurisdiction,
       m.announced_date, m.effective_date, m.move_type, m.confidence,
       m.confidence_components, m.review_state, m.classification_state,
       m.classified_taxonomy_version, m.extractor_version, m.team_move_id,
       p.canonical_name AS person_name, p.name_variants, p.surname_normalised,
       s.id AS source_id, s.reliability_tier
FROM canonical_moves m
JOIN people p ON p.id = m.person_id
JOIN move_sources ms ON ms.move_id = m.id AND ms.is_primary
JOIN raw_items ri ON ri.id = ms.raw_item_id
JOIN sources s ON s.id = ri.source_id
WHERE m.to_firm_id IS NOT NULL
ORDER BY p.surname_normalised, m.to_firm_id, m.announced_date
"""


def _blocks(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Group by the blocking key. Singletons are dropped — nothing to compare."""
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["surname_normalised"], row["to_firm_id"])].append(row)
    return {key: group for key, group in grouped.items() if len(group) > 1}


def _pairs(block: list[dict]):
    """Every pair in a block whose announcements fall inside the window."""
    window = timedelta(days=scoring.WINDOW_DAYS)
    for i, a in enumerate(block):
        for b in block[i + 1:]:
            if abs(a["announced_date"] - b["announced_date"]) <= window:
                yield a, b


class _Union:
    """Union-find over move ids, so a block merges once rather than pairwise."""

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def components(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return {root: members for root, members in out.items() if len(members) > 1}


def _is_stated(name: str, value) -> bool:
    """Whether a row actually said something about this field.

    `partner_tier` is NOT NULL with a default of 'undisclosed', so a row that
    never mentioned seniority still carries a value. Counting that as a stated
    value would record a conflict every time one outlet gave the tier and the
    other did not, and would put an otherwise clean merge in front of a human.
    """
    if value is None:
        return False
    return not (name == "partner_tier" and value == "undisclosed")


def _resolve_fields(members: list[dict]) -> tuple[dict, dict]:
    """(winning values, field_conflicts) across a component.

    The most authoritative row that states a field wins it. Every other stated
    value that differs is kept, with the source that said it, so the merge can
    be argued with later.
    """
    ordered = sorted(members, key=_authority)
    winners: dict = {}
    conflicts: dict = {}

    for name in CONTESTED_FIELDS:
        stated = [row for row in ordered if _is_stated(name, row.get(name))]
        if not stated:
            winners[name] = None
            continue
        winner = stated[0]
        winners[name] = winner[name]
        losing = [
            {
                "value": str(row[name]),
                "move_id": str(row["id"]),
                "source_id": str(row["source_id"]),
                "tier": int(row["reliability_tier"] or 9),
            }
            for row in stated[1:]
            if row[name] != winner[name]
        ]
        if losing:
            conflicts[name] = losing

    return winners, conflicts


def _merge_component(conn, members: list[dict]) -> tuple[str, bool]:
    """Collapse one component into a new canonical row.

    Returns (new move id, whether it needs a human).
    """
    ordered = sorted(members, key=_authority)
    winner = ordered[0]
    winners, conflicts = _resolve_fields(members)

    # The earliest report is the announcement; later ones are coverage of it.
    announced = min(row["announced_date"] for row in members)
    sources = {row["source_id"] for row in members}

    stored_components = winner["confidence_components"] or {}
    if isinstance(stored_components, str):
        stored_components = json.loads(stored_components)
    scored = confidence.recompute_with_corroboration(stored_components, len(sources))

    # A field two outlets actively disagree about is not something to settle
    # silently, even when the pair itself scored high enough to merge.
    needs_human = bool(conflicts) or confidence.needs_review(scored)
    review_state = "pending_review" if needs_human else "auto_accepted"

    # Guard the schema's own invariants rather than letting one odd component
    # abort the whole stage.
    move_type = winners["move_type"] or winner["move_type"]
    from_firm_id = winners["from_firm_id"]
    if move_type == "promotion":
        from_firm_id = winner["to_firm_id"]
    elif from_firm_id is not None and from_firm_id == winner["to_firm_id"]:
        from_firm_id = None

    created = conn.execute(
        """
        INSERT INTO moves
            (person_id, from_firm_id, to_firm_id, title_from, title_to,
             partner_tier, office_jurisdiction, announced_date, effective_date,
             move_type, confidence, confidence_components, review_state,
             classification_state, classified_taxonomy_version, field_conflicts,
             extractor_version, extraction_fingerprint)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        RETURNING id
        """,
        (
            winner["person_id"], from_firm_id, winner["to_firm_id"],
            winners["title_from"], winners["title_to"],
            winners["partner_tier"] or "undisclosed", winners["office_jurisdiction"],
            announced, winners["effective_date"], move_type,
            scored.total, json.dumps(scored.as_dict()), review_state,
            winner["classification_state"], winner["classified_taxonomy_version"],
            json.dumps(conflicts), winner["extractor_version"],
        ),
    ).fetchone()
    new_id = created["id"]

    member_ids = [row["id"] for row in members]

    # Every source of every input is a source of the merged row. One primary,
    # taken from the most authoritative input.
    conn.execute(
        """
        INSERT INTO move_sources (move_id, raw_item_id, is_primary)
        SELECT %s, ms.raw_item_id, (ms.move_id = %s AND ms.is_primary)
        FROM move_sources ms
        WHERE ms.move_id = ANY(%s)
        ON CONFLICT (move_id, raw_item_id) DO NOTHING
        """,
        (new_id, winner["id"], member_ids),
    )
    # Provenance spans follow, so the merged row can still point at the text.
    conn.execute(
        """
        INSERT INTO move_field_evidence
            (move_id, raw_item_id, field_name, span_start, span_end,
             extracted_value, excerpt)
        SELECT %s, e.raw_item_id, e.field_name, e.span_start, e.span_end,
               e.extracted_value, e.excerpt
        FROM move_field_evidence e
        WHERE e.move_id = ANY(%s)
        ON CONFLICT (move_id, raw_item_id, field_name) DO NOTHING
        """,
        (new_id, member_ids),
    )
    # The winner's classification carries over with its taxonomy version.
    conn.execute(
        """
        INSERT INTO move_practice_groups
            (move_id, practice_group_id, taxonomy_version, is_primary,
             confidence, assigned_by, rule_key)
        SELECT %s, g.practice_group_id, g.taxonomy_version, g.is_primary,
               g.confidence, g.assigned_by, g.rule_key
        FROM move_practice_groups g
        WHERE g.move_id = %s
        ON CONFLICT (move_id, practice_group_id) DO NOTHING
        """,
        (new_id, winner["id"]),
    )

    # A second spelling of the name is now known to be the same person.
    spellings = sorted({
        variant
        for row in members
        for variant in (list(row["name_variants"] or []) + [row["person_name"]])
        if variant
    })
    conn.execute(
        """
        UPDATE people SET name_variants = coalesce((
            SELECT array_agg(DISTINCT v) FROM unnest(name_variants || %s::text[]) AS v
            WHERE v IS NOT NULL AND btrim(v) <> ''
        ), '{}') WHERE id = %s
        """,
        (spellings, winner["person_id"]),
    )

    conn.execute(
        """
        UPDATE moves
           SET superseded_by_move_id = %s, merged_at = now(),
               review_state = 'superseded'
         WHERE id = ANY(%s)
        """,
        (new_id, member_ids),
    )
    return new_id, needs_human


def _queue(conn, move_id: str, reason: str, detail: dict,
           related_move_id: str | None = None) -> bool:
    """Open a review item, unless the same open one is already there."""
    row = conn.execute(
        """
        INSERT INTO review_queue (move_id, related_move_id, reason, detail)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (move_id, reason, coalesce(related_move_id, move_id))
            WHERE resolved_at IS NULL
        DO NOTHING
        RETURNING id
        """,
        (move_id, related_move_id, reason, json.dumps(detail)),
    ).fetchone()
    return row is not None


def _detect_team_moves(conn) -> int:
    """A lift-out is one market signal, not five unrelated laterals.

    Two or more canonical moves sharing a firm pair inside 60 days. Runs after
    merging so that three reports of one hire cannot look like a team of three.
    """
    rows = conn.execute(
        """
        SELECT m.id, m.person_id, m.from_firm_id, m.to_firm_id,
               m.office_jurisdiction, m.announced_date
        FROM canonical_moves m
        WHERE m.from_firm_id IS NOT NULL AND m.to_firm_id IS NOT NULL
          AND m.from_firm_id <> m.to_firm_id AND m.team_move_id IS NULL
        ORDER BY m.to_firm_id, m.from_firm_id, m.announced_date
        """
    ).fetchall()

    by_pair: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        by_pair[(row["to_firm_id"], row["from_firm_id"])].append(row)

    created = 0
    for (to_firm, from_firm), group in by_pair.items():
        cluster: list[dict] = []
        for row in group + [None]:
            if (
                row is not None
                and cluster
                and (row["announced_date"] - cluster[0]["announced_date"]).days
                <= scoring.WINDOW_DAYS
            ):
                cluster.append(row)
                continue
            # Distinct people, so one person reported twice is not a team.
            if len({m["person_id"] for m in cluster}) >= 2:
                offices = {m["office_jurisdiction"] for m in cluster}
                team = conn.execute(
                    """
                    INSERT INTO team_moves
                        (from_firm_id, to_firm_id, office_jurisdiction,
                         window_start, window_end)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """,
                    (
                        from_firm, to_firm,
                        offices.pop() if len(offices) == 1 else None,
                        min(m["announced_date"] for m in cluster),
                        max(m["announced_date"] for m in cluster),
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE moves SET team_move_id = %s WHERE id = ANY(%s)",
                    (team["id"], [m["id"] for m in cluster]),
                )
                created += 1
            cluster = [row] if row is not None else []
    return created


def run(conn, recorder=None, *, apply: bool = False) -> dict:
    """Score every blocked pair; merge, queue or leave alone.

    With `apply` false nothing is written and the same counts come back, so the
    decision can be read before it is taken.
    """
    rows = conn.execute(_CANDIDATE_SQL).fetchall()
    blocks = _blocks(rows)

    by_id = {row["id"]: row for row in rows}
    union = _Union()
    ambiguous: list[tuple] = []
    compared = 0

    for block in blocks.values():
        for a, b in _pairs(block):
            compared += 1
            result = scoring.score_pair(a, b)
            if result.merges:
                union.union(a["id"], b["id"])
            elif result.ambiguous:
                ambiguous.append((a["id"], b["id"], result))

    components = union.components()
    # A row that merges is no longer canonical, so a pair it was merely
    # ambiguous with is a question about a row that will not exist. Dropped
    # here rather than at write time so the dry run reports what apply does.
    merged_ids = {move_id for members in components.values() for move_id in members}
    ambiguous = [
        pair for pair in ambiguous
        if pair[0] not in merged_ids and pair[1] not in merged_ids
    ]

    stats = {
        "candidates": len(rows),
        "blocks": len(blocks),
        "pairs_compared": compared,
        "components": len(components),
        "moves_superseded": sum(len(m) for m in components.values()),
        "merged": 0,
        "queued": 0,
        "teams": 0,
        "applied": apply,
    }

    if not apply:
        stats["merged"] = len(components)
        stats["queued"] = len(ambiguous)
        return stats

    for members in components.values():
        _, needs_human = _merge_component(conn, [by_id[i] for i in members])
        stats["merged"] += 1
        if needs_human:
            stats["queued"] += 1
        if recorder is not None:
            recorder.moves_merged += 1

    for a_id, b_id, result in ambiguous:
        if _queue(conn, a_id, "dedupe_ambiguous", result.as_dict(), related_move_id=b_id):
            stats["queued"] += 1
            if recorder is not None:
                recorder.moves_queued_for_review += 1

    stats["teams"] = _detect_team_moves(conn)
    conn.commit()
    return stats
