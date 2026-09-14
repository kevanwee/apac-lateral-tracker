"""The URL slug as a check on who the article is about.

Asia Business Law Journal slugs {firm}-{person}-{place}, so the subject of the
piece is in the URL. Body extraction reads sentences, and a body names
partners who are not the subject: one giving a quote, one hired last December,
one in a list of the firm's other offices. Eighteen of 208 stored ABLJ records
named somebody the article's own slug contradicts.

Every URL below is real, and every stored/slug pair is one that actually
happened.
"""

from __future__ import annotations

import pytest

from tracker.extract.rules import slug_names_someone_else
from tracker.firms import FirmGazetteer

GAZETTEER = FirmGazetteer.load()
ABLJ = "asia-business-law-journal-archive"


@pytest.mark.parametrize(
    "url, stored_person, body, expected",
    [
        (
            "https://law.asia/hogan-lovells-charmian-aw-singapore/",
            "Rob Palmer",
            "Hogan Lovells has bolstered its presence in Singapore with the "
            "appointment of data privacy and cybersecurity partner Charmian Aw. "
            "The firm's expansion follows a series of strategic hires, "
            "including partner Rob Palmer, previously a managing partner at "
            "Ashurst.",
            "charmian aw",
        ),
        (
            "https://law.asia/morrison-foerster-naoya-shiota-japan/",
            "Scott Jalowayski",
            "Morrison Foerster has continued the expansion of its global "
            "private equity group with the appointment of partner Naoya "
            "Shiota in Tokyo. The arrival follows a wave of global hires this "
            "year, including partners Xiaoxi Lin in Hong Kong and Scott "
            "Jalowayski in Singapore.",
            "naoya shiota",
        ),
        (
            "https://law.asia/fangda-partners-shepard-liu-china/",
            "Monica Sun",
            "Fangda Partners has hired former Milbank chief representative in "
            "China, Shepard Liu, to join its Beijing office. Fangda has been "
            "shoring up its energy team, with the addition of Monica Sun and "
            "Li Jie in December 2024.",
            "shepard liu",
        ),
    ],
)
def test_the_slug_contradicts_a_person_the_body_merely_mentions(
    url, stored_person, body, expected
):
    assert slug_names_someone_else(ABLJ, url, stored_person, body, GAZETTEER) == expected


def test_a_slug_naming_the_same_person_does_not_contradict():
    url = "https://law.asia/kennedys-hong-kong-andrew-carpenter/"
    body = ("Kennedys has bulked up its corporate and commercial capabilities "
            "with the addition of Andrew Carpenter as a partner in Hong Kong.")
    assert slug_names_someone_else(ABLJ, url, "Andrew Carpenter", body, GAZETTEER) is None


@pytest.mark.parametrize(
    "url",
    [
        # Firms, places, practices and verbs only -- no person in the slug.
        "https://law.asia/herbert-smith-project-finance-partner-singapore/",
        "https://law.asia/white-case-hong-kong-makes-debt-finance-hire/",
        "https://law.asia/dentons-hong-kong-hires-ip-specialists/",
        "https://law.asia/kwm-recruits-ma-partner-in-beijing/",
        "https://law.asia/magic-circle-experts-join-mofo-in-hong-kong/",
    ],
)
def test_a_slug_that_names_nobody_never_contradicts(url):
    """Inventing a contradiction out of "project finance" would drop a true
    record. "hong kong" reads as a name unless place words are filtered at
    the token level, which is why PlaceGazetteer.tokens() exists."""
    body = ("The firm has appointed Ben Thompson as a partner in Singapore to "
            "lead its project finance practice in Southeast Asia.")
    assert slug_names_someone_else(ABLJ, url, "Ben Thompson", body, GAZETTEER) is None


def test_a_slug_name_absent_from_the_body_is_not_trusted():
    """The body is the evidence actually read; a slug may be stale or wrong."""
    url = "https://law.asia/fangda-partners-constance-zhao-singapore/"
    body = ("Fangda Partners has hired Shepard Liu to join its Beijing office. "
            "Liu specialises in project finance.")
    assert slug_names_someone_else(ABLJ, url, "Shepard Liu", body, GAZETTEER) is None


@pytest.mark.parametrize(
    "url, person, body",
    [("", "Sarah Chen", "body"),
     ("https://law.asia/x-y-z/", "", "body"),
     ("https://law.asia/x-y-z/", "Sarah Chen", "")],
)
def test_missing_inputs_never_assert_a_contradiction(url, person, body):
    assert slug_names_someone_else(ABLJ, url, person, body, GAZETTEER) is None


def test_an_outlet_that_slugs_the_headline_is_left_alone():
    """The Australasian Lawyer slugs the whole headline, so the residue is
    ordinary prose rather than a name.

    Measured against the stored corpus, running this check over that outlet
    flagged 15 records and every one was a false alarm -- residues like
    "chief transformation officer", "massive promotions" and "record third".
    Five of the fifteen were partners named in a single genuine announcement,
    so the check is opt-in per source rather than applied to every URL.
    """
    url = ("https://www.thelawyermag.com/au/news/general/bartier-perry-brings-"
           "in-first-chief-transformation-officer-appoints-four-partners/518564")
    body = ("Bartier Perry has brought in its first chief transformation "
            "officer. The firm further strengthened its leadership with four "
            "lawyers becoming the firm's newest partners.")
    assert slug_names_someone_else(
        "australasian-lawyer-archive", url, "Alison Cui", body, GAZETTEER) is None


def test_the_place_gazetteer_exposes_word_level_tokens():
    """`find` matches whole surfaces; a slug arrives split on hyphens."""
    from tracker.extract.rules import PLACES

    tokens = PLACES.tokens()
    assert {"hong", "kong", "singapore", "sydney"} <= tokens


def test_the_firm_gazetteer_exposes_word_level_tokens():
    tokens = GAZETTEER.tokens()
    assert {"kennedys", "fangda", "hogan", "lovells"} <= tokens
