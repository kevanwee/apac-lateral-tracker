"""Final confidence: a function of evidence, not the model's opinion of itself.

The model returns a self-reported confidence and it is worth something, but on
its own it is close to useless — it reflects how fluent the article was, not
whether the reading is right. It gets a 10% weight and no more.

The rest is evidence you can check:

  source tier          who said it (tier 1 is the firm itself)
  access level         how much of the item we were allowed to see
  field completeness   how much of the record the text actually supported
  span quality         whether the model could point at the text for each field
  corroboration        how many independent sources reported the same move

Corroboration is zero at extract time by construction — one item is one source.
It is recomputed at merge time in Phase 4, which is why the components are
stored on the row rather than only the total.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Above this, a move is accepted without a human looking at it.
AUTO_ACCEPT_THRESHOLD = 0.85

TIER_BASE = {1: 0.90, 2: 0.78, 3: 0.62}

ACCESS_FACTOR = {
    "full_public": 1.00,
    "summary": 0.96,
    # A headline states little and implies a lot. Phase 0, section 3.
    "headline_only": 0.80,
}

# Fields that carry analytical weight, and how much each contributes to
# completeness. person and to_firm are not here: without them there is no
# record at all, and extraction rejects rather than scoring.
COMPLETENESS_WEIGHTS = {
    "from_firm": 0.30,
    "title_to": 0.15,
    "office_jurisdiction": 0.25,
    "practice_text": 0.25,
    "partner_tier": 0.05,
}

SPAN_QUALITY_SCORE = {"exact": 1.0, "recovered": 0.75}

WEIGHTS = {
    "tier": 0.45,
    "completeness": 0.25,
    "span_quality": 0.20,
    "self_reported": 0.10,
}

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
    corroboration_count: int
    corroboration_bonus: float
    total: float

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
    corroboration_count: int = 1,
) -> ConfidenceComponents:
    tier_base = TIER_BASE.get(reliability_tier, 0.60)
    access = ACCESS_FACTOR.get(access_level, 0.90)
    comp = completeness(present_fields)
    spans = span_quality(span_qualities)
    # A model that reports 0.99 on everything should not be able to lift a
    # thin record over the line on its own; the weight is what limits it.
    self_reported = max(0.0, min(1.0, float(self_reported or 0.0)))

    base = (
        WEIGHTS["tier"] * tier_base
        + WEIGHTS["completeness"] * comp
        + WEIGHTS["span_quality"] * spans
        + WEIGHTS["self_reported"] * self_reported
    ) * access

    bonus = min(
        CORROBORATION_CAP, CORROBORATION_STEP * max(0, corroboration_count - 1)
    )

    return ConfidenceComponents(
        tier_base=round(tier_base, 3),
        access_factor=access,
        completeness=round(comp, 3),
        span_quality=round(spans, 3),
        self_reported=round(self_reported, 3),
        corroboration_count=corroboration_count,
        corroboration_bonus=round(bonus, 3),
        total=round(min(1.0, base + bonus), 3),
    )


def needs_review(total: float) -> bool:
    return total < AUTO_ACCEPT_THRESHOLD
