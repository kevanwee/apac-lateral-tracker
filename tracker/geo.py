"""Place gazetteer: "its Sydney office" -> AU-NSW.

Trade press names offices, not jurisdiction codes. Without this every record
has a null `office_jurisdiction`, and the depth-market filter — Singapore,
Hong Kong, Australia — has nothing to filter on.

Matching is longest-first over a punctuation-normalised form, so "Hong Kong" is
never read as "Hong", and "New South Wales" beats a bare "Wales" if one is ever
added. Regions that are not jurisdictions ("Asia Pacific", "EMEA") are listed
explicitly as non-places so they are skipped rather than mapped to something
convenient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "places.yaml"

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


@dataclass
class PlaceGazetteer:
    # normalised surface -> jurisdiction code
    _index: dict[str, str] = field(default_factory=dict, repr=False)
    _skip: set[str] = field(default_factory=set, repr=False)
    _pattern: re.Pattern[str] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> PlaceGazetteer:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        index: dict[str, str] = {}
        for code, surfaces in (raw.get("places") or {}).items():
            for surface in surfaces:
                key = normalise(str(surface))
                if key:
                    index.setdefault(key, code)
        skip = {normalise(s) for s in (raw.get("not_places") or [])}
        return cls(_index=index, _skip=skip).build()

    def build(self) -> PlaceGazetteer:
        surfaces = sorted(set(self._index) | self._skip, key=len, reverse=True)
        if surfaces:
            joined = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in surfaces)
            self._pattern = re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)
        return self

    def find(self, text: str) -> str | None:
        """The first jurisdiction code named in `text`, or None.

        A non-place match ("Asia Pacific") consumes the span and yields
        nothing, which is the point: it stops a broader term inside it from
        matching instead.
        """
        if self._pattern is None or not text:
            return None
        for match in self._pattern.finditer(normalise(text)):
            key = normalise(match.group(0))
            if key in self._skip:
                continue
            code = self._index.get(key)
            if code:
                return code
        return None

    def is_region(self, text: str) -> bool:
        """True for a geographic term that is not a jurisdiction we track.

        "South Asia Practice" reads like a practice area and is a region. This
        is what tells the role parser to drop it rather than record it.
        """
        key = normalise(text)
        return bool(key) and (key in self._skip or any(s in key for s in self._skip))

    def codes(self) -> set[str]:
        return set(self._index.values())

    def __len__(self) -> int:
        return len(self._index)
