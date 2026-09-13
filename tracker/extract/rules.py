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

from tracker.extract import body_rules, confidence, roles
from tracker.extract.extractor import ExtractedMove, ExtractionResult
from tracker.extract.spans import VerifiedField, _excerpt
from tracker.firms import FirmGazetteer
from tracker.geo import PlaceGazetteer
from tracker.sources.base import RawItem

log = logging.getLogger(__name__)

RULES_VERSION = "rules/2.0.0"

# Loaded once: the place gazetteer is read-only and shared.
PLACES = PlaceGazetteer.load()

# A person slot: one to four words, no digits, no separators that would mean
# the template has over-reached across a clause boundary.
_ONE_PERSON = r"[A-Za-z][\w'’-]*(?:\s+[A-Za-z][\w'’-]*){1,3}"
PERSON = rf"(?P<person>{_ONE_PERSON})"
# Two or more people sharing one verb. An article naming two partners is two
# records, never one, so this is captured and then split.
PEOPLE = (
    rf"(?P<person>{_ONE_PERSON}(?:\s*,\s*{_ONE_PERSON})*"
    rf"(?:\s*,?\s+and\s+{_ONE_PERSON})+)"
)
# One person or a list — for templates where either is normal.
PEOPLE_OR_ONE = (
    rf"(?P<person>{_ONE_PERSON}(?:\s*,\s*{_ONE_PERSON})*"
    rf"(?:\s*,?\s+and\s+{_ONE_PERSON})*)"
)
# The role noun that precedes a trailing name.
_TITLE_WORD = r"(?:partner|principal|counsel|lawyer|head|director|silk)"
# A firm slot. "&" and "+" appear as standalone tokens ("Drew & Napier",
# "Gilbert + Tobin"), so they need their own alternative — a character class
# cannot match them, since every word must start with a letter. Lazy, so a
# template with a trailing keyword finds the first one rather than the last.
_FIRM_WORD = r"(?:[&+]|[A-Za-z][\w'’.-]*)"
FIRM_A = rf"(?P<firm_a>[A-Za-z][\w'’.-]*(?:\s+{_FIRM_WORD}){{0,5}}?)"
FIRM_B = rf"(?P<firm_b>[A-Za-z][\w'’.-]*(?:\s+{_FIRM_WORD}){{0,5}})"
# Ampersands and commas are common in a real role clause: "co-heads of Fraud,
# Asset Recovery & Investigations". Excluding them silently dropped the record.
TITLE = r"(?P<title>[\w\s'’&,.\-]{3,80}?)"
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

# Regulator, agency and jurisdiction words. Trade press writes "Ex-SafeWork NSW
# prosecutor joins Macpherson Kelley" — a whole identity with no name in it.
# "ex" leads most of these, so it is the single most useful token here.
PUBLIC_BODY_WORDS = {
    "ex", "prosecutor", "regulator", "commissioner", "ombudsman", "inspector",
    "adviser", "advisor", "judge", "magistrate", "registrar", "barrister",
    "solicitor", "watchdog", "tribunal", "commission", "authority", "agency",
    "department", "ministry", "treasury", "government", "federal", "state",
    # Australian jurisdictions, which appear constantly in AU coverage.
    "nsw", "qld", "vic", "wa", "sa", "act", "tas", "nt", "australian",
    # Regulators that recur across APAC coverage.
    "safework", "asic", "accc", "apra", "ato", "austrac", "agc", "mas",
    "sfc", "sgx", "acra", "iras", "fca", "sec", "doj", "ftc",
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
    FUNCTION_WORDS
    | ROLE_WORDS
    | QUANTITY_WORDS
    | DESCRIPTOR_WORDS
    | PRACTICE_WORDS
    | PUBLIC_BODY_WORDS
)

# A captured title must be partner-level or this is not a movement we track.
# "appoints X as advisor to the data privacy practice" is an advisory
# appointment, not a lateral partner move.
PARTNER_LEVEL_TITLE = re.compile(
    r"\b(?:partner|counsel|head|chair|chairman|chairwoman|managing|"
    r"principal|director|general\s+counsel|gc\b|clo\b|silk|kc\b|qc\b|sc\b)",
    re.IGNORECASE,
)

