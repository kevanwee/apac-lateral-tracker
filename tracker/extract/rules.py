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

# Bumped whenever a change alters what the rules produce, so a stored record
# can be attributed to the version that wrote it (moves.extractor_version).
#   2.0.0  headline templates plus body reading
#   2.1.0  firm-boundary fix, direction cues, person-slot cleaning, headline
#          jurisdiction on the body path
#   2.1.1  a firm followed by a loss cue ("Blow for X as partners exit") is
#          the origin, never the destination; classification reads the page
#          headline rather than the ingested slug
RULES_VERSION = "rules/2.4.0"

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
    "secondee", "secondment", "trainee", "intern", "consultant",
}

# Site furniture. When an article body is read, the page's own section
# headings sit in the same text as the prose, and a heading like "Most Popular"
# or "Deal Highlights" is three capitalised words in a row — indistinguishable
# from a name to a positional template. Every entry here was observed becoming
# a person in the ABLJ backfill.
NAVIGATION_WORDS = {
    "most", "popular", "latest", "trending", "featured", "sponsored",
    "deal", "deals", "highlights", "related", "recommended", "newsletter",
    "subscribe", "subscription", "login", "register", "menu", "search",
    "share", "print", "comments", "advertisement", "advertise",
    "rankings", "ranking", "awards", "briefing", "briefings", "bulletin",
    "weekly", "daily", "monthly", "edition", "archive", "archives",
    "copyright", "privacy", "terms", "contact", "about", "home",
    # Breadcrumbs and section names that precede an ABLJ body: "Market pulse
    # News Carlton Ng ..." — the name follows the section, and the section is
    # capitalised.
    "news", "pulse", "insights", "analysis", "opinion", "feature", "features",
    "column", "magazine", "premium", "exclusive",
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

# Words that name an organisation and never appear inside a person's name.
# The person slot is filled from a sentence, and a sentence contains outlets
# and firms as well as people: three records survived every other guard with
# an outlet's masthead, a firm's suffix and a role phrase in the person slot.
#
# `law` is deliberately absent. It is the obvious organisation word and it is
# also a common Hong Kong surname, so excluding it would reject real partners
# in one of the depth markets. Each of the three records is caught by a less
# ambiguous word in the same string, so the ambiguous one is not needed.
# The same reasoning keeps out `young`, `king`, `long` and `white`.
ORGANISATION_WORDS = {
    "legal", "offices", "journal", "business", "founder", "chambers",
    "solicitors", "attorneys", "advocates", "consultancy", "consulting",
    "corporation", "incorporated", "partnership", "practice", "bureau",
    # Company suffixes. An article names its subject's clients, and two of
    # them reached the person slot from one sentence listing construction
    # companies. `construction` is already a practice word; the plural is not.
    "constructions", "builders", "holdings", "group", "industries",
    "technologies", "systems", "solutions", "ventures", "partners",
    "ltd", "limited", "pty", "inc", "corp", "plc",
}

NOT_A_PERSON = (
    FUNCTION_WORDS
    | ROLE_WORDS
    | QUANTITY_WORDS
    | DESCRIPTOR_WORDS
    | PRACTICE_WORDS
    | PUBLIC_BODY_WORDS
    | NAVIGATION_WORDS
    | ORGANISATION_WORDS
)

# A captured title must be partner-level or this is not a movement we track.
# "appoints X as advisor to the data privacy practice" is an advisory
# appointment, not a lateral partner move.
PARTNER_LEVEL_TITLE = re.compile(
    r"\b(?:partner|counsel|head|chair|chairman|chairwoman|managing|"
    r"principal|director|general\s+counsel|gc\b|clo\b|silk|kc\b|qc\b|sc\b)",
    re.IGNORECASE,
)

# Titles that contain a partner-level *word* without being partner-level.
#
# PARTNER_LEVEL_TITLE accepts bare "counsel" and bare "director", which is how
# 17 records entered the dataset for appointments the articles plainly
# describe as something else: "of counsel at its Hong Kong office", "patent
# counsel in Beijing", "its newest counsel", "special counsel", "the firm's
# first director of global workforce", "its executive director of licencing".
# None of those is a partner move, and the brief is partner-level movement.
#
# Checked before the positive test and it wins, so adding a form here removes
# it whatever else the clause says.
NON_PARTNER_TITLE = re.compile(
    r"\b(?:"
    r"of\s+counsel"
    r"|(?:special|senior|patent|international|legal|corporate|in-house)\s+counsel"
    # A bare "counsel" with nothing making it general counsel or a partner.
    r"|(?<!general\s)(?<!\w)counsel(?!\s*\()"
    # "associate partner" is partner-level in some firms; a plain associate,
    # senior or otherwise, is not.
    r"|(?:senior\s+)?associate(?!\s+partner)"
    r"|paralegal|trainee|secondee|graduate|intern"
    # "Director" alone is partner-equivalent in an incorporated legal practice,
    # so only a director *of a named function* is excluded.
    r"|(?:executive\s+)?director\s+of\s+\w+"
    r"|executive\s+director"
    r"|chief\s+\w+\s+officer"
    r")\b",
    re.IGNORECASE,
)

# The words that override the exclusion above when both appear. `partnership`
# counts: "adds a special counsel to its partnership" is an elevation to
# partner, and reading only `partner` would reject it.
_PARTNER_WORD = re.compile(r"\b(?:partners?(?:hip)?|principal)\b", re.IGNORECASE)


def sentence_states_non_partner_role(body: str, person_start: int) -> str | None:
    """The non-partner role this person's own sentence gives them, if any.

    `is_partner_level` only runs when a template captured a title. Ten records
    captured none, so nothing checked them, and appointments the article calls
    a special counsel or a senior associate were stored as partner moves.

    The test is the person's own sentence rather than the headline, and that
    distinction is the whole point. "Hicksons promotes three to senior
    associate" reads like a clean rejection, but one of the people named
    further down was separately appointed a partner -- the body says so -- and
    a headline rule would have deleted a true record. A sentence naming any
    partner-level word is left alone for the same reason: one sentence often
    announces a partner and a senior associate together.
    """
    if not body or person_start < 0:
        return None
    start = body.rfind(".", 0, person_start) + 1
    end = body.find(".", person_start)
    sentence = body[start: end if end > 0 else len(body)]
    if _PARTNER_WORD.search(sentence):
        return None
    found = NON_PARTNER_TITLE.search(sentence)
    return found.group(0) if found else None


# Verbs and counting words that appear in a slug without naming anybody.
# Separate from NOT_A_PERSON because that list guards a person *slot* in
# prose, where these forms do not occur.
_SLUG_VERBS = frozenset({
    "promotes", "joins", "expands", "hires", "makes", "reshuffles", "boosts",
    "adds", "names", "lures", "welcomes", "recruits", "grows", "launches",
    "opens", "relaunch", "appoints", "bolsters", "strengthens", "taps",
    "onboards", "returns", "rejoins", "lands", "brings", "elevates", "beefs",
    "tops", "lifts", "gains", "picks", "snaps", "nabs", "two", "three", "four",
})


# Outlets whose URLs slug the entities in the story rather than the headline.
# Asia Business Law Journal writes law.asia/{firm}-{person}-{place}; the
# Australasian Lawyer writes the whole headline, so its slug residue is
# ordinary prose -- "chief transformation officer", "massive promotions",
# "record third" -- and reading that as a contradicting name would have
# dropped five genuine Bartier Perry partners from one article. The check is
# therefore opt-in per source rather than applied to every URL.
ENTITY_SLUG_SOURCES = frozenset({
    "asia-business-law-journal-archive",
    "asia-business-law-journal",
})


# Phrases that make the mention after them a reference back to an earlier
# event rather than the one being reported. Every one is taken from a stored
# record that named the wrong partner.
#
# Neutral phrases are deliberately absent. "the arrival of", "the hiring of"
# and "the recruitment of" introduce the subject of the piece as often as they
# refer back to somebody else, and including them flagged three articles whose
# opening sentence is exactly that shape.
_BACKWARD_CUE = re.compile(
    r"\b(?:follows?|following|comes? after"
    r"|earlier this year|last year|last month"
    r"|in\s+(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)"
    r"|also\s+(?:welcomed|hired|added|recruited|appointed)"
    r"|previously|prior to|had\s+(?:joined|hired))\b",
    re.IGNORECASE,
)

# How close the cue has to sit to the name to be said to govern it. Beyond
# this the two are unrelated halves of a long sentence.
_CUE_REACH = 60


# An internal elevation. The phrasing has to name both the act and the grade:
# "promotions round" on its own is common in articles about lateral hires at
# firms that happen to have just run one.
_PROMOTION_CUE = re.compile(
    r"\b(?:"
    r"promot(?:es?|ed|ing|ion)\s+(?:of\s+)?(?:[\w&'’-]+\s+){0,8}?to\s+"
    r"(?:the\s+|its\s+|our\s+)?(?:national\s+|global\s+)?partner"
    r"|elevat(?:es?|ed|ing)\s+(?:[\w&'’-]+\s+){0,8}?to\s+"
    r"(?:the\s+|its\s+)?(?:national\s+|global\s+)?partner"
    r"|been\s+promoted\s+to\s+(?:the\s+)?partner"
    r"|promotions?\s+round"
    r"|rounds?\s+of\s+promotions?"
    r"|partnership\s+promotions?"
    r")",
    re.IGNORECASE,
)

# Arrived because two firms combined, not because one person moved. A distinct
# market signal: nobody chose anything.
_MERGER_CUE = re.compile(
    r"\b(?:absorb(?:s|ed|ing)?|merger\s+with|merges?\s+with|merging\s+with"
    r"|combination\s+with|combines?\s+with|tie-?up\s+with)\b",
    re.IGNORECASE,
)


def classify_move_type(text: str, *, origin_is_known: bool) -> str | None:
    """`promotion`, `merger_absorbed`, or None to leave the caller's default.

    A stated origin firm always wins. It is direct evidence that the person
    came from somewhere else, and it outranks any amount of promotion
    vocabulary: "Dentons adds Holding Redlich special counsel to partnership"
    names the firm he left, so it is a lateral however much it reads like an
    elevation.

    Without an origin, the language decides, and only unambiguous language.
    "Welcomes four to partnership" is left alone, because a firm welcomes
    people to its partnership whether it promoted them or hired them, and
    guessing either way invents a fact the article withheld.
    """
    if origin_is_known or not text:
        return None
    if _MERGER_CUE.search(text):
        return "merger_absorbed"
    if _PROMOTION_CUE.search(text):
        return "promotion"
    return None


def mention_is_backward_looking(body: str, person_start: int) -> str | None:
    """The cue introducing this mention as an earlier event, if there is one.

    An article about one hire routinely names other partners: the piece ends
    by noting who else arrived this year. Those sentences read exactly like
    movement sentences, because they describe movements -- just not the one
    the article is reporting, and usually not in the period the record would
    be dated to.

    The cue has to precede the name, inside the same sentence, and close
    enough to be the thing introducing it. Requiring only that a cue appear
    somewhere in the sentence flagged opening lines whose cue sat after the
    name, which would have dropped true records.
    """
    if not body or person_start <= 0:
        return None
    sentence_start = body.rfind(".", 0, person_start) + 1
    clause = body[sentence_start:person_start]
    for cue in _BACKWARD_CUE.finditer(clause):
        if len(clause) - cue.end() <= _CUE_REACH:
            return cue.group(0)
    return None


def slug_names_someone_else(
    source_slug: str, url: str, person: str, body: str, gazetteer
) -> str | None:
    """The person an outlet's URL slug names, when it is not `person`.

    Asia Business Law Journal slugs its articles {firm}-{person}-{place}, so
    the subject of the piece is in the URL. Extraction reads the body, and a
    body mentions partners who are not the subject: an existing partner giving
    a quote, an earlier hire the piece refers back to, a lift-out reported last
    December. Eighteen of 208 stored ABLJ records named somebody the article's
    own slug contradicts: the row carried a partner quoted further down the
    piece while the slug, and the article, named the person who had moved.

    Returns the slug's name so the caller can abstain and say why, or None
    when the slug agrees, names nobody, or names somebody the body never
    mentions. All three of those are reasons not to act:

    - agrees: nothing to decide.
    - names nobody: "herbert-smith-project-finance-partner-singapore" leaves
      no residual once firms, places and verbs are removed, and inventing a
      contradiction out of "project finance" would drop a true record.
    - names somebody absent from the body: the slug may be stale or wrong,
      and the body is the evidence we actually read.
    """
    if source_slug not in ENTITY_SLUG_SOURCES:
        return None
    if not url or not person or not body:
        return None

    from tracker.sources.sitemap import headline_from_slug

    slug = (headline_from_slug(url) or "").lower()
    if not slug:
        return None

    # Two-letter tokens count. Plenty of surnames in this corpus are two
    # letters, so a three-letter floor drops half of a name and leaves the
    # slug looking as though it names nobody. The exclusion sets below use
    # the same floor, so short non-name tokens are still filtered.
    slug_tokens = [t for t in re.split(r"[^a-z]+", slug) if len(t) >= 2]
    if not slug_tokens:
        return None

    person_tokens = {t for t in re.split(r"[^a-z]+", person.lower()) if len(t) >= 2}
    if not person_tokens or (person_tokens & set(slug_tokens)):
        return None

    excluded = (
        NOT_A_PERSON
        | _SLUG_VERBS
        | gazetteer.tokens()
        | PLACES.tokens()
        | person_tokens
    )
    residual = [t for t in slug_tokens if t not in excluded]
    if len(residual) < 2:
        return None

    # The decisive test: the slug's name has to be in the text we read. A
    # first and last name adjacent, as the article would write them.
    candidate = f"{residual[0]} {residual[1]}"
    if re.search(rf"(?<!\w){re.escape(residual[0])}\s+{re.escape(residual[1])}(?!\w)",
                 body, re.IGNORECASE):
        return candidate
    return None


def is_partner_level(title: str | None) -> bool:
    """Whether a captured title describes a partner-level appointment.

    An explicit non-partner form wins unless the same clause also says
    partner: "partner and head of counsel training" is a partner, "of counsel
    to join its litigation team" is not.
    """
    if not title:
        return False
    if NON_PARTNER_TITLE.search(title) and not _PARTNER_WORD.search(title):
        return False
    return bool(PARTNER_LEVEL_TITLE.search(title))

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


def _dedupe_repeated_name(candidate: str) -> str:
    """'Hiral Motta Hiral Motta' -> 'Hiral Motta'.

    A name repeated back to back is a page that printed it twice (a byline
    above a photo caption, most often) rather than a four-part name, and the
    person slot swallowed both copies.
    """
    words = candidate.split()
    half = len(words) // 2
    if len(words) >= 2 and len(words) % 2 == 0:
        first, second = words[:half], words[half:]
        if [w.lower() for w in first] == [w.lower() for w in second]:
            return " ".join(first)
    return candidate


def strip_leading_noise(candidate: str) -> str:
    """Drop site furniture and datelines that sit in front of a real name.

    "Share David Nisbet" is a share button abutting a byline; "Brisbane Helen
    Clarke" is a dateline. Both are real people with a stray token in front,
    so trimming recovers the record where rejecting would lose it. Only leading
    tokens are trimmed, and only while a plausible two-word name remains.
    """
    words = candidate.split()
    while len(words) > 2:
        head = words[0].lower().strip(".,'’-")
        if head in NAVIGATION_WORDS or PLACES.find(words[0]):
            words = words[1:]
            continue
        break
    return " ".join(words)


def _looks_like_a_person(candidate: str, gazetteer: FirmGazetteer | None = None) -> bool:
    """Conservative by design: when unsure, say no and lose the record."""
    words = [w for w in re.split(r"\s+", candidate.strip()) if w]
    if not (2 <= len(words) <= 4):
        return False

    # A name slot that *begins* with a firm is a firm plus its office or its
    # byline, not a person: "Kennedys Hong Kong", "Corrs Brisbane".
    if gazetteer and len(words) > 2:
        for end in range(len(words) - 1, 1, -1):
            if gazetteer.resolve(" ".join(words[:end])):
                return False

    lowered = {w.lower().strip(".,'’-") for w in words}
    if lowered & NOT_A_PERSON:
        return False
    if any(any(ch.isdigit() for ch in w) for w in words):
        return False
    # A name slot that resolves to a firm means the template misread the
    # sentence, not that the firm is a person.
    if gazetteer and gazetteer.resolve(candidate):
        return False
    # A place left inside a name slot means the template swallowed an
    # organisation or a dateline: "HP India", "Kennedys Hong Kong".
    #
    # This is a deliberate, measured loss of recall, not a free win. Surnames
    # that are also place names are real — "Matt Spain" is rejected here, and
    # nothing in the text distinguishes him from "HP India" without a given-name
    # gazetteer, which does not exist yet (see tracker/names.py, Phase 4).
    # Erring towards rejection is the instruction this dataset is built on: a
    # false movement record is worse than a missed one.
    return not PLACES.find(candidate)


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
            person = _dedupe_repeated_name(strip_leading_noise(_strip_honorific(hit.person)))
            if not _looks_like_a_person(person, self.gazetteer):
                continue
            # Dropping a repeated copy shortens the matched text, so the span
            # has to shrink with it or it would cite more than the value.
            person_start = hit.person_start
            person_end = person_start + len(person)
            if hit.title and not is_partner_level(hit.title):
                continue

            # The body names partners who are not the subject of the piece:
            # an existing partner quoted on the hire, an earlier arrival the
            # article refers back to. When the outlet's own URL slug names a
            # different person and the body confirms that person exists, this
            # is the wrong one and there is nothing to repair -- every other
            # field was read from the same sentence.
            contradicted = slug_names_someone_else(
                item.source_slug, item.url, person, body, self.gazetteer
            )
            if contradicted:
                log.debug(
                    "body hit %r abstained: the slug of %s names %r",
                    person, item.url, contradicted,
                )
                continue

            # The same problem without a slug to settle it: the sentence
            # introduces this person as somebody who arrived earlier, not as
            # the move being reported. The record would carry the right person
            # against the wrong firm pair and the wrong date.
            backward = mention_is_backward_looking(body, hit.person_start)
            if backward:
                log.debug(
                    "body hit %r abstained: introduced by %r, an earlier event",
                    person, backward,
                )
                continue

            # No template captured a title, so is_partner_level never ran.
            # The sentence itself may still say what the appointment was.
            if not hit.title:
                role = sentence_states_non_partner_role(body, hit.person_start)
                if role:
                    log.debug(
                        "body hit %r abstained: its sentence calls the role %r",
                        person, role,
                    )
                    continue

            # Origin, best evidence first: this person's own sentence, then
            # the firm pair the headline set up.
            from_firm = hit.from_firm or body_rules.origin_for(
                person, body, self.gazetteer
            ) or headline_origin

            # Without this, every internal elevation was recorded as a lateral,
            # because the only thing that made a promotion was the origin firm
            # happening to equal the destination -- and a promotion article
            # states no origin at all. Eleven stored records were affected,
            # against eleven promotions in the whole corpus.
            move_type = "promotion" if from_firm == to_firm else "lateral"
            stated = classify_move_type(
                f"{headline}\n{body}",
                origin_is_known=from_firm is not None and from_firm != to_firm,
            )
            if stated:
                move_type = stated
                if stated == "promotion":
                    # The schema requires both ends of a promotion to be the
                    # same firm, and a promotion article names one firm.
                    from_firm = to_firm

            fields: dict[str, VerifiedField] = {
                "person_name": VerifiedField(
                    name="person_name", value=person,
                    span_start=offset + person_start,
                    span_end=offset + person_end,
                    quality="exact",
                    excerpt=_excerpt(body, person_start, person_end),
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
                    span_start=offset + person_start,
                    span_end=offset + person_end,
                    quality="recovered",
                    excerpt=_excerpt(body, person_start, person_end),
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
                    span_start=offset + person_start,
                    span_end=offset + person_end,
                    quality="recovered",
                    excerpt=_excerpt(body, person_start, person_end),
                )
            fields["move_type"] = VerifiedField(
                name="move_type", value=move_type,
                span_start=offset + person_start,
                span_end=offset + person_end,
                quality="recovered",
                excerpt=_excerpt(body, person_start, person_end),
            )
            # The headline names the office more reliably than the body does
            # ("... in HK office"), and the template path already reads it;
            # the body path did not, which is why jurisdiction coverage sat at
            # 9% while the headlines were naming cities.
            from_headline = PLACES.find(headline)
            if from_headline:
                fields["office_jurisdiction"] = VerifiedField(
                    name="office_jurisdiction", value=from_headline,
                    span_start=0, span_end=len(headline), quality="recovered",
                    excerpt=_excerpt(headline, 0, len(headline)),
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

        person = _dedupe_repeated_name(
            strip_leading_noise(
                _strip_honorific((person or groups.get("person") or "").strip())
            )
        )
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
        if title and not is_partner_level(title):
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
