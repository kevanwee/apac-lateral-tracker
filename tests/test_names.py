"""Name normalisation.

The surname key decides what dedupe is allowed to compare, so the test that
matters most is the pairing one: two spellings of one partner must produce one
key. Every case below is a shape that occurs in the corpus or in the gold set,
not a shape that could theoretically occur.
"""

from __future__ import annotations

import pytest

from tracker import names

# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, surname, given",
    [
        # Western order, the common case.
        ("Sarah Chen", "chen", "sarah"),
        ("Michael Robertson", "robertson", "michael"),
        # Surname-first, which the stub read from the wrong end.
        ("Tan Wei Ming", "tan", "weiming"),
        ("Lim Boon Heng", "lim", "boonheng"),
        ("Nguyen Thi Hoa", "nguyen", "thihoa"),
        ("Le Van Thanh", "le", "vanthanh"),
        # ...and the same names written the other way round.
        ("Wei Ming Tan", "tan", "weiming"),
        ("Thanh Le", "le", "thanh"),
        # Korean romanisation, hyphenated and not.
        ("Min-jun Kim", "kim", "minjun"),
        ("Minjun Kim", "kim", "minjun"),
        ("Kim Min-jun", "kim", "minjun"),
        # Particles belong to the surname.
        ("Jan van der Berg", "vanderberg", "jan"),
        ("Maria de Silva", "desilva", "maria"),
    ],
)
def test_the_surname_is_taken_from_the_right_end(raw, surname, given):
    assert names.surname_key(raw) == surname
    assert names.given_key(raw) == given


@pytest.mark.parametrize(
    "one, other",
    [
        ("Tan Wei Ming", "Wei Ming Tan"),
        ("Kim Min-jun", "Min-jun Kim"),
        ("Min-jun Kim", "Minjun Kim"),
        ("Wei Ming (Kevin) Tan SC", "Kevin Tan"),
        ("Sarah O'Brien", "Sarah OBrien"),
        ("Dr Sarah Chen", "Sarah Chen"),
        ("Michael Robertson KC", "Michael Robertson"),
    ],
)
def test_two_spellings_of_one_partner_share_a_blocking_key(one, other):
    """If this fails the two rows can never meet in a blocking window."""
    assert names.surname_key(one) == names.surname_key(other)


def test_an_ambiguous_name_still_keys_deterministically():
    """Both ends are known surnames, so the string alone cannot decide.

    It falls to the Western reading. The property that matters is not which
    end wins but that the same string always produces the same key.
    """
    assert names.surname_key("Lim Tan") == names.surname_key("Lim Tan") == "tan"


# --------------------------------------------------------------------------
# Affixes that are also names
# --------------------------------------------------------------------------

def test_a_post_nominal_that_is_also_a_surname_is_not_stripped_away():
    """`Ma` is a Master of Arts and a common Hong Kong surname.

    Stripping it positionally left "Jessica Ma" keyed to `jessica`, which would
    block her against every Jessica joining the same firm.
    """
    assert names.surname_key("Jessica Ma") == "ma"
    assert names.given_key("Jessica Ma") == "jessica"


def test_a_post_nominal_is_stripped_when_a_full_name_survives_it():
    assert names.surname_key("David Ma SC") == "ma"
    assert names.given_key("David Ma SC") == "david"


def test_honorifics_come_off_the_front_and_post_nominals_off_the_back():
    assert names.tokens("Dr Wei Ming Tan SC") == ["wei", "ming", "tan"]


# --------------------------------------------------------------------------
# CJK script
# --------------------------------------------------------------------------

def test_a_hangul_name_keys_off_its_first_character():
    assert names.surname_key("김민준") == "김"
    assert names.given_key("김민준") == "민준"


def test_a_cjk_name_is_a_known_under_merge_against_its_romanisation():
    """Documented cost: bridging the two needs a romanisation table.

    Asserted rather than left implicit, so that adding one is a visible change
    in this test rather than a silent change in the merge rate.
    """
    assert names.surname_key("김민준") != names.surname_key("Min-jun Kim")


# --------------------------------------------------------------------------
# Variants — what _resolve_person matches a second article against
# --------------------------------------------------------------------------

