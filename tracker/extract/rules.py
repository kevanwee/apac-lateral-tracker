"""Rule-based extraction — no model, no API key, no per-item cost.

## Why this exists

99% of the backfill corpus is headline-only: a sitemap gives a URL and a date,
and the headline is rebuilt from the slug. There is no article text to reason
about. Sending `seven-new-partners-join-minterellison` to a language model is
paying for judgement where there is nothing to judge — the string is formulaic
and positional.

So templates. They are free, deterministic, instant, reproducible in CI, and
they produce span provenance natively: a regex match *is* a character span, so
the verification layer that guards the LLM path applies here unchanged.

## Why it works on text that lost its capitalisation

A slug headline reads "herbert smith freehills appoints nick baker as managing
partner". Nothing marks the firm or the person. Two things recover it:

  * the firm gazetteer (config/firms.yaml) recognises the firm by name
  * the template recovers the person by *position* — whatever sits in the
    `appoints ... as` slot is the person

Capitalisation is never relied on, so the same templates work on a published
headline and on a reconstructed one.

## Precision over coverage, enforced by abstention

Every template is written to fire only on an unambiguous shape. Anything else
yields nothing at all rather than a guess. The measured consequence is a high
abstention rate, which is the correct trade for this dataset: a missed move
gets picked up by the next source that reports it, and a fabricated one does
not get picked up by anything.

Where a headline names no person — "Pinsent Masons adds IP partner duo in
Germany from Vossius" — there is no record to make, and this returns none.
That is the same answer the LLM path gives, for a fraction of a cent less.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from tracker.extract import body_rules, confidence
from tracker.extract.extractor import ExtractedMove, ExtractionResult
from tracker.extract.spans import VerifiedField, _excerpt
from tracker.firms import FirmGazetteer
from tracker.sources.base import RawItem

log = logging.getLogger(__name__)

RULES_VERSION = "rules/1.0.0"

# A person slot: one to four words, no digits, no separators that would mean
# the template has over-reached across a clause boundary.
PERSON = r"(?P<person>[A-Za-z][\w'’-]*(?:\s+[A-Za-z][\w'’-]*){1,3})"
# A firm slot. "&" and "+" appear as standalone tokens ("Drew & Napier",
# "Gilbert + Tobin"), so they need their own alternative — a character class
# cannot match them, since every word must start with a letter. Lazy, so a
# template with a trailing keyword finds the first one rather than the last.
_FIRM_WORD = r"(?:[&+]|[A-Za-z][\w'’.-]*)"
FIRM_A = rf"(?P<firm_a>[A-Za-z][\w'’.-]*(?:\s+{_FIRM_WORD}){{0,5}}?)"
FIRM_B = rf"(?P<firm_b>[A-Za-z][\w'’.-]*(?:\s+{_FIRM_WORD}){{0,5}})"
TITLE = r"(?P<title>[\w\s'’-]{3,60}?)"
PRACTICE = r"(?P<practice>[\w\s'’-]{3,40}?)"

# Words that can never be part of a person's name. Every entry here was added
# because it produced a wrong record on real headlines, not speculatively.
#
#   "Qic gc joins hsf as executive counsel"            -> person "Qic gc"
#   "Squire patton boggs welcomes funds private
#    equity partner in london"                         -> person "in london"
#   "Minterellison promotes even dozen to partner"     -> person "even dozen"
#
# All three passed the first version of this check, which only knew about
# seniority words. A headline that trips any of these is abstained on rather
# than repaired: the shape was misread, so nothing it produced is trustworthy.

# Prepositions, articles and conjunctions. A name contains none of these.
FUNCTION_WORDS = {
    "a", "an", "the", "in", "at", "on", "of", "to", "for", "from", "with",
    "and", "or", "as", "by", "into", "after", "before", "amid", "over",
    "its", "his", "her", "their", "this", "that", "new",
}

# Role words and their abbreviations.
ROLE_WORDS = {
    "partner", "partners", "counsel", "lawyer", "lawyers", "solicitor",
    "barrister", "silk", "head", "chief", "director", "associate",
    "associates", "practice", "group", "office", "leader", "managing",
    "global", "regional", "deputy", "acting", "senior", "junior", "former",
    "first", "executive", "gc", "clo", "ceo", "cfo", "coo", "cto", "gm",
    "md", "kc", "qc", "sc", "llp", "team", "dealmaker", "litigator",
    "veteran", "specialist", "expert", "trio", "duo", "pair",
}

# Quantities. "an even dozen" is not a person.
QUANTITY_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "dozen", "dozens", "handful", "several",
    "numerous", "multiple", "many", "raft", "swathe", "wave", "round",
    "twin", "double", "triple", "even",
}

# Epithets trade press uses INSTEAD of a name: "Corrs IP star", "seasoned
# investment funds star", "magic circle veteran". These read exactly like a
# name in a template slot and are the most common false positive by far.
DESCRIPTOR_WORDS = {
    "star", "expert", "experts", "ace", "guru", "heavyweight", "rainmaker",
    "boss", "pro", "hire", "hires", "recruit", "name", "talent", "figure",
    "player", "high", "flyer", "rising", "seasoned", "prominent", "leading",
    "top", "big", "magic", "circle", "silver", "four", "eminent", "noted",
    "well", "known", "respected", "trio", "quartet", "quintet",
}

# Practice and sector vocabulary. A name slot containing one of these means the
# template swallowed a practice description: "Corrs IP star", "private equity
# experts".
PRACTICE_WORDS = {
    "ip", "ma", "esg", "funds", "fund", "investment", "investments", "equity",
    "private", "corporate", "litigation", "litigator", "disputes", "dispute",
    "tax", "employment", "labour", "banking", "finance", "financial", "energy",
    "projects", "project", "infrastructure", "real", "estate", "property",
    "insurance", "shipping", "aviation", "arbitration", "regulatory",
    "compliance", "antitrust", "competition", "privacy", "data", "cyber",
    "patent", "patents", "trademark", "trade", "marks", "restructuring",
    "insolvency", "capital", "markets", "securities", "construction",
    "technology", "tech", "media", "entertainment", "healthcare", "life",
    "sciences", "pharma", "mining", "resources", "wellbeing", "esports",
}

NOT_A_PERSON = (
    FUNCTION_WORDS | ROLE_WORDS | QUANTITY_WORDS | DESCRIPTOR_WORDS | PRACTICE_WORDS
)

# A captured title must be partner-level or this is not a movement we track.
# "appoints X as advisor to the data privacy practice" is an advisory
# appointment, not a lateral partner move.
PARTNER_LEVEL_TITLE = re.compile(
    r"\b(?:partner|counsel|head|chair|chairman|chairwoman|managing|"
    r"principal|director|general\s+counsel|gc\b|clo\b|silk|kc\b|qc\b|sc\b)",
    re.IGNORECASE,
)

# Ordered most specific first; the first template that matches wins.
TEMPLATES: list[tuple[str, str, str]] = [
    # "X joins Firm A from Firm B"
    ("joins_from", "lateral",
     rf"^{PERSON}\s+(?:re)?joins?\s+{FIRM_A}\s+from\s+{FIRM_B}\b"),
    # "Firm A welcomes X as <title>"
    ("welcomes_as", "lateral",
     rf"^{FIRM_A}\s+welcomes?\s+{PERSON}\s+as\s+{TITLE}$"),
    # "Firm A welcomes new <practice> partner X"
    ("welcomes_practice_partner", "lateral",
     rf"^{FIRM_A}\s+welcomes?\s+(?:new\s+)?{PRACTICE}\s+partner\s+{PERSON}$"),
    # "Firm A appoints X as <title>"
    ("appoints_as", "lateral",
     rf"^{FIRM_A}\s+appoints?\s+{PERSON}\s+as\s+{TITLE}$"),
    # "Firm A hires X from Firm B"
    ("hires_from", "lateral",
     rf"^{FIRM_A}\s+(?:hires?|adds?|recruits?|poaches?|lures?)\s+{PERSON}"
     rf"\s+from\s+{FIRM_B}\b"),
    # "X steps up as Firm A's new <title>"  (very common in AU coverage)
    ("steps_up_as", "promotion",
     rf"^{PERSON}\s+steps?\s+up\s+as\s+{FIRM_A}\s+(?:new\s+)?{TITLE}$"),
    # "Firm A promotes X to <title>"
    ("promotes_to", "promotion",
     rf"^{FIRM_A}\s+promotes?\s+{PERSON}\s+to\s+{TITLE}$"),
    # "X joins Firm A as <title>"
    ("joins_as", "lateral",
     rf"^{PERSON}\s+(?:re)?joins?\s+{FIRM_A}\s+as\s+{TITLE}$"),
]

COMPILED = [(name, mtype, re.compile(pat, re.IGNORECASE)) for name, mtype, pat in TEMPLATES]


HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|miss|prof(?:essor)?|sir|dame)\s+", re.IGNORECASE)


def _strip_honorific(name: str) -> str:
    """'Dr Clarisse Girot' -> 'Clarisse Girot'. Titles are not part of a name."""
    return HONORIFIC.sub("", name).strip()


def _looks_like_a_person(candidate: str, gazetteer: FirmGazetteer | None = None) -> bool:
    """Conservative by design: when unsure, say no and lose the record."""
    words = [w for w in re.split(r"\s+", candidate.strip()) if w]
    if not (2 <= len(words) <= 4):
        return False

    lowered = {w.lower().strip(".,'’-") for w in words}
    if lowered & NOT_A_PERSON:
        return False
    if any(any(ch.isdigit() for ch in w) for w in words):
        return False
    # A name slot that resolves to a firm means the template misread the
    # sentence, not that the firm is a person.
    return not (gazetteer and gazetteer.resolve(candidate))


@dataclass
class RuleExtractor:
    """Same interface as Extractor. Swap one for the other freely."""

    gazetteer: FirmGazetteer
    model: str = RULES_VERSION

    def extract(self, item: RawItem, *, reliability_tier: int) -> ExtractionResult:
        text = item.extraction_text
        result = ExtractionResult(
            item=item.without_text(),
            is_movement=False,
            not_movement_reason=None,
            model=RULES_VERSION,
            # Free. This is the entire point.
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
        )

        # Templates are anchored to a headline, so match the first line only.
        # A summary is read for firms but never for the shape of the sentence.
        headline = text.split("\n", 1)[0].strip().rstrip(".")

        for name, move_type, pattern in COMPILED:
            match = pattern.match(headline)
            if match is None:
                continue
            move = self._build(match, name, move_type, headline, item, reliability_tier)
            if move is None:
                continue
            result.is_movement = True
            result.moves.append(move)
            return result

        # The headline named nobody. Most of them do not — the names are one
        # level down, in the article body, when we have it.
        body = item.body_text
        if body:
            moves = self._from_body(body, headline, item, reliability_tier)
            if moves:
                result.is_movement = True
                result.moves.extend(moves)
                return result

        result.not_movement_reason = (
            "no rule template matched the headline"
            + ("" if body else "; no article body available")
        )
        return result

    def _from_body(
        self, body: str, headline: str, item: RawItem, reliability_tier: int
    ) -> list[ExtractedMove]:
        """Records from the article body, with the destination from the headline."""
        to_firm = body_rules.subject_firm(headline, self.gazetteer)
        if to_firm is None:
            return []

        # Spans must index the combined text the verifier sees.
        offset = len(headline) + 2
        moves: list[ExtractedMove] = []

        for hit in body_rules.find(body, headline, self.gazetteer):
            person = _strip_honorific(hit.person)
            if not _looks_like_a_person(person, self.gazetteer):
                continue
            if hit.title and not PARTNER_LEVEL_TITLE.search(hit.title):
                continue

            move_type = "lateral"
            from_firm = hit.from_firm
            if from_firm == to_firm:
                move_type = "promotion"

            fields: dict[str, VerifiedField] = {
                "person_name": VerifiedField(
                    name="person_name", value=person,
                    span_start=offset + hit.person_start,
                    span_end=offset + hit.person_end,
                    quality="exact",
                    excerpt=_excerpt(body, hit.person_start, hit.person_end),
                ),
                "to_firm": VerifiedField(
                    name="to_firm", value=to_firm,
                    span_start=0, span_end=len(headline),
                    # The destination is carried from the headline rather than
                    # matched in this sentence, so it is not an exact span.
                    quality="recovered",
                    excerpt=_excerpt(headline, 0, len(headline)),
                ),
            }
            if from_firm:
                fields["from_firm"] = VerifiedField(
                    name="from_firm", value=from_firm,
                    span_start=offset + hit.person_start,
                    span_end=offset + hit.person_end,
                    quality="recovered",
                    excerpt=_excerpt(body, hit.person_start, hit.person_end),
                )
            if hit.title:
                fields["title_to"] = VerifiedField(
                    name="title_to", value=hit.title,
                    span_start=offset + hit.person_start,
                    span_end=offset + hit.person_end,
                    quality="recovered",
                    excerpt=_excerpt(body, hit.person_start, hit.person_end),
                )
            fields["move_type"] = VerifiedField(
                name="move_type", value=move_type,
                span_start=offset + hit.person_start,
                span_end=offset + hit.person_end,
                quality="recovered",
                excerpt=_excerpt(body, hit.person_start, hit.person_end),
            )

            present = {f for f in confidence.COMPLETENESS_WEIGHTS if f in fields}
            moves.append(
                ExtractedMove(
                    fields=fields,
                    dropped=[],
                    team_size=None,
                    self_confidence=0.9,
                    confidence=confidence.score(
                        reliability_tier=reliability_tier,
                        access_level=item.access_level,
                        present_fields=present,
                        span_qualities=[f.quality for f in fields.values()],
                        self_reported=0.9,
                    ),
                )
            )
        return moves

    # -- internals ---------------------------------------------------------

    def _build(
        self,
        match: re.Match[str],
        rule_name: str,
        move_type: str,
        headline: str,
        item: RawItem,
        reliability_tier: int,
    ) -> ExtractedMove | None:
        groups = match.groupdict()

        person = _strip_honorific((groups.get("person") or "").strip())
        if not _looks_like_a_person(person, self.gazetteer):
            log.debug("%s: %r is not a person, abstaining", rule_name, person)
            return None

        # A captured title that is not partner-level means this is a different
        # kind of appointment, not a move we track.
        title = (groups.get("title") or "").strip()
        if title and not PARTNER_LEVEL_TITLE.search(title):
            log.debug("%s: %r is not a partner-level title, abstaining", rule_name, title)
            return None

        # A firm slot that the gazetteer does not recognise is a guess, and a
        # guessed firm is exactly the kind of record this project must not make.
        to_firm = self._firm(groups.get("firm_a"))
        from_firm = self._firm(groups.get("firm_b"))
        if to_firm is None:
            return None
        if groups.get("firm_b") and from_firm is None:
            return None

        # A promotion is within one firm, which the schema enforces.
        if move_type == "promotion":
            from_firm = to_firm

        fields: dict[str, VerifiedField] = {}
        self._add(fields, "person_name", person, headline, match, "person")
        self._add(fields, "to_firm", to_firm, headline, match, "firm_a", literal=False)
        if from_firm:
            # A promotion has no second firm group, so its origin span is the
            # same mention as its destination.
            origin_group = "firm_a" if move_type == "promotion" else "firm_b"
            self._add(
                fields, "from_firm", from_firm, headline, match, origin_group,
                literal=False,
            )
        if title:
            self._add(fields, "title_to", title, headline, match, "title")
        if groups.get("practice"):
            self._add(
                fields, "practice_text", groups["practice"].strip(), headline, match, "practice"
            )

        span = match.span()
        fields["move_type"] = VerifiedField(
            name="move_type",
            value=move_type,
            span_start=span[0],
            span_end=span[1],
            quality="exact",
            excerpt=_excerpt(headline, span[0], span[1]),
        )

        present = {
            f for f in confidence.COMPLETENESS_WEIGHTS if f in fields
        }
        components = confidence.score(
            reliability_tier=reliability_tier,
            access_level=item.access_level,
            present_fields=present,
            span_qualities=[f.quality for f in fields.values()],
            # A template either matched or it did not. There is no model
            # opinion to discount, so this is a constant rather than a signal.
            self_reported=0.9,
            dropped_fields=0,
        )
        return ExtractedMove(
            fields=fields,
            dropped=[],
            team_size=None,
            self_confidence=0.9,
            confidence=components,
        )

    def _firm(self, raw: str | None) -> str | None:
        if not raw:
            return None
        return self.gazetteer.resolve(raw.strip())

    @staticmethod
    def _add(
        fields: dict[str, VerifiedField],
        name: str,
        value: str,
        text: str,
        match: re.Match[str],
        group: str,
        *,
        literal: bool = True,
    ) -> None:
        """Record a field with the span the template matched it at.

        `literal=False` is for a value the gazetteer canonicalised, where the
        stored value differs from the matched surface. The span still points at
        real supporting text, which is what provenance requires.
        """
        try:
            start, end = match.span(group)
        except (IndexError, KeyError):
            return
        if start < 0:
            return
        fields[name] = VerifiedField(
            name=name,
            value=value,
            span_start=start,
            span_end=end,
            quality="exact" if literal else "recovered",
            excerpt=_excerpt(text, start, end),
        )


@dataclass
class CascadingExtractor:
    """Rules first; pay for a model only on what the rules would not touch.

    The rules abstain on roughly 95% of gate-passed headlines, so this is not a
    large saving in volume — but every record it does produce is free,
    deterministic and reproducible, and the model never sees an item that has
    already been answered.

    Both halves return the same ExtractionResult, and both feed the same span
    verification and confidence functions, so a record's provenance is the same
    shape whichever produced it. `model` on the result says which one did.
    """

    rules: RuleExtractor
    fallback: object
    model: str = RULES_VERSION

    def extract(self, item: RawItem, *, reliability_tier: int) -> ExtractionResult:
        result = self.rules.extract(item, reliability_tier=reliability_tier)
        if result.moves:
            return result
        return self.fallback.extract(item, reliability_tier=reliability_tier)
