"""Relevance gate: the cheap filter in front of the expensive model.

Roughly nine in ten ingested items are not movement news — court reports,
awards, CSR, webinars, deal announcements. Sending those to an LLM is money
spent to be told "no".

This gate is deliberately tuned for **recall, not precision**, which is the
opposite of the pipeline's overall bias. The asymmetry is intentional:

  * a false negative here is a move the system never sees again
  * a false positive costs one cheap model call and is then rejected by
    extraction, which is the component that is allowed to be strict

Everything the gate rejects is written to `raw_items` with `gate_passed =
false` and the terms it matched, so the false-negative rate can be measured
against the gold set instead of assumed.

`GATE_VERSION` changes whenever the rules change, so a shift in yield can be
attributed rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GATE_VERSION = "gate/1.0.0"


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


# Verbs and nouns that describe someone changing role or firm. Deliberately
# broad; trade press phrasing is endlessly inventive.
MOVEMENT = _any(
    # re- prefixes are common ("rejoins", "rehired") and the word boundary in
    # \bjoin does not match inside them.
    r"\bre-?join(s|ed|ing)?\b|\bjoin(s|ed|ing)?\b",
    r"\bre-?hire(s|d|ing)?\b|\bhire(s|d|ing)?\b",
    r"\bre-?appoint(s|ed|ment|ments|ing)?\b",
    r"\bappoint(s|ed|ment|ments|ing)?\b",
    # "names senior Dow Jones lawyer as GC" — the role often sits between
    # the verb and the "as", so match across it rather than adjacent to it.
    r"\bname[sd]?\b[^.]{0,70}?\bas\b",
    r"\bname[sd]?\s+(?:its|new|the)\b",
    r"\bwelcome(s|d)?\b",
    r"\brecruit(s|ed|ing|ment)?\b",
    r"\bpoach(es|ed|ing)?\b",
    r"\blure[sd]?\b",
    r"\bsnap(s|ped)\s+up\b",
    r"\bmove[sd]?\s+to\b",
    r"\bdepart(s|ed|ure|ures|ing)?\b",
    r"\bexit(s|ed|ing)?\b",
    r"\bquit(s|ting)?\b",
    r"\bleave[sd]?\b|\bleaving\b|\bleft\b",
    r"\bpromote[sd]?\b|\bpromotion[s]?\b|\belevate[sd]?\b",
    r"\bstep(s|ped|ping)\s+(?:down|up)\b",
    r"\bretire(s|d|ment)?\b",
    r"\blift[- ]?out\b|\bteam\s+move\b",
    r"\bswitch(es|ed)?\b",
    r"\bbolster(s|ed)?\b|\bstrengthen(s|ed|ing)?\b|\bbeef(s|ed)\s+up\b",
    r"\bgrow(s|ing)?\s+its\b|\badd(s|ed)\b",
    r"\blaunch(es|ed)\s+(?:a\s+)?(?:new\s+)?(?:office|practice|firm)\b",
    r"\bset(s|ting)?\s+up\s+(?:a\s+)?(?:new\s+)?(?:office|practice|firm)\b",
    r"\bopen(s|ed|ing)\s+(?:a\s+)?(?:new\s+)?office\b",
)

# Partner-level signal. The dataset is about partners, so an item with movement
# language but no seniority language is usually an associate hire, a business
# services appointment, or a judicial appointment.
SENIORITY = _any(
    r"\bpartner(s|ship)?\b",
    r"\bof\s+counsel\b",
    r"\bcounsel\b",
    r"\bhead\s+of\b",
    # "to head up the Gibraltar office", "will head the practice"
    r"\bhead(s|ed)?\s+up\b",
    r"\bto\s+head\b",
    r"\bco[- ]?head\b",
    r"\bpractice\s+(?:group\s+)?(?:head|leader)\b",
    r"\bmanaging\s+(?:partner|director)\b",
    r"\bsenior\s+(?:\w+\s+){0,3}?(?:counsel|lawyer|associate|litigator)\b",
    r"\bseasoned\s+(?:\w+\s+){0,2}?(?:counsel|lawyer|litigator|practitioner)\b",
    r"\bgeneral\s+counsel\b|\bGC\b",
    r"\bchief\s+legal\s+officer\b|\bCLO\b",
    r"\bdirector\b",
    r"\bprincipal\b",
    r"\bsilk\b|\bSC\b|\bKC\b|\bQC\b",
)

# Phrases strong enough on their own: a firm saying it welcomed someone as a
# partner is a movement announcement whatever else the sentence contains.
STRONG = _any(
    r"\bwelcomes?\b[^.]{0,60}\bas\s+(?:a\s+|its\s+)?(?:new\s+)?partner\b",
    r"\b(?:joins?|joined)\b[^.]{0,40}\bas\s+(?:a\s+|its\s+)?(?:new\s+)?partner\b",
    r"\bhires?\b[^.]{0,40}\bpartner\b",
    r"\bpartner\b[^.]{0,30}\bjoins?\b",
    r"\bnew\s+partner\b",
    r"\bpartner\s+(?:hire|appointment|promotion)s?\b",
    r"\blateral\s+(?:hire|move)s?\b",
    r"\bteam\s+(?:of\s+)?\w+\s+partners?\b",
    r"\b\w+[- ]partner\s+team\b",
)

# Items that look like movement news but are not. These suppress rather than
# veto: a disqualifier only sinks an item that had no strong signal.
DISQUALIFIERS = _any(
    r"\bwebinar\b|\bpodcast\b|\bseminar\b|\bconference\b|\bmasterclass\b",
    r"\bsponsors?(hip)?\b|\bcharity\b|\bfundrais\w+\b|\bdonation[s]?\b",
    r"\bpro\s*bono\s+(?:week|day|award)\b",
    r"\baward(s|ed)?\b|\branking[s]?\b|\bshortlist\w*\b|\bfinalist[s]?\b",
    r"\bbest\s+law\s+firms?\b|\bemployers?\s+of\s+choice\b",
    r"\bcourt\s+(?:rules|orders|finds|rejects|holds|dismisses)\b",
    r"\bfederal\s+court\b|\bsupreme\s+court\b|\bhigh\s+court\b|\bcourt\s+of\s+appeal\b",
    r"\bjudgment\b|\bappeal\b|\blawsuit\b|\bsues?\b|\bsued\b",
    r"\badvis(?:es|ed|ing)\s+on\b|\bacted\s+for\b|\bdeal\s+of\s+the\b",
    r"\bsurvey\b|\breport\s+finds\b|\bresearch\s+shows\b",
    r"\bgraduate[s]?\b|\btrainee[s]?\b|\bintern(ship)?s?\b",
    # The *firm* joining an organisation, not a person joining the firm.
    r"\bjoins?\s+the\b[^.]{0,70}?\b(?:association|society|network|alliance"
    r"|council|chamber|initiative|pledge|coalition)\b",
    r"\bas\s+a\s+(?:\w+\s+){0,2}?(?:corporate|platinum|gold|silver|founding)\s+"
    r"(?:member|sponsor|partner)\b",
)

# Judicial and regulatory appointments are real news but not lateral movement.
# They are excluded here and, if wanted later, belong in their own dataset.
JUDICIAL = _any(
    r"\b(?:named|appointed|announced)\b[^.]{0,50}\b(?:justice|judge|magistrate)\b",
    r"\bjudicial\s+appointment\b",
    r"\bchief\s+justice\b",
    r"\b(?:justice|judge)\s+of\s+the\b",
    r"\bsupreme\s+court\s+president\b",
)


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    score: float
    matched_terms: tuple[str, ...]
    version: str = GATE_VERSION

    @property
    def reason(self) -> str:
        if self.passed:
            return "relevant"
        if "judicial" in self.matched_terms:
            return "judicial or regulatory appointment"
        if "disqualifier" in self.matched_terms:
            return "matched a disqualifier with no movement signal"
        if "movement" not in self.matched_terms:
            return "no movement language"
        return "movement language without partner-level seniority"


def _hits(pattern: re.Pattern[str], text: str) -> list[str]:
    return [m.group(0).strip().lower() for m in pattern.finditer(text)]


def evaluate(
    headline: str,
    body: str | None = None,
    *,
    reliability_tier: int | None = None,
) -> GateDecision:
    """Decide whether an item is worth an extraction call.

    Scored on the headline plus whatever summary the outlet gave us. The
    headline is weighted higher: trade press puts the move in the title, and
    body text drags in unrelated boilerplate about the firm.

    `reliability_tier` lowers the bar for a firm's own newsroom. When a firm
    posts that it "strengthens its tax practice", that is almost always a
    partner hire written in the firm's own house style, and the seniority word
    turns up in the body rather than the headline. Tier 1 items therefore pass
    on a movement signal alone.
    """
    headline = headline or ""
    body = body or ""
    combined = f"{headline}\n{body}"

    matched: list[str] = []
    score = 0.0

    strong_hits = _hits(STRONG, combined)
    movement_head = _hits(MOVEMENT, headline)
    movement_body = _hits(MOVEMENT, body)
    seniority_hits = _hits(SENIORITY, combined)
    disqualifier_hits = _hits(DISQUALIFIERS, combined)
    judicial_hits = _hits(JUDICIAL, combined)

    if strong_hits:
        matched.append("strong")
        score += 0.6
    if movement_head:
        matched.append("movement")
        score += 0.3
    elif movement_body:
        matched.append("movement")
        score += 0.15
    if seniority_hits:
        matched.append("seniority")
        score += 0.25
    if disqualifier_hits:
        matched.append("disqualifier")
        score -= 0.3
    if judicial_hits:
        matched.append("judicial")
        score -= 0.6

    score = max(0.0, min(1.0, score))

    # A strong phrase carries the item on its own, even past a disqualifier: a
    # firm can announce a partner hire in the same post as an award.
    if strong_hits and not judicial_hits:
        return GateDecision(True, score, tuple(matched))

    if judicial_hits:
        return GateDecision(False, score, tuple(matched))

    has_movement = bool(movement_head or movement_body)
    if not has_movement:
        return GateDecision(False, score, tuple(matched))

    # The lowered tier-1 bar still has to yield to a disqualifier, or a firm
    # joining an association reads as a person joining a firm.
    if not seniority_hits and (reliability_tier != 1 or disqualifier_hits):
        return GateDecision(False, score, tuple(matched))

    # Movement plus seniority, but the item reads as something else entirely.
    if disqualifier_hits and not movement_head:
        return GateDecision(False, score, tuple(matched))

    return GateDecision(True, score, tuple(matched))


# ---------------------------------------------------------------------------
# Entity-shaped slugs
# ---------------------------------------------------------------------------
# Some outlets do not slug the headline. Asia Business Law Journal uses
# {firm}-{person}-{city}:
#
#     law.asia/squire-patton-boggs-scott-crabb-perth/
#     law.asia/dla-piper-jake-robson-singapore/
#     law.asia/norton-rose-fulbright-hires-kate-jefferson-sydney/
#
# There is no verb, so the language gate above rejects every one of them — and
# they are exactly the records this project wants. The signal here is not
# wording but shape: a known firm and a known place in the same slug, with
# room between them for a name.
#
# This is a candidate filter, not a decision. It is deliberately loose; the
# article body is fetched afterwards and extraction stays strict.

# Slug words that mean the piece is about a deal, an event or a survey rather
# than a person. Checked before the entity shape, because those slugs also
# carry a firm and a place.
_NOT_A_MOVE_SLUG = _any(
    r"\b(?:advises?|advised|acts?\s+for|acted|counsel\s+to)\b",
    r"\b(?:financing|refinancing|acquisition|merger|ipo|listing|bond|issuance"
    r"|offering|investment|fundraising|deal|transaction|joint\s+venture)\b",
    r"\b(?:seminar|webinar|conference|forum|summit|awards?|survey|report"
    r"|guide|rankings?|roundtable|briefing)\b",
    r"\b(?:billing|rates|reform|regulation|guidelines|ruling|judgment)\b",
    r"\b(?:opens?|launches?|office)\b",
)


def evaluate_entity_slug(
    slug_text: str,
    firms,
    places,
    *,
    reliability_tier: int | None = None,
) -> GateDecision:
    """Gate a slug that names entities instead of describing an event.

    `firms` and `places` are the gazetteers. Requiring both, plus at least one
    unclaimed word between them for a name, keeps deal and event coverage out
    without needing the article.
    """
    text = slug_text or ""
    matched: list[str] = []

    if _NOT_A_MOVE_SLUG.search(text):
        return GateDecision(False, 0.0, ("deal_or_event",))

    firm_hits = firms.find(text)
    place = places.find(text)
    if firm_hits:
        matched.append("firm")
    if place:
        matched.append("place")

    if not firm_hits or not place:
        return GateDecision(False, 0.2 if matched else 0.0, tuple(matched))

    # Room for a name: the firm and the place cannot account for every word.
    firm_words = sum(len(m.matched_text.split()) for m in firm_hits)
    place_words = len(place.split("-"))
    spare = len(text.split()) - firm_words - place_words
    if spare < 2:
        return GateDecision(False, 0.3, (*matched, "no_room_for_a_name"))

    matched.append("name_space")
    return GateDecision(True, 0.6, tuple(matched))
