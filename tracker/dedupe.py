"""Phase 4 pair scoring — is this the same move reported twice?

Blocking has already done its half by the time anything here runs: two moves
reach `score_pair` only if they share a normalised surname, a destination firm
and a 60-day window. That is a narrow enough funnel that most surviving pairs
really are one event reported twice. The job here is to catch the ones that
are not.

## The failure that is worse

Two partners with the same surname joining the same firm in the same quarter is
not a hypothetical — it is what a team move looks like. "Sarah Chen" and
"Michael Chen" joining Allen & Gledhill in March are two records and must stay
two records; merging them deletes a partner from the dataset and inflates the
confidence of the row that survives. So the given name is a gate, not a
weighting: when both sides state a given name and the two disagree, the pair is
`distinct` and no other agreement can rescue it.

Everything else — origin firm, office, title, how far apart the two reports
are — only moves a pair that already passed that gate.

## Bands

    >= MERGE_THRESHOLD      merge, creating a new canonical row
    AMBIGUOUS_THRESHOLD..   review queue, reason `dedupe_ambiguous`
    below                   leave both rows alone

The middle band is the one the schema was built for: `review_queue` carries
`related_move_id` precisely so a human decides the pairs the rule will not.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from tracker import names

# A pair at or above this merges without a human.
MERGE_THRESHOLD = 0.85
# Below this the two rows are left alone. Between the two they go to review.
AMBIGUOUS_THRESHOLD = 0.60

# Blocking window, each side of the earlier announcement.
WINDOW_DAYS = 60

# A pair that clears the given-name gate starts here and is adjusted.
#
# Set so that an exact given-name match on its own merges: by that point
# blocking has already established the same surname, the same destination firm
# and a 60-day window, and the given names agree outright. Requiring a fourth
# agreement on top of that would leave genuine duplicates unmerged in the
# common case where one of the two outlets never named the origin firm.
#
# Everything weaker than an exact match lands below the line and needs
# corroboration to reach it, which is the intended asymmetry.
BASE = 0.85

AGREE_FROM_FIRM = 0.10
CONFLICT_FROM_FIRM = -0.15
AGREE_JURISDICTION = 0.05
CONFLICT_JURISDICTION = -0.10
AGREE_TITLE = 0.05
# Same-day reporting is worth a little; the far edge of the window is worth
# nothing. Never negative — the window itself is the date test.
PROXIMITY_MAX = 0.05

_SEPARATORS = re.compile(r"[\s-]+")


def _compact(text: str | None) -> str:
    return _SEPARATORS.sub("", (text or "").strip().lower())


def _name_keys(raw: str) -> tuple[str, str]:
    """(surname key, given key) for one spelling."""
    return names.surname_key(raw), _compact(names.given_key(raw) or "")


def _all_keys(name: str, variants: list[str] | None) -> set[tuple[str, str]]:
    """Every (surname, given) pair this person is known by."""
    out = {_name_keys(name)}
    for variant in variants or ():
        out.add(_name_keys(variant))
    return {k for k in out if k[0] or k[1]}


def _initial_compatible(one: list[str], other: list[str]) -> bool:
    """'J Smith' against 'John Smith'. One side abbreviates the other."""
    if not one or not other:
        return False
    short, long_ = (one, other) if len(one[0]) < len(other[0]) else (other, one)
    return len(short[0]) == 1 and long_[0].startswith(short[0])


@dataclass(frozen=True)
class GivenMatch:
    score: float
    label: str


def compare_given(
    name_a: str,
    name_b: str,
    *,
    variants_a: list[str] | None = None,
    variants_b: list[str] | None = None,
) -> GivenMatch:
    """How much the two given names agree, on 0..1.

    0.0 is a hard disagreement and stops the pair. 0.5 means one side simply
    did not state a given name, which is weak evidence either way — common in
    headline-only items, where the outlet wrote "Chen joins ...".
    """
    keys_a, keys_b = _all_keys(name_a, variants_a), _all_keys(name_b, variants_b)

    # A spelling in common settles it: this is how 'Wei Ming Tan' meets
    # 'Kevin Tan', whose bracketed alias is already in people.name_variants.
    shared = {k for k in keys_a & keys_b if k[1]}
    if shared:
        return GivenMatch(1.0, "a spelling in common")

    given_a, given_b = names.given_tokens(name_a), names.given_tokens(name_b)
    if not given_a or not given_b:
        return GivenMatch(0.5, "one side states no given name")

    if _compact(" ".join(given_a)) == _compact(" ".join(given_b)):
        return GivenMatch(1.0, "given names match")

    if given_a[0] == given_b[0]:
        # "Sarah Jane Chen" against "Sarah Chen": a middle name appears in one
        # report and not the other. Very common, and not a disagreement.
        return GivenMatch(0.9, "first given name matches, middle differs")

    if _initial_compatible(given_a, given_b):
        return GivenMatch(0.75, "one given name is an initial of the other")

    return GivenMatch(0.0, "given names disagree")


def _agreement(a, b, *, agree: float, conflict: float) -> tuple[float, str | None]:
    """Score a field that may be null on either side.

    A field only one side states is not disagreement. Trade press omits the
    origin firm constantly; treating silence as conflict would block exactly
    the merges that matter.
    """
    if a is None or b is None:
        return 0.0, None
    if a == b:
        return agree, "agree"
    return conflict, "conflict"


def _proximity(days_apart: int) -> float:
    if days_apart >= WINDOW_DAYS:
        return 0.0
    return round(PROXIMITY_MAX * (1 - days_apart / WINDOW_DAYS), 4)


@dataclass(frozen=True)
class PairScore:
    total: float
    decision: str
    given: GivenMatch
    components: dict = field(default_factory=dict)

    @property
    def merges(self) -> bool:
        return self.decision == "merge"

    @property
    def ambiguous(self) -> bool:
        return self.decision == "ambiguous"

    def as_dict(self) -> dict:
        out = asdict(self)
        out["given"] = {"score": self.given.score, "label": self.given.label}
        return out


def score_pair(a: dict, b: dict) -> PairScore:
    """Score two blocked moves.

    Each argument is a row-like mapping carrying `person_name`, `name_variants`,
    `from_firm_id`, `office_jurisdiction`, `title_to` and `announced_date`.
    Destination firm and surname are not scored: blocking already required them
    to agree, so scoring them again would only inflate every total.
    """
    given = compare_given(
        a.get("person_name") or "",
        b.get("person_name") or "",
        variants_a=a.get("name_variants"),
        variants_b=b.get("name_variants"),
    )

    components: dict = {"given": given.label, "given_score": given.score}

    # The gate. Nothing below can rescue a pair whose given names disagree.
    if given.score == 0.0:
        return PairScore(0.0, "distinct", given, components)

    total = BASE * given.score

    from_delta, from_label = _agreement(
        a.get("from_firm_id"), b.get("from_firm_id"),
        agree=AGREE_FROM_FIRM, conflict=CONFLICT_FROM_FIRM,
    )
    juris_delta, juris_label = _agreement(
        a.get("office_jurisdiction"), b.get("office_jurisdiction"),
        agree=AGREE_JURISDICTION, conflict=CONFLICT_JURISDICTION,
    )
    title_delta, title_label = _agreement(
        _compact(a.get("title_to")) or None, _compact(b.get("title_to")) or None,
        agree=AGREE_TITLE, conflict=0.0,
    )

    days = abs((a["announced_date"] - b["announced_date"]).days)
    proximity = _proximity(days)

    total += from_delta + juris_delta + title_delta + proximity
    total = round(max(0.0, min(1.0, total)), 4)

    components.update(
        from_firm=from_label, from_firm_delta=from_delta,
        jurisdiction=juris_label, jurisdiction_delta=juris_delta,
        title_to=title_label, title_delta=title_delta,
        days_apart=days, proximity=proximity,
    )

    if total >= MERGE_THRESHOLD:
        decision = "merge"
    elif total >= AMBIGUOUS_THRESHOLD:
        decision = "ambiguous"
    else:
        decision = "distinct"

    return PairScore(total, decision, given, components)
