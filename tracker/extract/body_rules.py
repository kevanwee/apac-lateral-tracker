"""Sentence-level templates for article bodies.

The headline templates in `rules.py` are anchored with `^`/`$` because a
headline is one short, formulaic sentence. An article body is neither, so it
needs a different shape of rule: search rather than match, scoped to one
sentence at a time, with the subject firm carried in from the headline.

That last part is what makes body extraction tractable. A body sentence reads
"Cui is a lateral hire from HNT Legal" — it never repeats the destination firm
because the headline already said it. So the destination comes from the
headline, and the sentence only has to yield a person and, if stated, an
origin.

Measured motivation: 63% of gate-passed Australasian Lawyer headlines name no
person at all. The body of one such article named five partners the headline
named none of.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tracker.firms import FirmGazetteer

# Reused shapes. Kept separate from rules.py's headline versions because a body
# sentence tolerates more punctuation than a headline does.
_PERSON = r"[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,3}"
_FIRM = r"[A-Z][\w'’&+.-]*(?:\s+(?:[&+]|[A-Za-z][\w'’.-]*)){0,5}"
_TITLE = r"[\w\s'’-]{3,60}"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# How far from a person's own mention a loose "from <Firm>" may sit and still
# be read as that person's origin, in characters.
_ORIGIN_PROXIMITY = 120

# One person plus an origin firm. These give the richest record, so they run
# first and win.
WITH_ORIGIN = [
    (
        # "adds former Dechert's partner Stephen Chan" — the origin leads, as a
        # possessive, and the person follows. Runs first because it is the most
        # specific shape and names both ends unambiguously.
        "ex_firm_partner",
        rf"(?:former|ex[- ]|previously\s+(?:of|at))\s*(?P<firm_b>{_FIRM}?)"
        rf"(?:['’]s)?\s+(?:partner|counsel|principal|lawyer)\s+"
        rf"(?P<person>{_PERSON})",
    ),
    (
        "lateral_hire_from",
        rf"(?P<person>{_PERSON})\s+is\s+a\s+lateral\s+hire"
        rf"\s+from\s+(?P<firm_b>{_FIRM})",
    ),
    (
        "joins_from",
        rf"(?P<person>{_PERSON})\s+(?:who\s+)?(?:has\s+|had\s+)?"
        rf"(?:re)?joins?(?:ed)?\s+(?:the\s+firm\s+)?from\s+(?P<firm_b>{_FIRM})",
    ),
    (
        "arrives_from",
        rf"(?P<person>{_PERSON})\s+(?:arrives|moves|transfers|comes)"
        rf"\s+from\s+(?P<firm_b>{_FIRM})",
    ),
    (
        "was_previously_at",
        rf"(?P<person>{_PERSON})\s+was\s+(?:previously\s+)?"
        rf"(?:a\s+partner\s+)?at\s+(?P<firm_b>{_FIRM})",
    ),
]

# One person, destination implied by the headline.
WITHOUT_ORIGIN = [
    (
        "joined_as",
        rf"(?P<person>{_PERSON})\s+(?:has\s+)?(?:re)?joins?(?:ed)?"
        rf"\s+(?:the\s+firm\s+)?as\s+(?P<title>{_TITLE})",
    ),
    (
        # "Dorsey has hired Carlton Ng as of counsel" — the verb list needs the
        # hiring verbs too, not only the appointing ones. Still precise: the
        # name must be followed by "as/to <title>", so a verb alone cannot
        # fire it.
        "appointed",
        rf"(?:appointed|named|welcomed|promoted|elevated|hired|recruited|added|"
        rf"onboarded|brought\s+in|signed)"
        rf"\s+(?P<person>{_PERSON})\s+(?:as|to)\s+(?P<title>{_TITLE})",
    ),
    (
        "was_promoted",
        rf"(?P<person>{_PERSON})\s+(?:has\s+been\s+|was\s+)"
        rf"(?:appointed|promoted|elevated)\s+(?:as|to)\s+(?P<title>{_TITLE})",
    ),
    (
        "will_join",
        rf"(?P<person>{_PERSON})\s+(?:will\s+|has\s+|is\s+(?:set\s+)?to\s+)?"
        rf"(?:re)?joins?(?:ed)?\s+(?:the\s+firm|{_FIRM})",
    ),
    (
        # "with the addition of Andrew Carpenter as a partner in Hong Kong"
        "addition_of",
        rf"(?:addition|arrival|appointment|hiring|recruitment)\s+of"
        rf"\s+(?P<person>{_PERSON})",
    ),
    (
        # "adds former Dechert's partner Stephen Chan to its corporate practice"
        #
        # Both tenses. A headline writes "adds"; the body it introduces writes
        # "has added", "has recruited", "has taken onboard". Only the present
        # tense was listed, so the shape that carries the name most often --
        # a body sentence -- never matched. Measured on the rejected items,
        # this was the largest single template gap.
        "adds_named_role",
        rf"(?:add(?:s|ed)|appoint(?:s|ed)|hire[sd]|recruit(?:s|ed)|"
        rf"welcome[sd]|land(?:s|ed)|onboard(?:s|ed)|taken\s+onboard|"
        rf"tak(?:es|en)\s+on|brought\s+(?:in|on\s+board)|sign(?:s|ed))\s+"
        rf"(?:former\s+|ex-)?[\w'’\- ]{{0,40}}?"
        rf"(?:partner|counsel|principal|head|lawyer|practitioner|specialist)"
        rf"\s+(?P<person>{_PERSON})\b",
    ),
    (
        # "with the addition of aviation lawyer Ethan Tan as a partner"
        #
        # `addition_of` requires the name to follow "of" directly, so any role
        # phrase in between defeated it. Kept separate rather than loosening
        # that rule, because the role word is what makes this one safe: it is
        # the evidence that the noun being introduced is a lawyer and not a
        # practice, an office or a client.
        "addition_of_role",
        rf"(?:addition|arrival|appointment|hiring|recruitment)\s+of\s+"
        rf"(?:[\w'’\-]+\s+){{0,4}}?"
        rf"(?:partner|counsel|principal|head|lawyer|practitioner|specialist)\s+"
        rf"(?P<person>{_PERSON})\b",
    ),
    (
        "led_by",
        rf"(?:is\s+|are\s+|was\s+|were\s+)?(?:led|headed|spearheaded|fronted)"
        rf"\s+by\s+(?P<person>{_PERSON})",
    ),
    (
        "brought_in",
        rf"(?:brought\s+in|brings\s+in)\s+(?:its\s+|the\s+)?(?:first\s+)?"
        rf"{_TITLE}?\s*in\s+(?P<person>{_PERSON})",
    ),
]

# A list of names sharing one verb: "Alison Cui, Kate Ralph, Raffael Maestri
# and Mario Rashid-Ring becoming the firm's newest partners". One sentence,
# four partners — the single highest-yield pattern in firm announcements.
NAME_LIST = re.compile(
    rf"(?P<names>{_PERSON}(?:\s*,\s*{_PERSON})*\s*,?\s+and\s+{_PERSON})"
    rf"\s+(?:becoming|have\s+become|were\s+made|have\s+been\s+(?:made|promoted|elevated)|"
    rf"join(?:ed)?\s+as|are\s+(?:the\s+firm['’]?s\s+)?(?:newest\s+)?)"
    rf"[^.]{{0,60}}?partners?\b"
)

# The mirror image of NAME_LIST: the verb leads and the names follow.
# "The group comprises Stephen Boyko, Mary Katherine Rawls and Michelle Iodice."
NAME_LIST_AFTER_VERB = re.compile(
    rf"(?:comprises?|includes?|features?|consists\s+of|are|being)\s+"
    rf"(?P<names>{_PERSON}(?:\s*,\s*{_PERSON})*\s*,?\s+and\s+{_PERSON})"
)

COMPILED_WITH_ORIGIN = [(n, re.compile(p)) for n, p in WITH_ORIGIN]
COMPILED_WITHOUT_ORIGIN = [(n, re.compile(p)) for n, p in WITHOUT_ORIGIN]


@dataclass(frozen=True)
class BodyHit:
    person: str
    from_firm: str | None
    title: str | None
    rule: str
    # Offsets into the body string, not the combined extraction text. The
    # caller shifts them.
    person_start: int
    person_end: int


def subject_firm(headline: str, gazetteer: FirmGazetteer) -> str | None:
    """The firm the article is about, taken from the headline.

    Body sentences rarely repeat the destination, so without this there is
    nothing to attach a person to.
    """
    matches = gazetteer.find(headline)
    if not matches:
        return None
    # Longest mention wins: "Herbert Smith Freehills" over "Herbert Smith".
    return max(matches, key=lambda m: m.length).canonical_name


def resolve_leading_firm(candidate: str, gazetteer: FirmGazetteer) -> str | None:
    """Resolve the longest firm name at the start of `candidate`.

    The firm pattern is greedy and has no way to know where a name ends, so
    "from Ashurst today" hands us "Ashurst today", which resolves to nothing.
    Trimming trailing words until something resolves recovers the firm without
    loosening the pattern — and still returns None when no prefix is a firm we
    know, so an unrecognised origin is never guessed at.
    """
    words = candidate.split()
    for end in range(len(words), 0, -1):
        prefix = " ".join(words[:end])
        resolved = gazetteer.resolve(prefix)
        if resolved:
            return resolved
        # Possessives are how a body names an origin: "former Dechert's
        # partner". Without this the correct pattern fails and a weaker one
        # wins with the wrong firm.
        stripped = re.sub(r"['’]s$", "", prefix)
        if stripped != prefix:
            resolved = gazetteer.resolve(stripped)
            if resolved:
                return resolved
    return None


def _split_names(blob: str) -> list[str]:
    parts = re.split(r"\s*,\s*|\s+and\s+", blob)
    return [p.strip() for p in parts if p.strip()]


def find(body: str, headline: str, gazetteer: FirmGazetteer) -> list[BodyHit]:
    """Every distinct person the body reports moving. Empty when unsure."""
    hits: list[BodyHit] = []
    seen: set[str] = set()

    def add(person: str, *, from_firm: str | None, title: str | None,
            rule: str, start: int, end: int) -> None:
        key = person.lower()
        if key in seen:
            return
        seen.add(key)
        hits.append(
            BodyHit(person=person, from_firm=from_firm, title=title, rule=rule,
                    person_start=start, person_end=end)
        )

    offset = 0
    for sentence in _SENTENCE_SPLIT.split(body):
        base = body.find(sentence, offset)
        if base < 0:
            base = offset
        offset = base + len(sentence)

        # A shared-verb name list first: it covers several people at once, and
        # the single-person rules would otherwise pick off only the last name.
        listed = NAME_LIST.search(sentence) or NAME_LIST_AFTER_VERB.search(sentence)
        if listed:
            blob = listed.group("names")
            for name in _split_names(blob):
                at = sentence.find(name)
                if at >= 0:
                    add(name, from_firm=None, title="partner", rule="name_list",
                        start=base + at, end=base + at + len(name))
            continue

        matched = False
        for rule, pattern in COMPILED_WITH_ORIGIN:
            for m in pattern.finditer(sentence):
                origin = resolve_leading_firm(m.group("firm_b"), gazetteer)
                if origin is None:
                    continue  # an unrecognised origin is a guess, so skip it
                span = m.span("person")
                add(m.group("person"), from_firm=origin, title=None, rule=rule,
                    start=base + span[0], end=base + span[1])
                matched = True
        if matched:
            continue

        for rule, pattern in COMPILED_WITHOUT_ORIGIN:
            for m in pattern.finditer(sentence):
                span = m.span("person")
                title = (m.groupdict().get("title") or "").strip() or None
                add(m.group("person"), from_firm=None, title=title, rule=rule,
                    start=base + span[0], end=base + span[1])

    return hits


# ---------------------------------------------------------------------------
# Directional firm pairs
# ---------------------------------------------------------------------------
# Trade press names both firms in the headline and the person in the body:
#
#   "Cravath Recruits 6 Weil Partners"                      Weil -> Cravath
#   "Baker McKenzie's Global Chair ... Jumps to Travers Smith"  BM -> Travers
#   "Ogletree Deakins adds partner trio from rival Littler"  Littler -> Ogletree
#
# Taking the longest firm mention as the destination gets the second of those
# backwards, so direction has to come from the words around each mention.

# Immediately before an origin firm. "former"/"ex" are the strongest origin
# cues in APAC trade press and were missing: "Former Norton Rose maritime
# practice head rejoins WFW" has no "from" in it at all, so both firms fell
# through to the leads-with-the-hirer fallback and the move was recorded
# backwards.
_ORIGIN_CUE = re.compile(
    r"\b(?:from|leaves?|leaving|exits?|quits?|former|formerly\s+(?:of|at)|"
    r"ex)[\s-]+(?:rival\s+|the\s+)?$"
)
# Immediately before a destination firm. `\bjoins` never matched "rejoins" —
# the boundary fails mid-word — which is precisely the verb used when someone
# returns to a firm they left.
_DEST_CUE = re.compile(
    r"\b(?:to|for|(?:re)?joins?|(?:re)?joining|jumps?\s+to|moves?\s+to|"
    r"heads?\s+to|defects?\s+to|departs?\s+for|lands?\s+at)"
    r"\s+(?:rival\s+|the\s+)?$"
)


# Cues that mark the firm *before* them as the origin. "Blow for Ashurst as
# two senior partners exit" names one firm and a loss; the leads-with-the-
# hirer fallback recorded Ashurst as the destination. Checked on the text
# after the mention, out to the end of the clause.
_ORIGIN_CUE_AFTER = re.compile(
    r"^\s+(?:as\s+)?(?:[\w'’-]+\s+){0,6}?"
    r"(?:exits?|exodus|departs?|departures?|quits?|leaves?|loses|lost|"
    r"walks?\s+out|jumps?\s+ship|defects?|haemorrhages?|hemorrhages?)\b"
)
# The mention is preceded by a loss phrase: "Blow for Ashurst".
_LOSS_BEFORE = re.compile(r"\b(?:blow\s+(?:for|to)|setback\s+for|loss\s+for)\s+$")


def firm_pair(headline: str, gazetteer: FirmGazetteer) -> tuple[str | None, str | None]:
    """(destination, origin) from a headline naming one or two firms.

    Falls back to the convention that trade press leads with the hiring firm,
    which holds for "Firm A hires ... from Firm B" and everything shaped like
    it. Returns (None, None) when no firm is recognised at all — a guessed firm
    is worse than no record.
    """
    from tracker.firms import normalise

    normalised = normalise(headline)
    matches = gazetteer.find(headline)
    if not matches:
        return None, None

    destination: str | None = None
    origin: str | None = None

    for match in matches:
        preceding = normalised[: match.start]
        following = normalised[match.end:]
        if (
            _ORIGIN_CUE.search(preceding)
            or _LOSS_BEFORE.search(preceding)
            or _ORIGIN_CUE_AFTER.search(following)
        ):
            origin = origin or match.canonical_name
        elif _DEST_CUE.search(preceding):
            destination = destination or match.canonical_name

    ordered = list(dict.fromkeys(m.canonical_name for m in matches))

    if destination is None:
        # The hiring firm leads the sentence.
        destination = next((n for n in ordered if n != origin), None)
    if origin is None and len(ordered) > 1:
        origin = next((n for n in ordered if n != destination), None)

    return destination, origin


# "joins the corporate team", "will lead the disputes practice", "a partner in
# the firm's energy group". The body states the practice far more often than
# the headline does.
_BODY_PRACTICE = re.compile(
    r"\b(?:in|to|of|joins?|joining|leads?|leading|heads?|heading|within)\s+"
    r"(?:the\s+|its\s+|their\s+|a\s+|an\s+)?(?:firm['’]?s\s+)?"
    r"(?P<practice>[\w&,'’\- ]{3,60}?)\s+"
    r"(?:practice\s+group|practice|team|group|department|division)\b",
    re.IGNORECASE,
)


def sentences_about(person: str, body: str) -> str | None:
    """The body sentences that mention this person, joined; None if none do.

    Classification evidence for one person must not be read from a sentence
    about another. Matching on the surname as well as the full name follows
    trade press style, which introduces "Carlton Ng" and then writes "Ng".
    """
    parts = person.split()
    surname = (parts[-1] if parts else person).lower()
    needle = person.lower()
    found = [
        s for s in _SENTENCE_SPLIT.split(body)
        if needle in s.lower() or re.search(rf"\b{re.escape(surname)}\b", s.lower())
    ]
    return " ".join(found) or None


def practice_for(person: str, body: str) -> str | None:
    """The practice area stated near a named person, or None.

    Scoped to a sentence mentioning the person for the same reason origin_for
    is: a practice named elsewhere in the article belongs to someone else.
    """
    from tracker.extract.roles import clean_practice

    parts = person.split()
    surname = (parts[-1] if parts else person).lower()
    needle = person.lower()
    for sentence in _SENTENCE_SPLIT.split(body):
        lowered = sentence.lower()
        if needle not in lowered and surname not in lowered:
            continue
        for match in _BODY_PRACTICE.finditer(sentence):
            cleaned = clean_practice(match.group("practice"))
            if cleaned:
                return cleaned
    return None


def origin_for(person: str, body: str, gazetteer: FirmGazetteer) -> str | None:
    """The firm a named person came from, read out of the body.

    Only accepts a sentence that mentions this person, so a firm named
    elsewhere in the article cannot be attached to the wrong individual.
    """
    parts = person.split()
    surname = parts[-1] if parts else person
    # A slug-derived headline is lowercase and the body is not, so these must
    # be compared case-insensitively or they never match at all.
    needle, surname = person.lower(), surname.lower()
    for sentence in _SENTENCE_SPLIT.split(body):
        lowered = sentence.lower()
        if needle not in lowered and surname not in lowered:
            continue
        for _rule, pattern in COMPILED_WITH_ORIGIN:
            for m in pattern.finditer(sentence):
                resolved = resolve_leading_firm(m.group("firm_b"), gazetteer)
                if resolved:
                    return resolved
        # "... joins from Firm X" with the person earlier in the sentence.
        loose = re.search(rf"\bfrom\s+(?P<firm_b>{_FIRM})", sentence)
        if loose:
            resolved = resolve_leading_firm(loose.group("firm_b"), gazetteer)
            if resolved:
                return resolved
    return None
