"""The golden runner's promise check: negated wording is not a promise; real promises still are."""

import pytest

from scripts.golden.checks import promises

NOT_PROMISES = [
    "It only goes ahead once 4 players say yes and book, so no spot is guaranteed.",   # live G14 draft
    "Spots are not guaranteed until the booking is paid.",
    "This isn't confirmed yet.",
    "Nothing is reserved for you until you book.",
    "Reply YES to join, NO to pass. Reply STOP to opt out.",
]
PROMISES = [
    ("The first 4 to reply YES get the spots.", "get the spots"),
    ("Guaranteed spot for you tonight!", "Guarantee"),
    ("Reply YES and you're in.", "you're in"),
    ("No waiting: your spot is saved.", "your spot is saved"),              # negation in an earlier clause
    ("It's not a big game, but you're booked.", "you're booked"),           # 'but' starts a new clause
    ("Your place is guaranteed.", "guarantee"),
]


@pytest.mark.parametrize("text", NOT_PROMISES)
def test_negated_wording_is_not_a_promise(text):
    assert promises(text) == []


@pytest.mark.parametrize("text,phrase", PROMISES)
def test_real_promises_are_still_caught(text, phrase):
    assert any(phrase.lower() in p.lower() for p in promises(text))
