"""Split a role phrase into title, practice area and location.

The templates capture whatever sits after "as" or "to", which in real coverage
is a whole clause carrying three different facts at once:

    "a partner in its white-collar defence and investigations practice"
    "the firm's New York office as a partner in the investment funds group"
    "a partner on its antitrust and competition team in Sydney"

Stored whole, that is a bad title, a missing practice and a missing
jurisdiction. Split, it is all three:

    title="partner"  practice="white-collar defence and investigations"
    title="partner"  practice="investment funds"  location=US
    title="partner"  practice="antitrust and competition"  location=AU-NSW

Everything here is conservative: a part that cannot be identified comes back
None rather than guessed, because a wrong practice area is worse in a trend
dataset than an absent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tracker.geo import PlaceGazetteer

# Seniority qualifiers that belong with the title, not stripped from it.
_QUALIFIER = (
    r"(?:managing|senior|junior|equity|salaried|fixed[- ]share|consulting|"
    r"regional|global|national|local|deputy|acting|interim|executive|"
    r"co[- ]?|vice|assistant|associate|general|special|of)"
)
_HEAD_NOUN = (
    r"(?:partners?|principals?|counsel|heads?|chairs?|chairmen|chairwomen|"
    r"directors?|partner[- ]in[- ]charge|silks?)"
)

# The title itself: optional qualifiers then a head noun.
TITLE = re.compile(
    # "co-head" and "vice-chair" attach with a hyphen and no space, so the
    # prefix cannot be part of the space-separated qualifier run.
    rf"\b(?:{_QUALIFIER}\s+){{0,3}}(?:(?:co|vice|deputy)[-\s]?)?"
    rf"{_HEAD_NOUN}(?:\s+in\s+charge)?\b",
    re.IGNORECASE,
)

# Leading filler the templates always drag in.
_LEADING = re.compile(
    r"^(?:a|an|the|its|their|his|her|new|newest|first|second|next|"
    r"firm[''’]?s?|company[''’]?s?)\s+",
    re.IGNORECASE,
)

# A practice area sits between a preposition and a practice noun:
#   "in its white-collar defence and investigations practice"
#   "on its antitrust and competition team"
#   "of Fraud, Asset Recovery & Investigations"
PRACTICE_BOUNDED = re.compile(
    r"\b(?:in|on|of|to|within|for)\s+(?:the\s+|its\s+|their\s+|a\s+|an\s+)?"
    r"(?:firm[''’]?s?\s+)?(?P<practice>[\w&,''’\- ]{3,70}?)\s+"
    r"(?:practice\s+group|practice|team|group|department|division|bench)\b",
    re.IGNORECASE,
)
# Same idea without the trailing noun: "... as a partner in investment funds"
PRACTICE_TRAILING = re.compile(
    r"\b(?:in|on|of|to|within|for)\s+(?:the\s+|its\s+|their\s+|a\s+|an\s+)?"
    r"(?:firm[''’]?s?\s+)?(?P<practice>[\w&,''’\- ]{3,70})$",
    re.IGNORECASE,
)

# Words that are never a practice area, however the sentence is shaped.
_NOT_PRACTICE = {
    "office", "offices", "partnership", "partner", "partners", "firm",
    "team", "group", "practice", "role", "position", "market", "region",
    "business", "leadership", "succession", "round", "promotion",
    "promotions", "company", "bench", "seat", "charge", "board",
}

# Trailing noise the templates pick up after the useful part.
_TAIL_NOISE = re.compile(
    r"\s+(?:as\s+part\s+of|in\s+planned|to\s+boost|to\s+expand|to\s+lead|"
    r"amid|after|following|ahead\s+of|while|which|who|that)\b.*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleParts:
    title: str | None
    practice: str | None
    jurisdiction: str | None

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.practice or self.jurisdiction)


_AND_ARTICLE = re.compile(r"\s+and\s+(?:a|an|the|its)\s+.*$", re.IGNORECASE)


def clean_practice(raw: str) -> str | None:
    text = _AND_ARTICLE.sub("", raw).strip(" ,;-–—").strip()
    text = re.sub(
        r"^(?:of|in|on|for|to|at|the|its|a|an|their|new|newest)\s+",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"^(?:the|its|a|an)\s+", "", text, flags=re.IGNORECASE)
    if not text or len(text) < 3:
        return None
    words = [w for w in re.split(r"[\s,]+", text.lower()) if w]
    if not words:
        return None
    # A phrase made only of structural words carries no practice information.
    if all(w in _NOT_PRACTICE or w in {"and", "&", "the", "of", "its"} for w in words):
        return None
    # Trailing structural noun: "corporate practice" -> "corporate".
    while words and words[-1] in _NOT_PRACTICE:
        words.pop()
        text = " ".join(text.split()[: len(words)])
    if not words:
        return None
    return text.strip(" ,;-").strip() or None


def parse(phrase: str | None, places: PlaceGazetteer | None = None) -> RoleParts:
    """Pull title, practice and jurisdiction out of a captured role phrase."""
    if not phrase:
        return RoleParts(None, None, None)

    text = _TAIL_NOISE.sub("", phrase.strip())
    text = _LEADING.sub("", text).strip(" ,;.")

    title_match = TITLE.search(text)
    title = None
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(0)).strip()
        # "co - head" from a hyphen split.
        title = title.replace("co- ", "co-").replace("co -", "co-")

    practice = None
    # Prefer the bounded form; it delimits the practice on both sides.
    for pattern in (PRACTICE_BOUNDED, PRACTICE_TRAILING):
        for match in pattern.finditer(text):
            candidate = clean_practice(match.group("practice"))
            if candidate and (not title or candidate.lower() not in title.lower()):
                practice = candidate
                break
        if practice:
            break

    jurisdiction = places.find(text) if places else None

    # A practice that is really a place is not a practice: "in Sydney" is a
    # location, and "South Asia Practice" is a region wearing the word.
    if practice and places:
        looks_geographic = places.find(practice) or places.is_region(practice)
        if looks_geographic and len(practice.split()) <= 3:
            practice = None

    return RoleParts(title=title, practice=practice, jurisdiction=jurisdiction)
