"""Name normalisation — the Phase 4 utility.

Dedupe blocks on `(surname_normalised, to_firm_id, announced_date ± 60 days)`.
That makes the surname key the one value in the system that decides whether two
reports of a move are ever compared at all. A key that disagrees with itself
across two spellings of one name costs a merge; a key that collapses two people
onto one string costs a wrong record. Between the two, this file prefers the
first — an under-merge leaves two honest rows, an over-merge invents a person.

## What the previous stub got wrong

It assumed given-name-first ordering and took the last token as the surname.
For roughly a third of the APAC corpus that is the wrong end of the name:
"Tan Wei Ming" and "Wei Ming Tan" are the same partner and produced the keys
`ming` and `tan`. The two rows could never meet in a blocking window.

## How ordering is decided now

By gazetteer, not by guessing. `ASIAN_SURNAMES` holds the romanised surnames
that are common enough in Singapore, Hong Kong, Malaysia, Korea, Vietnam and
mainland China to be worth the lookup. The rule is deliberately one-sided:

    first token is a known surname AND last token is not  ->  surname-first
    anything else                                         ->  surname-last

So "Tan Wei Ming" reads surname-first, "Wei Ming Tan" reads surname-last, and
both key to `tan`. When *both* ends are known surnames ("Lim Tan") the name is
genuinely ambiguous from the string alone, and it falls to the Western reading
rather than to a coin toss — the same string always produces the same key,
which is what blocking needs.

## Known, deliberate costs

- A CJK-script name (`김민준`) keys off its first character and therefore never
  blocks with its romanisation (`Min-jun Kim`). Bridging the two needs a
  romanisation table this project does not have; the cost is an under-merge.
- A surname-first name whose surname is not in the gazetteer reads Western and
  keys off the wrong token. Adding a surname is a one-line config change.
- Two-character Korean and Chinese surnames (남궁, 欧阳) key off their first
  character only. Rare, and again an under-merge.
"""

from __future__ import annotations

import re
import unicodedata

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "hon"}
POST_NOMINALS = {
    "sc", "kc", "qc", "llb", "llm", "jd", "ba", "ma", "phd", "faciarb", "ficarb",
}

# Tokens that belong to the surname that follows them. Absorbed walking
# backwards from the end, so "Jan van der Berg" keys to `van der berg` while
# "Thanh Le" keeps `le` as a surname in its own right.
SURNAME_PARTICLES = {
    "van", "von", "der", "den", "de", "del", "della", "di", "da", "das", "dos",
    "du", "la", "le", "lo", "ten", "ter", "bin", "binte", "binti", "ibn", "al",
}

# Romanised surnames common in the markets this project covers. Presence here
# only ever answers "is this token a surname", never "is this person Chinese";
# the list is a parsing aid and nothing else is inferred from a hit.
ASIAN_SURNAMES = {
    # Chinese — Mandarin, Cantonese and Hokkien romanisations side by side
    "chan", "chang", "chen", "cheng", "cheung", "chew", "chia", "chiang",
    "chin", "chiu", "cho", "choi", "chong", "chou", "chow", "chu", "chua",
    "chung", "fan", "fang", "feng", "fong", "foo", "fu", "goh", "gong", "guo",
    "han", "hao", "he", "ho", "hong", "hsu", "hu", "hua", "huang", "hui",
    "hung", "kang", "khoo", "koh", "kong", "ku", "kuo", "kwan", "kwok", "lai",
    "lam", "lau", "law", "lee", "lei", "leong", "leung", "li", "liang", "liao",
    "lim", "lin", "ling", "liu", "lo", "loh", "long", "low", "lu", "luo", "ma",
    "mak", "mao", "mok", "ng", "ngai", "ong", "ou", "pan", "pang", "peng",
    "phua", "poon", "qian", "qiu", "quek", "seah", "seow", "sha", "shen",
    "shi", "sim", "sin", "song", "soh", "su", "sun", "sung", "tai", "tam",
    "tan", "tang", "tao", "teo", "teoh", "tian", "ting", "toh", "tong", "tsai",
    "tsang", "tse", "tso", "tu", "wan", "wang", "wei", "wen", "weng", "wong",
    "woo", "wu", "xie", "xu", "xue", "yan", "yang", "yao", "yap", "yeo",
    "yeoh", "yeung", "yin", "ying", "yip", "yong", "yu", "yuan", "yuen", "zeng",
    "zhang", "zhao", "zheng", "zhong", "zhou", "zhu", "zhuang",
    # Korean
    "ahn", "bae", "baek", "byun", "cha", "chae", "choe", "chun", "gim", "ha",
    "hahm", "ham",
    "hwang", "hyun", "im", "jang", "jeon", "jeong", "jin", "jo", "joo", "jung",
    "kim", "ko", "kwon", "moon", "nam", "no", "oh", "paik", "park",
    "ryu", "seo", "seok", "shim", "shin", "son", "suh", "yi", "yoo", "yoon",
    "yun",
    # Vietnamese
    "bui", "cao", "dang", "dao", "dinh", "do", "doan", "duong",
    "hoang", "huynh", "le", "luong", "luu", "ly", "mai", "ngo", "nguyen",
    "phan", "pham", "quach", "ta", "thai", "tran", "trinh", "truong",
    "vo", "vu",
    # Japanese
    "abe", "aoki", "endo", "fujii", "fujita", "goto", "hara", "hashimoto",
    "hayashi", "ikeda", "inoue", "ishida", "ishii", "ito", "kato", "kimura",
    "kobayashi", "kondo", "maeda", "matsuda", "matsumoto", "mori", "murakami",
    "nakamura", "nakajima", "nishimura", "ogawa", "okada", "ota", "saito",
    "sakamoto", "sasaki", "sato", "shimizu", "suzuki", "takahashi", "takeuchi",
    "tanaka", "taniguchi", "watanabe", "yamada", "yamaguchi", "yamamoto",
    "yamazaki", "yoshida",
    # South Asian surnames that also appear surname-first in wire copy
    "agarwal", "agrawal", "bhat", "chandra", "das", "desai", "gupta", "iyer",
    "jain", "kapoor", "khan", "kumar", "malhotra", "mehta", "menon", "nair",
    "patel", "rao", "reddy", "shah", "sharma", "singh", "sinha", "verma",
}

