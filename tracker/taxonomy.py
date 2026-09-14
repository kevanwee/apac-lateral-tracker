"""Taxonomy: load the versioned YAML, and map trade press phrasing onto it.

The taxonomy is a fixed artifact, not something a model invents per record.
Classification either lands on a code that exists in `practice_groups.yaml` or
lands on `unclassified` and enters review.

Deterministic first, model second. Where the text names a practice that
`taxonomy_mappings.yaml` knows, the mapping decides and no model is invoked.
That file is meant to grow from review queue resolutions, so the system gets
cheaper and more deterministic as it runs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TAXONOMY_DIR = Path(__file__).resolve().parent.parent / "taxonomy"
PRACTICE_PATH = TAXONOMY_DIR / "practice_groups.yaml"
SECTORS_PATH = TAXONOMY_DIR / "sectors.yaml"
MAPPINGS_PATH = TAXONOMY_DIR / "taxonomy_mappings.yaml"

UNCLASSIFIED = "unclassified"
MAX_SECONDARY = 2

# How much a classification is discounted by where its evidence came from.
# A stated practice clause is the article's own claim about the person. A
# headline usually describes the hire but sometimes the firm ("IP firm adds
# partner"). A body sentence is the weakest: proximity to the name is not the
# same as being about the name.
EVIDENCE_WEIGHT = {"stated": 1.0, "headline": 0.75, "body": 0.6}

_PUNCT = re.compile(r"[^\w\s&]")
_WS = re.compile(r"\s+")


class TaxonomyError(ValueError):
    pass


def normalise(text: str) -> str:
    """Comparison form. Keeps & because "M&A" and "Fraud & Investigations" need it."""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


@dataclass(frozen=True)
class Node:
    code: str
    name: str
    level: int
    parent: str | None
    is_unclassified: bool = False


@dataclass(frozen=True)
class Assignment:
    primary: str
    secondary: tuple[str, ...] = ()
    rule_key: str | None = None
    confidence: float = 0.0

    @property
    def assigned_by(self) -> str:
        """Who made this classification.

        Inferred from `rule_key` rather than stored, so the two can never
        disagree — and the database enforces the same link (migration 0005:
        `assigned_by <> 'rule' OR rule_key IS NOT NULL`).

        Every path through `Taxonomy.classify` sets a rule_key, including the
        ones that decide *not* to classify, so today this always answers
        "rule". It reported "model" on those paths for a while, which made the
        classification breakdown claim a model had run on 154 of 228 records
        when no model is called anywhere in the rule pipeline and the spend was
        $0. A deliberate abstention is still a decision the rules made.
        """
        return "rule" if self.rule_key else "model"


def _flatten(raw: list[dict], kind: str) -> list[Node]:
    nodes: list[Node] = []
    for top in raw:
        nodes.append(
            Node(
                code=top["code"],
                name=top["name"],
                level=1,
                parent=None,
                is_unclassified=bool(top.get(UNCLASSIFIED)),
            )
        )
        for child in top.get("children") or []:
            if not child["code"].startswith(f"{top['code']}."):
                raise TaxonomyError(
                    f"{kind} child {child['code']!r} is not under {top['code']!r}"
                )
            nodes.append(
                Node(code=child["code"], name=child["name"], level=2, parent=top["code"])
            )
    return nodes


@dataclass
class Taxonomy:
    version: str
    practice_groups: list[Node]
    sectors: list[Node]
    checksum: bytes
    # normalised phrase -> (primary, secondary)
    _mappings: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    _not_practice: set[str] = field(default_factory=set)
    _pattern: re.Pattern[str] | None = field(default=None, repr=False)

    @classmethod
    def load(
        cls,
        practice_path: Path = PRACTICE_PATH,
        sectors_path: Path = SECTORS_PATH,
        mappings_path: Path = MAPPINGS_PATH,
    ) -> Taxonomy:
        practice_raw = yaml.safe_load(practice_path.read_text(encoding="utf-8"))
        sectors_raw = yaml.safe_load(sectors_path.read_text(encoding="utf-8"))
        mappings_raw = yaml.safe_load(mappings_path.read_text(encoding="utf-8"))

        version = str(practice_raw["version"])
        if str(sectors_raw["version"]) != version:
            raise TaxonomyError(
                f"sectors.yaml is version {sectors_raw['version']} but "
                f"practice_groups.yaml is {version}; one release covers both axes"
            )
        if str(mappings_raw["taxonomy_version"]) != version:
            raise TaxonomyError(
                f"taxonomy_mappings.yaml targets {mappings_raw['taxonomy_version']} "
                f"but the taxonomy is {version}"
            )

        groups = _flatten(practice_raw["groups"], "practice group")
        sectors = _flatten(sectors_raw["sectors"], "sector")

        if not any(n.is_unclassified for n in groups):
            raise TaxonomyError(
                "practice_groups.yaml has no node marked `unclassified: true`. "
                "It is reserved and the exactly-one-primary-group invariant needs it."
            )

        known = {n.code for n in groups}
        mappings: dict[str, tuple[str, tuple[str, ...]]] = {}
        for entry in mappings_raw["mappings"]:
            primary = entry["primary"]
            secondary = tuple(entry.get("secondary") or ())
            unknown = {primary, *secondary} - known
            if unknown:
                raise TaxonomyError(
                    f"mapping {entry['match']!r} names unknown node(s): {sorted(unknown)}"
                )
            if len(secondary) > MAX_SECONDARY:
                raise TaxonomyError(
                    f"mapping {entry['match']!r} has {len(secondary)} secondary "
                    f"groups; at most {MAX_SECONDARY} are allowed"
                )
            if primary in secondary:
                raise TaxonomyError(
                    f"mapping {entry['match']!r} repeats its primary as a secondary"
                )
            mappings[normalise(str(entry["match"]))] = (primary, secondary)

        checksum = hashlib.sha256(
            practice_path.read_bytes()
            + sectors_path.read_bytes()
            + mappings_path.read_bytes()
        ).digest()

        return cls(
            version=version,
            practice_groups=groups,
            sectors=sectors,
            checksum=checksum,
            _mappings=mappings,
            _not_practice={
                normalise(str(p)) for p in (mappings_raw.get("not_a_practice") or [])
            },
        ).build()

    def build(self) -> Taxonomy:
        phrases = sorted(
            set(self._mappings) | self._not_practice, key=len, reverse=True
        )
        if phrases:
            joined = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in phrases)
            self._pattern = re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)
        return self

    # -- classification ----------------------------------------------------

    def classify(self, practice_text: str | None) -> Assignment:
        """Map stated practice text onto the taxonomy.

        Returns the `unclassified` sentinel rather than None when nothing
        matches, so every move always carries exactly one primary group and
        unclassified records stay in the denominator of every chart.
        """
        if not practice_text or self._pattern is None:
            # No practice was stated. That is a fact about the article, not a
            # classifier failure, so it is recorded as a rule decision.
            return Assignment(
                primary=UNCLASSIFIED, rule_key="no_practice_text", confidence=0.0
            )

        normalised = normalise(practice_text)

        # Longest phrase first: "private equity" must beat "equity".
        for match in self._pattern.finditer(normalised):
            phrase = normalise(match.group(0))
            if phrase in self._not_practice:
                # A firm-wide role is not a practice area. Saying so is a
                # decision, not a failure — hence full confidence.
                return Assignment(
                    primary=UNCLASSIFIED, rule_key=f"not_a_practice:{phrase}",
                    confidence=0.9,
                )
            mapped = self._mappings.get(phrase)
            if mapped:
                primary, secondary = mapped
                # An exact whole-string match is stronger evidence than a
                # phrase found inside a longer description.
                exact = phrase == normalised
                return Assignment(
                    primary=primary,
                    secondary=secondary,
                    rule_key=f"mapping:{phrase}",
                    confidence=0.95 if exact else 0.8,
                )

        # Practice text was stated but nothing in the taxonomy matched it.
        # Worth distinguishing from "none stated": a run of these means the
        # mapping file needs a term, which the other case never does.
        return Assignment(
            primary=UNCLASSIFIED, rule_key="no_mapping_matched", confidence=0.0
        )

    def classify_from(self, evidence: list[tuple[str, str | None]]) -> Assignment:
        """Classify from the best evidence available, in the order given.

        `evidence` is [(kind, text), ...]. The kinds, strongest first:

          stated    the practice clause the article attached to the person —
                    "as a partner in its white-collar defence practice"
          headline  the headline, which names the practice far more often than
                    it names the person: "Cooley grows capital markets with new
                    partner in Beijing"
          body      the sentence(s) in the body that mention this person

        Measured motivation: under stated-only classification 68% of records
        were unclassified, and a practice-group trend over 32% of the data is
        not a trend. The headline is stored in raw_items, so a headline-derived
        assignment is fully auditable; it is recorded at lower confidence and
        with the evidence kind in its rule key, so an analysis can choose the
        floor it trusts.

        A `not_a_practice` hit is decisive only for stated evidence: an article
        that attaches "pro bono" to the person has said what the role is. The
        same word in a headline may describe the firm's week, not the hire.
        """
        had_text = False
        for kind, text in evidence:
            if not text:
                continue
            had_text = True
            found = self.classify(text)
            key = found.rule_key or ""
            if key.startswith("not_a_practice") and kind != "stated":
                continue
            if found.primary == UNCLASSIFIED and not key.startswith("not_a_practice"):
                continue
            weight = EVIDENCE_WEIGHT[kind]
            return Assignment(
                primary=found.primary,
                secondary=found.secondary,
                rule_key=f"{kind}:{key}",
                confidence=round(found.confidence * weight, 3),
            )
        return Assignment(
            primary=UNCLASSIFIED,
            rule_key="no_evidence_matched" if had_text else "no_practice_text",
            confidence=0.0,
        )

    def matched_phrase(self, text: str) -> str | None:
        """The mapping phrase `classify` fired on, for provenance spans."""
        if not text or self._pattern is None:
            return None
        normalised = normalise(text)
        for match in self._pattern.finditer(normalised):
            phrase = normalise(match.group(0))
            if phrase in self._not_practice or phrase in self._mappings:
                return phrase
        return None

    # -- lookups -----------------------------------------------------------

    @property
    def unclassified_code(self) -> str:
        return next(n.code for n in self.practice_groups if n.is_unclassified)

    def group(self, code: str) -> Node | None:
        return next((n for n in self.practice_groups if n.code == code), None)

    def codes(self) -> set[str]:
        return {n.code for n in self.practice_groups}

    def __len__(self) -> int:
        return len(self.practice_groups)
