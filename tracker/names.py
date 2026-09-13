"""Name normalisation — minimal version.

**This is a placeholder for the Phase 4 utility.** It handles enough to create
a blocking key, and no more. The full treatment — romanisation variants,
surname-first ordering, Western given names alongside legal names, honorifics
and post-nominals, with the fixture file of real-world variants — is the
Phase 4 deliverable, and gold-s001 and gold-s004 exist to test it.

Until then, the surname key this produces will be wrong for surname-first
names and for names with multi-word surnames. That means Phase 4 dedupe would
under-merge, not over-merge, which is the safer of the two failures to ship
with.
"""

from __future__ import annotations

import re
import unicodedata

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "hon"}
POST_NOMINALS = {
    "sc", "kc", "qc", "llb", "llm", "jd", "ba", "ma", "phd", "faciarb", "ficarb",
}

_BRACKETED = re.compile(r"\(([^)]*)\)")
_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def tokens(raw: str) -> list[str]:
    """Name tokens with honorifics, post-nominals and bracketed aliases removed."""
    without_brackets = _BRACKETED.sub(" ", raw)
    cleaned = _PUNCT.sub(" ", strip_accents(without_brackets)).lower()
    parts = [p for p in cleaned.split() if p]
    return [
        p for p in parts if p not in HONORIFICS and p not in POST_NOMINALS
    ]


def bracketed_alias(raw: str) -> str | None:
    """The Western given name in 'Wei Ming (Kevin) Tan', if there is one."""
    match = _BRACKETED.search(raw)
    if not match:
        return None
    inner = match.group(1).strip()
    return inner or None


def surname_key(raw: str) -> str:
    """Blocking key.

    Assumes given-name-first ordering, which is wrong for the surname-first
    names Phase 4 must handle. Documented rather than silently approximated.
    """
    parts = tokens(raw)
    if not parts:
        return ""
    return parts[-1]


def given_key(raw: str) -> str | None:
    parts = tokens(raw)
    return parts[0] if len(parts) > 1 else None


def variants(raw: str) -> list[str]:
    """Every spelling of this name we have seen or can derive from this string."""
    seen = {raw.strip()}
    alias = bracketed_alias(raw)
    if alias:
        parts = tokens(raw)
        if parts:
            seen.add(f"{alias} {parts[-1].title()}")
    return sorted(s for s in seen if s)
