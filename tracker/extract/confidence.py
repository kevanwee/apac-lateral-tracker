"""Final confidence: a function of evidence, not the model's opinion of itself.

Confidence answers one question — should a human look at this before it enters
the dataset? Not "how rich is this record".

## The calibration mistake this file used to make

The first version weighted field completeness at 25% of the score, which
conflated two unrelated things: *the extractor missed a field* and *the article
never stated it*. Trade press routinely reports a move without naming the
practice or the origin firm, and the extraction prompt requires null in that
case. So a flawless extraction of a sparse item was penalised for obeying its
own instructions.

Measured against the gold set, that put 79% of **perfect** extractions into the
review queue against a 15% ceiling. Sparseness is not unreliability: "Emily
Harlan succeeds Brandon Asbill, who is retiring" is thin and completely
trustworthy.

So richness now only nudges the score. What drives it is evidence quality:

  source tier      who said it — a firm's own announcement, or an aggregator
  access level     how much of the item the outlet let us see
  span quality     whether every field could be pointed at the text
  dropped fields   how often the model asserted something unsupported

That last one is the strongest per-record signal of a bad reading, and it is
free: it is exactly what span verification already rejected.

Corroboration is 1 at extract time by construction — one item is one source.
It is recomputed at merge time in Phase 4, which is why the components are
stored on the row rather than only the total.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Above this, a move is accepted without a human looking at it.
AUTO_ACCEPT_THRESHOLD = 0.85

# P(this record is correct) given only who reported it. Established trade press
# gets partner moves right — the firms brief them — so tier 2 sits high. Tier 3
# aggregators sit below the threshold on their own, by design: an aggregator
# alone should never enter the dataset unreviewed.
TIER_BASE = {1: 0.95, 2: 0.88, 3: 0.70}

ACCESS_FACTOR = {
    "full_public": 1.00,
    "summary": 0.98,
    # A headline states little and implies a lot. Phase 0, section 3.
    "headline_only": 0.88,
}

# A recovered span is still proof the value occurs in the text, so it costs
# little. The floor of 0.90 keeps span quality from dominating.
SPAN_QUALITY_SCORE = {"exact": 1.0, "recovered": 0.90}
SPAN_FLOOR = 0.90

# Richness, as a small bonus rather than a multiplier. Present fields raise
# confidence; absent ones no longer sink it.
COMPLETENESS_WEIGHTS = {
    "from_firm": 0.30,
    "title_to": 0.15,
    "office_jurisdiction": 0.25,
    "practice_text": 0.25,
    "partner_tier": 0.05,
}
COMPLETENESS_BONUS = 0.05

# Every field whose span failed verification is a field the model asserted and
# the text did not support. One is a slip; two is a pattern.
DROPPED_FIELD_PENALTY = 0.06
DROPPED_FIELDS_FORCING_REVIEW = 2

# The model's own confidence cannot raise a score — only flag a low one.
# "Return a self-reported confidence, but do not trust it alone."
SELF_REPORTED_REVIEW_FLOOR = 0.60

# Each additional independent source adds this much, capped.
CORROBORATION_STEP = 0.04
CORROBORATION_CAP = 0.10


@dataclass(frozen=True)
class ConfidenceComponents:
    tier_base: float
    access_factor: float
    completeness: float
    span_quality: float
    self_reported: float
    dropped_fields: int
    dropped_penalty: float
    corroboration_count: int
    corroboration_bonus: float
    total: float
    forced_review_reason: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def completeness(present_fields: set[str]) -> float:
    earned = sum(w for f, w in COMPLETENESS_WEIGHTS.items() if f in present_fields)
    return earned / sum(COMPLETENESS_WEIGHTS.values())


def span_quality(qualities: list[str]) -> float:
    """Mean quality across the fields that survived verification."""
    if not qualities:
        return 0.0
    return sum(SPAN_QUALITY_SCORE.get(q, 0.0) for q in qualities) / len(qualities)


def score(
    *,
    reliability_tier: int,
    access_level: str,
    present_fields: set[str],
    span_qualities: list[str],
    self_reported: float,
    dropped_fields: int = 0,
    corroboration_count: int = 1,
) -> ConfidenceComponents:
    tier_base = TIER_BASE.get(reliability_tier, 0.60)
    access = ACCESS_FACTOR.get(access_level, 0.90)
    comp = completeness(present_fields)
    spans = span_quality(span_qualities)
    self_reported = max(0.0, min(1.0, float(self_reported or 0.0)))

    span_factor = SPAN_FLOOR + (1.0 - SPAN_FLOOR) * spans
    dropped_penalty = DROPPED_FIELD_PENALTY * max(0, dropped_fields)
    bonus = min(CORROBORATION_CAP, CORROBORATION_STEP * max(0, corroboration_count - 1))

    total = (
        tier_base * access * span_factor
        + COMPLETENESS_BONUS * comp
        + bonus
        - dropped_penalty
    )
    total = max(0.0, min(1.0, total))

    # Two independent grounds for review that a score alone would hide.
    forced: str | None = None
    if dropped_fields >= DROPPED_FIELDS_FORCING_REVIEW:
        forced = f"{dropped_fields} fields asserted without support in the text"
    elif self_reported < SELF_REPORTED_REVIEW_FLOOR:
        forced = f"model reported low confidence in its own reading ({self_reported:.2f})"

    return ConfidenceComponents(
        tier_base=round(tier_base, 3),
        access_factor=access,
        completeness=round(comp, 3),
        span_quality=round(spans, 3),
        self_reported=round(self_reported, 3),
        dropped_fields=dropped_fields,
        dropped_penalty=round(dropped_penalty, 3),
        corroboration_count=corroboration_count,
        corroboration_bonus=round(bonus, 3),
        total=round(total, 3),
        forced_review_reason=forced,
    )


def needs_review(components: ConfidenceComponents | float) -> bool:
    """Accepts either the components or a bare total, for callers that only have one."""
    if isinstance(components, ConfidenceComponents):
        return (
            components.forced_review_reason is not None
            or components.total < AUTO_ACCEPT_THRESHOLD
        )
    return components < AUTO_ACCEPT_THRESHOLD