def test_variants_carry_both_orderings():
    out = names.variants("Tan Wei Ming")
    assert "Wei Ming Tan" in out
    assert "Tan Wei Ming" in out


def test_variants_carry_the_bracketed_western_name():
    """gold-s001: 'Wei Ming (Kevin) Tan SC' is also reported as 'Kevin Tan'."""
    out = names.variants("Wei Ming (Kevin) Tan SC")
    assert "Kevin Tan" in out
    assert "Wei Ming Tan" in out


def test_variants_carry_the_unhyphenated_romanisation():
    """gold-s004: 'Min-jun Kim' is also romanised 'Minjun Kim'."""
    assert "Minjun Kim" in names.variants("Min-jun Kim")


def test_an_empty_name_produces_nothing_rather_than_a_blank_key():
    assert names.variants("") == []
    assert names.surname_key("") == ""
    assert names.given_key("") is None


# --------------------------------------------------------------------------
# Given-name comparison inputs
# --------------------------------------------------------------------------

def test_given_tokens_split_hyphenated_romanisations():
    assert names.given_tokens("Min-jun Kim") == ["min", "jun"]
    assert names.given_tokens("Tan Wei Ming") == ["wei", "ming"]


def test_display_puts_the_name_back_in_western_order():
    assert names.display("Tan Wei Ming") == "Wei Ming Tan"
    assert names.display("Sarah Chen") == "Sarah Chen"


# --------------------------------------------------------------------------
# Real names out of the stored corpus
#
# Every case below is a `people.canonical_name` that is actually in the
# database. The stub keyed all of them off the given name, so none of them
# could ever have blocked against a second report of the same move.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, surname",
    [
        ("Chen Sijia", "chen"),
        ("Chen Wenhao", "chen"),
        ("Fu Duan", "fu"),
        ("Huang Ke", "huang"),
        ("Li Bo", "li"),
        ("Li Laixiang", "li"),
        ("Lin Jiqiang", "lin"),
        ("Ng Kim Beng", "ng"),
        ("Sun Chuan", "sun"),
        ("Sun Zhiwei", "sun"),
        ("Tan Loke-Khoon", "tan"),
        ("Wang Rongxia", "wang"),
        ("Wen Ye", "wen"),
        ("Yuan Shuaiqi", "yuan"),
        ("Zhang Shihai", "zhang"),
        ("Zheng Jianhao", "zheng"),
        ("Zhou Quan", "zhou"),
        ("Leong Chuo-ming", "leong"),
    ],
)
def test_stored_surname_first_names_key_off_the_surname(raw, surname):
    assert names.surname_key(raw) == surname


@pytest.mark.parametrize(
    "raw, surname",
    [
        # Both ends are listed surnames, so these fall to the Western reading,
        # which is the correct one for each.
        ("Han Ming Ho", "ho"),
        ("Yun Seek Hahm", "hahm"),
        ("Lee Chen", "chen"),
    ],
)
def test_a_listed_surname_used_as_a_given_name_does_not_flip_the_order(raw, surname):
    """`Kim`, `Lee`, `Han` and `Yun` are Western given names as well as surnames.

    They are only read as a leading surname when the other end of the name is
    *not* also a known surname. `Yun Seek Hahm` keyed to `yun` until `hahm`
    was added to the gazetteer, which is the cheap fix for this whole class:
    a more complete surname list makes the ambiguous branch fire more often,
    and the ambiguous branch is the safe one.
    """
    assert names.surname_key(raw) == surname


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known cost. A Western name whose *given* name is a listed Asian "
        "surname and whose surname is not listed reads surname-first, so "
        "'Lee Horan' keys to `lee` rather than `horan` (same shape: 'Kim "
        "Jones', 'Han Wilson'). Separating these needs a given-name gazetteer, "
        "which this project does not have — CLAUDE.md lists it as Phase 4 "
        "work. The failure is an under-merge: the row stays honest, it just "
        "never meets its duplicate in a blocking window. Measured on the "
        "stored corpus this rule fixed 18 surname-first keys and broke 1."
    ),
)
def test_a_western_name_whose_given_name_is_an_asian_surname_is_read_backwards():
    """'Lee Horan' is a real `people.canonical_name` in the database."""
    assert names.surname_key("Lee Horan") == "horan"