# Ordered most specific first; the first template that matches wins. The
# multi-person forms lead, because their single-person twins would otherwise
# match the last name in a list and silently drop the rest.
TEMPLATES: list[tuple[str, str, str]] = [
    # "Rajah & Tann Singapore names Avinash Pradhan and Vikna Rajah as
    #  co-heads of South Asia Practice" -> two records.
    ("names_people_as", "lateral",
     rf"^{FIRM_A}\s+(?:names?|appoints?|welcomes?|elevates?|promotes?|hires?|adds?)"
     rf"\s+{PEOPLE}\s+(?:as|to)\s+{TITLE}$"),
    ("people_join_as", "lateral",
     rf"^{PEOPLE}\s+(?:re)?joins?\s+{FIRM_A}(?:\s+as\s+{TITLE})?$"),
    # "Rajah & Tann Strengthens Aviation and Asset Finance Practice with New
    #  Partner Michelle Zheng" — the name trails the whole sentence. Common in
    #  firm announcements, which lead with the practice, not the person.
    ("firm_with_new_partner", "lateral",
     rf"^{FIRM_A}\s+[^.]{{0,90}}?\bwith\s+(?:its\s+|the\s+)?(?:new\s+)?"
     rf"(?:{_TITLE_WORD})s?\s+{PEOPLE_OR_ONE}$"),
    # "... expands corporate practice, hires leading lawyer Raymond Tong"
    ("firm_hires_named", "lateral",
     rf"^{FIRM_A}\s+[^.]{{0,90}}?\b(?:hires?|recruits?|welcomes?|appoints?|adds?)"
     rf"\s+(?:leading\s+|senior\s+|veteran\s+|new\s+)?"
     rf"(?:{_TITLE_WORD})s?\s+{PEOPLE_OR_ONE}$"),
    ("people_join_from", "lateral",
     rf"^{PEOPLE}\s+(?:re)?joins?\s+{FIRM_A}\s+from\s+{FIRM_B}\b"),
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
    # "Mike Aiello, Weil Corporate Chair, Plans Exit for Cravath" — the person
    # leads, an appositive names their current firm, the destination follows.
    ("named_exit_for", "lateral",
     rf"^{PERSON}\s*,[^.]{{0,80}}?\b(?:exit|exits|move|moves|jump|jumps|"
     rf"depart|departs|defect|defects)\w*\s+(?:for|to)\s+{FIRM_A}"),
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
            # One headline can name several people, and each is its own move.
            moves = [
                built
                for person in self._split_people(match.groupdict().get("person") or "")
                if (
                    built := self._build(
                        match, name, move_type, headline, item,
                        reliability_tier, person=person,
                    )
                )
                is not None
            ]
            if not moves:
                continue
            for move in moves:
                # The headline gave us a person. The body usually knows where
                # they came from, which the headline almost never says — so
                # enrich rather than returning early.
                self._enrich_from_body(move, headline, item, reliability_tier)
            result.is_movement = True
            result.moves.extend(moves)
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
        to_firm, headline_origin = body_rules.firm_pair(headline, self.gazetteer)
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

            # Origin, best evidence first: this person's own sentence, then
            # the firm pair the headline set up.
            from_firm = hit.from_firm or body_rules.origin_for(
                person, body, self.gazetteer
            ) or headline_origin
            move_type = "promotion" if from_firm == to_firm else "lateral"

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
            practice = body_rules.practice_for(person, body)
            if practice:
                at = body.find(practice)
                if at >= 0:
                    fields["practice_text"] = VerifiedField(
                        name="practice_text", value=practice,
                        span_start=offset + at, span_end=offset + at + len(practice),
                        quality="exact",
                        excerpt=_excerpt(body, at, at + len(practice)),
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

    @staticmethod
    def _split_people(blob: str) -> list[str]:
        """One record per person. "A and B" is two moves, never one."""
        parts = re.split(r"\s*,\s*|\s+and\s+", blob)
        return [p.strip() for p in parts if p.strip()]

    def _build(
        self,
        match: re.Match[str],
        rule_name: str,
        move_type: str,
        headline: str,
        item: RawItem,
        reliability_tier: int,
        person: str | None = None,
    ) -> ExtractedMove | None:
        groups = match.groupdict()

        person = _strip_honorific((person or groups.get("person") or "").strip())
        if not _looks_like_a_person(person, self.gazetteer):
            log.debug("%s: %r is not a person, abstaining", rule_name, person)
            return None

        # The captured clause carries three facts, not one: a title, a practice
        # area and often a location. Stored whole it is a bad title and two
        # missing fields.
        role = roles.parse(groups.get("title"), PLACES)
        title = role.title or ""
        if groups.get("title") and not title:
            # A clause with no recognisable title is not a partner appointment.
            return None
        if title and not PARTNER_LEVEL_TITLE.search(title):
            log.debug("%s: %r is not a partner-level title, abstaining", rule_name, title)
            return None

        # A firm slot that the gazetteer does not recognise is a guess, and a
        # guessed firm is exactly the kind of record this project must not make.
        to_firm = self._firm(groups.get("firm_a"))
        from_firm = self._firm(groups.get("firm_b"))

        # A lazy firm group can stop short — "Rajah & Tann Strengthens ..."
        # captures just "Rajah", which resolves to nothing. The gazetteer's own
        # scan of the headline finds the full name and its direction.
        if to_firm is None:
            to_firm, paired_origin = body_rules.firm_pair(headline, self.gazetteer)
            from_firm = from_firm or paired_origin
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
            self._add(fields, "title_to", title, headline, match, "title", literal=False)
        if role.practice:
            self._add(
                fields, "practice_text", role.practice, headline, match, "title",
                literal=False,
            )
        if role.jurisdiction:
            self._add(
                fields, "office_jurisdiction", role.jurisdiction, headline, match,
                "title", literal=False,
            )
        # The headline as a whole may name the office even when the role clause
        # does not: "... as a partner in hong kong".
        if "office_jurisdiction" not in fields:
            from_headline = PLACES.find(headline)
            if from_headline:
                fields["office_jurisdiction"] = VerifiedField(
                    name="office_jurisdiction", value=from_headline,
                    span_start=0, span_end=len(headline), quality="recovered",
                    excerpt=_excerpt(headline, 0, len(headline)),
                )
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

    def _enrich_from_body(
        self, move: ExtractedMove, headline: str, item: RawItem, reliability_tier: int
    ) -> None:
        """Fill in what the headline did not say, from the body and firm pair.

        Only ever adds; a value the headline stated is never overwritten. The
        origin firm is the field this matters for — headlines say where someone
        is going far more often than where they came from, so without this the
        firm-to-firm flow matrix is empty by construction.
        """
        person = move.value("person_name")

        # The body states a practice far more often than a headline does.
        if "practice_text" not in move.fields and item.body_text and person:
            found = body_rules.practice_for(person, item.body_text)
            if found:
                at = item.body_text.find(found)
                offset = len(headline) + 2
                if at >= 0:
                    move.fields["practice_text"] = VerifiedField(
                        name="practice_text", value=found,
                        span_start=offset + at, span_end=offset + at + len(found),
                        quality="exact",
                        excerpt=_excerpt(item.body_text, at, at + len(found)),
                    )

        if "from_firm" in move.fields:
            return

        to_firm = move.value("to_firm")
        origin = None

        if item.body_text and person:
            origin = body_rules.origin_for(person, item.body_text, self.gazetteer)
        if origin is None:
            _to, headline_origin = body_rules.firm_pair(headline, self.gazetteer)
            origin = headline_origin
        if origin is None or origin == to_firm:
            return

        # The span points at the evidence that named the origin, which is the
        # headline when the firm pair supplied it and the body otherwise.
        source_text = item.body_text or headline
        at = source_text.find(origin)
        offset = (len(headline) + 2) if item.body_text else 0
        if at < 0:
            at, offset = 0, 0
            source_text = headline
        move.fields["from_firm"] = VerifiedField(
            name="from_firm",
            value=origin,
            span_start=offset + at,
            span_end=offset + at + len(origin),
            quality="recovered",
            excerpt=_excerpt(source_text, at, at + len(origin)),
        )
        # Richness changed, so the score has to be recomputed rather than left
        # describing the record as it was before enrichment.
        present = {f for f in confidence.COMPLETENESS_WEIGHTS if f in move.fields}
        move.confidence = confidence.score(
            reliability_tier=reliability_tier,
            access_level=item.access_level,
            present_fields=present,
            span_qualities=[f.quality for f in move.fields.values()],
            self_reported=move.self_confidence,
            dropped_fields=len(move.dropped),
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
