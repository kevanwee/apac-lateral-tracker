"""Firm gazetteer: recognising a firm name in running text.

Loaded from config/firms.yaml. Used for two things:

  * seeding `firms` and `firm_aliases`, so extraction resolves a known firm
    instead of creating a row and sending the move to review
  * letting the rule-based extractor tell a firm from a person in text that has
    lost its capitalisation — "herbert smith freehills appoints nick baker"
    is unparseable without knowing which half is the firm

Matching is longest-first and case-insensitive, over a punctuation-normalised
form, so "Herbert Smith Freehills" also matches "herbert smith freehills" from
a URL slug and "Herbert Smith Freehills'" from running prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "firms.yaml"

# Ampersands, dots and possessives vary constantly in how outlets write a firm
# name, and a URL slug drops all of them.
_AND = re.compile(r"\s*(?:&|\+|\band\b)\s*")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Comparison form: lowercase, ampersands folded, punctuation dropped."""
    text = _AND.sub(" ", text.lower())
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class FirmMatch:
    canonical_name: str
    matched_text: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class FirmEntry:
    canonical_name: str
    firm_type: str
    hq_jurisdiction: str | None
    aliases: tuple[str, ...]


@dataclass
class FirmGazetteer:
    entries: list[FirmEntry] = field(default_factory=list)
    # normalised surface form -> canonical name
    _index: dict[str, str] = field(default_factory=dict, repr=False)
    _pattern: re.Pattern[str] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> FirmGazetteer:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries = [
            FirmEntry(
                canonical_name=e["name"],
                firm_type=e.get("type", "other"),
                hq_jurisdiction=e.get("hq"),
                aliases=tuple(e.get("aliases") or []),
            )
            for e in raw["firms"]
        ]
        return cls(entries=entries).build()

    def tokens(self) -> set[str]:
        """Every word appearing in any firm surface, for slug filtering.

        Word level rather than surface level, for the same reason as the
        place gazetteer: a URL slug arrives already split on hyphens.
        """
        words: set[str] = set()
        for surface in self._index:
            for word in re.split(r"[^a-z]+", surface.lower()):
                if len(word) > 1:
                    words.add(word)
        return words

    def build(self) -> FirmGazetteer:
        index: dict[str, str] = {}
        for entry in self.entries:
            for surface in (entry.canonical_name, *entry.aliases):
                key = normalise(surface)
                if not key:
                    continue
                # A longer canonical name wins a collision: "Rajah & Tann
                # Singapore" should not be shadowed by "Rajah & Tann".
                if key not in index or len(entry.canonical_name) > len(index[key]):
                    index[key] = entry.canonical_name
        self._index = index

        # Longest-first alternation, so "Rajah & Tann Singapore" is preferred
        # over "Rajah & Tann" at the same position.
        surfaces = sorted(index, key=len, reverse=True)
        if surfaces:
            joined = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in surfaces)
            # The group around the alternation is load-bearing. Without it,
            # `(?<!\w)a|b|c(?!\w)` parses as `((?<!\w)a) | (b) | (c(?!\w))` —
            # alternation binds looser than concatenation, so every surface but
            # the first and last matched with no boundary check at all. That let
            # the two-letter aliases match inside ordinary words: "EY" inside
            # "Cool-ey-", "A&G" ("a g") inside "Mide-a G-roup", "G+T" ("g t")
            # inside "Chan-g T-si", "S&C" ("s c") inside "grow-s c-apital".
            # Measured on the ABLJ backfill, this was the single largest source
            # of wrong destination firms.
            self._pattern = re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)
        return self

    def find(self, text: str) -> list[FirmMatch]:
        """Every firm mention in `text`, non-overlapping, longest-first.

        Offsets index into the **normalised** text, so callers that need spans
        into the original should search the original for the matched surface.
        """
        if self._pattern is None:
            return []
        normalised = normalise(text)
        out: list[FirmMatch] = []
        for match in self._pattern.finditer(normalised):
            canonical = self._index.get(match.group(0).strip().lower())
            if canonical is None:
                canonical = self._index.get(normalise(match.group(0)))
            if canonical is None:
                continue
            out.append(
                FirmMatch(
                    canonical_name=canonical,
                    matched_text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
        return out

    def resolve(self, text: str) -> str | None:
        """Canonical name for an exact firm string, or None if unknown."""
        return self._index.get(normalise(text))

    def __len__(self) -> int:
        return len(self.entries)
