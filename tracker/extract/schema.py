"""The extraction contract.

An explicit JSON schema rather than a Pydantic model, because this schema is
the interface between the model and the database and it should be readable as
a document, not inferred from Python classes.

Every extracted value is wrapped in `{value, span_start, span_end}`. The span
is structural, not optional: a field that cannot point at the text that states
it is dropped before it reaches the database. That is what stops the model
filling in a plausible firm or practice that the article never mentioned.
"""

from __future__ import annotations

SCHEMA_VERSION = "extract/1.0.0"

# Enum values must match the database types in migration 0001 exactly.
MOVE_TYPES = [
    "lateral",
    "promotion",
    "in_house_exit",
    "in_house_entry",
    "retirement",
    "firm_launch",
    "merger_absorbed",
]
PARTNER_TIERS = ["equity", "salaried", "undisclosed"]


def _spanned(description: str, *, enum: list[str] | None = None) -> dict:
    """A value that must point at the text stating it."""
    value: dict = {"type": "string", "description": description}
    if enum:
        value["enum"] = enum
    return {
        "type": ["object", "null"],
        "properties": {
            "value": value,
            "span_start": {
                "type": "integer",
                "description": "Character offset in the source text where the "
                "supporting phrase begins.",
            },
            "span_end": {
                "type": "integer",
                "description": "Character offset just past the supporting phrase.",
            },
        },
        "required": ["value", "span_start", "span_end"],
        "additionalProperties": False,
    }


MOVE_SCHEMA = {
    "type": "object",
    "properties": {
        "person_name": _spanned(
            "The individual's name exactly as the text spells it, including any "
            "Western given name in brackets and any post-nominals. Do not "
            "reorder, translate or normalise it."
        ),
        "from_firm": _spanned(
            "The organisation the person is leaving, exactly as named in the "
            "text. Null if the text does not say where they came from. Never "
            "infer it from the person's practice area or seniority."
        ),
        "to_firm": _spanned(
            "The organisation the person is joining, exactly as named in the "
            "text. For a multi-entity or verein firm, use the specific entity "
            "the text names (for example 'Rajah & Tann Thailand', not 'Rajah & "
            "Tann Asia') and do not expand it to the network brand."
        ),
        "title_from": _spanned("Their title before the move, if the text states it."),
        "title_to": _spanned("Their title after the move, if the text states it."),
        "partner_tier": _spanned(
            "Only if the text says so explicitly: 'equity partner' or 'equity' "
            "gives equity, 'salaried partner' or 'fixed-share' gives salaried. "
            "If the text just says 'partner', this is undisclosed.",
            enum=PARTNER_TIERS,
        ),
        "office_jurisdiction": _spanned(
            "The office or market this move concerns, as an ISO 3166-1 alpha-2 "
            "code, optionally with a subdivision: SG, HK, AU, AU-NSW, CN, GB. "
            "Only where the text names a city, office or market. A firm being "
            "headquartered somewhere is not where this move happened."
        ),
        "practice_text": _spanned(
            "The practice area exactly as the text words it — 'finance and "
            "projects', 'contentious construction'. Do not map it to a "
            "category; that happens later against a fixed taxonomy."
        ),
        "sector_text": _spanned(
            "An industry sector the text names for this person's work, such as "
            "fintech, energy, healthcare. Separate from practice area. Null "
            "unless the text actually names an industry."
        ),
        "effective_date_text": _spanned(
            "When the move takes or took effect, as the text words it "
            "('from 1 March', 'later this year'). Null if not stated."
        ),
        "move_type": _spanned(
            "lateral: firm to firm. promotion: a rank change inside one firm. "
            "in_house_exit: private practice to a company legal team. "
            "in_house_entry: a company legal team to private practice. "
            "retirement: leaving practice. firm_launch: founding a new firm. "
            "merger_absorbed: arrived through a firm combination.",
            enum=MOVE_TYPES,
        ),
        "team_size": {
            "type": ["integer", "null"],
            "description": "Total partners in this move if the text describes a "
            "team or group move ('a five-partner team' gives 5). Null for an "
            "individual move.",
        },
        "self_confidence": {
            "type": "number",
            "description": "0 to 1. How confident you are that this is a real, "
            "correctly read partner-level movement.",
        },
    },
    "required": [
        "person_name",
        "from_firm",
        "to_firm",
        "title_from",
        "title_to",
        "partner_tier",
        "office_jurisdiction",
        "practice_text",
        "sector_text",
        "effective_date_text",
        "move_type",
        "team_size",
        "self_confidence",
    ],
    "additionalProperties": False,
}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_movement": {
            "type": "boolean",
            "description": "True only if the text reports one or more named "
            "individuals at partner level or equivalent changing role or "
            "organisation.",
        },
        "not_movement_reason": {
            "type": ["string", "null"],
            "description": "If is_movement is false, one short phrase saying "
            "what the item is instead.",
        },
        "moves": {
            "type": "array",
            "description": "One entry per person who moved. A five-partner team "
            "move named in full gives five entries; a team move where only some "
            "names are given gives one entry per named person, each with "
            "team_size set.",
            "items": MOVE_SCHEMA,
        },
    },
    "required": ["is_movement", "not_movement_reason", "moves"],
    "additionalProperties": False,
}
