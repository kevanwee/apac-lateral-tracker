"""Span verification: the guard that makes "never infer" enforceable.

The model is instructed to point every field at the text that states it. This
module checks that it did. A field whose span does not actually contain its
value is dropped — not corrected, not trusted with a lower score. Dropping is
cheap; a fabricated firm name in a market dataset is not.

Three outcomes per field:

  exact     the span contains the value, allowing for case and punctuation
  recovered the span was wrong but the value does occur verbatim in the text,
            so the span is repaired to the real location and the field is kept
            with a quality penalty
  dropped   the value does not occur in the text at all

`recovered` exists because models are reliably good at quoting and unreliably
good at counting characters. Rejecting a correct value because its offsets were
off by four would cost real coverage for no accuracy gain. A recovered span is
still proof the text contains the value, which is the property that matters.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

SpanQuality = Literal["exact", "recovered", "dropped"]

# Words per excerpt, capped by the database too (Phase 0, section 2).
EXCERPT_WORD_CAP = 25
# Characters of context kept around a span for the provenance excerpt.
EXCERPT_PADDING = 60


@dataclass(frozen=True)
class VerifiedField:
    name: str
    value: str
    span_start: int
    span_end: int
    quality: SpanQuality
    excerpt: str

    @property
    def kept(self) -> bool:
        return self.quality != "dropped"


def _fold(text: str) -> str:
    """Case, accent and punctuation insensitive form for comparison only."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def _excerpt(text: str, start: int, end: int) -> str:
    """A short provenance excerpt around the span, capped at 25 words."""
    left = max(0, start - EXCERPT_PADDING)
    right = min(len(text), end + EXCERPT_PADDING)
    window = " ".join(text[left:right].split())
    words = window.split(" ")
    if len(words) > EXCERPT_WORD_CAP:
        window = " ".join(words[:EXCERPT_WORD_CAP])
    return window


def _find_verbatim(text: str, value: str) -> tuple[int, int] | None:
    """Locate the value in the text, tolerating case and whitespace runs."""
    exact = text.find(value)
    if exact != -1:
        return exact, exact + len(value)

    lowered = text.lower().find(value.lower())
    if lowered != -1:
        return lowered, lowered + len(value)

    # Whitespace in the value may not match whitespace in the text (feeds
    # collapse newlines inconsistently).
    pattern = r"\s+".join(re.escape(part) for part in value.split())
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.start(), match.end()
    return None


def verify(
    name: str,
    field: dict | None,
    text: str,
    *,
    value_must_appear: bool = True,
) -> VerifiedField | None:
    """Check one `{value, span_start, span_end}` object against the source text.

    `value_must_appear` is False for fields whose value is a code the text will
    not contain literally — office_jurisdiction returns "HK" for "Hong Kong",
    move_type returns "lateral" for "joins". For those, the span still has to
    be a real, non-empty region of the text; it just cannot be string-matched.
    """
    if not field:
        return None

    value = (field.get("value") or "").strip()
    if not value:
        return None

    start = field.get("span_start")
    end = field.get("span_end")
    span_is_sane = (
        isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(text)
    )

    if not value_must_appear:
        if not span_is_sane:
            return VerifiedField(name, value, 0, 0, "dropped", "")
        return VerifiedField(name, value, start, end, "exact", _excerpt(text, start, end))

    if span_is_sane and _fold(value) and _fold(value) in _fold(text[start:end]):
        return VerifiedField(name, value, start, end, "exact", _excerpt(text, start, end))

    found = _find_verbatim(text, value)
    if found is None:
        return VerifiedField(name, value, 0, 0, "dropped", "")

    start, end = found
    return VerifiedField(name, value, start, end, "recovered", _excerpt(text, start, end))