_BRACKETED = re.compile(r"\(([^)]*)\)")
# Apostrophes join rather than separate: "O'Brien" and "OBrien" have to produce
# one key, so the mark is deleted before the rest of the punctuation becomes
# whitespace. Splitting on it keyed the same partner to `brien` and `obrien`.
_APOSTROPHE = re.compile(r"[’'`]")
_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
# Han (CJK Unified Ideographs, plus extension A) and Hangul syllables.
_CJK = re.compile(r"[㐀-䶿一-鿿가-힯]")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def is_cjk_script(raw: str) -> bool:
    """True when the name is written in Han or Hangul rather than romanised."""
    return bool(_CJK.search(raw or ""))


def tokens(raw: str) -> list[str]:
    """Name tokens with honorifics, post-nominals and bracketed aliases removed.

    Affixes are stripped from the end they actually occur at, and only while a
    first and last name would survive. Several post-nominals are also surnames
    in these markets — `Ma` is the one that bit: stripping it positionally left
    the surname key holding the given name instead.
    """
    without_brackets = _BRACKETED.sub(" ", raw or "")
    without_apostrophes = _APOSTROPHE.sub("", strip_accents(without_brackets))
    cleaned = _PUNCT.sub(" ", without_apostrophes).lower()
    parts = [p for p in cleaned.split() if p]

    while len(parts) > 1 and parts[0] in HONORIFICS:
        parts = parts[1:]
    while len(parts) > 2 and parts[-1] in POST_NOMINALS:
        parts = parts[:-1]
    return parts


def bracketed_alias(raw: str) -> str | None:
    """The Western given name in 'Wei Ming (Kevin) Tan', if there is one."""
    match = _BRACKETED.search(raw or "")
    if not match:
        return None
    inner = match.group(1).strip()
    return inner or None


def _split_cjk(raw: str) -> tuple[list[str], list[str]]:
    """Surname and given tokens for a Han or Hangul name.

    One character of surname, the rest given. Two-character surnames exist in
    both scripts and are not detected; that under-merges, which is the safe
    direction.
    """
    compact = "".join(_CJK.findall(raw))
    if len(compact) < 2:
        return ([compact], []) if compact else ([], [])
    return ([compact[0]], [compact[1:]])


def split(raw: str) -> tuple[list[str], list[str]]:
    """(surname tokens, given tokens).

    The ordering decision lives here and nowhere else, so the blocking key and
    the display variants can never disagree about which end is the surname.
    """
    if is_cjk_script(raw):
        return _split_cjk(raw)

    parts = tokens(raw)
    if not parts:
        return [], []
    if len(parts) == 1:
        return parts, []

    first, last = parts[0], parts[-1]
    if first in ASIAN_SURNAMES and last not in ASIAN_SURNAMES:
        return [first], parts[1:]

    # Western reading: the surname is the trailing token plus any particles in
    # front of it. At least one token is always left as a given name.
    cut = len(parts) - 1
    while cut > 1 and parts[cut - 1] in SURNAME_PARTICLES:
        cut -= 1
    return parts[cut:], parts[:cut]


def _compact(parts: list[str]) -> str:
    """Join for comparison: 'Min-jun' and 'Min Jun' and 'Minjun' all collapse."""
    return re.sub(r"[\s-]+", "", " ".join(parts))


def surname_key(raw: str) -> str:
    """Blocking key. Stable across orderings and hyphenation."""
    surname, _ = split(raw)
    return _compact(surname)


def given_key(raw: str) -> str | None:
    """Normalised given name, or None when the string carries only a surname."""
    _, given = split(raw)
    return _compact(given) or None


def given_tokens(raw: str) -> list[str]:
    """Given-name tokens, in order, for initial-versus-full comparison."""
    _, given = split(raw)
    out: list[str] = []
    for token in given:
        out.extend(p for p in token.split("-") if p)
    return out


def display(raw: str) -> str:
    """The name in Western order, title-cased. Used to build variants."""
    surname, given = split(raw)
    if not surname:
        return (raw or "").strip()
    if not given:
        return " ".join(surname).title()
    return f"{' '.join(given).title()} {' '.join(surname).title()}"


def variants(raw: str) -> list[str]:
    """Every spelling of this name we have seen or can derive from this string.

    Written to `people.name_variants`, which `_resolve_person` matches against,
    so a second article using the other ordering resolves to the same person
    rather than creating a new one.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    seen = {raw}

    surname, given = split(raw)
    if surname:
        sur = " ".join(surname).title()
        if given:
            giv = " ".join(given).title()
            seen.add(f"{giv} {sur}")          # Western order
            seen.add(f"{sur} {giv}")          # surname-first order
            # Hyphenated Korean given names are romanised both ways.
            flat = _compact(given).title()
            if flat and flat.lower() != giv.replace(" ", "").lower():
                seen.add(f"{flat} {sur}")
            elif "-" in giv:
                seen.add(f"{giv.replace('-', '')} {sur}")
        alias = bracketed_alias(raw)
        if alias:
            seen.add(f"{alias.strip()} {sur}")

    return sorted(s for s in seen if s)
