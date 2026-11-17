"""Text checks shared by the golden-case runner.

promises(): finds wording that promises a spot or booking before one exists. A match is ignored only
when a negation ("no", "not", "never", "isn't", "without"...) comes before it in the same clause, so
"no spot is guaranteed" is not a promise, while "guaranteed spot" or "you're in" still are.
Changed 2026-11-17: before, any "guarantee" counted, so the correct G14 draft
"...once 4 players say yes and book, so no spot is guaranteed" was marked as a promise.
"""

from __future__ import annotations

import re

GUARANTEE_RE = re.compile(r"guarantee|you'?re (in|booked|confirmed)|is confirmed|confirmed spot|reserved for you|"
                          r"get the spots?|your spot is (yours|saved)|it'?s yours", re.I)

_NEGATION_RE = re.compile(r"\b(no|not|never|nothing|none|isn'?t|aren'?t|won'?t|can'?t|cannot|without)\b", re.I)
_CLAUSE_BREAK_RE = re.compile(r"[.!?;:,]|\b(but|and|so|then)\b", re.I)
_LOOKBACK_WORDS = 4


def _negated(text: str, start: int) -> bool:
    """True if a negation word sits within the few words before `start`, in the same clause."""
    before = text[:start]
    breaks = list(_CLAUSE_BREAK_RE.finditer(before))
    clause = before[breaks[-1].end():] if breaks else before
    words = clause.split()[-_LOOKBACK_WORDS:]
    return bool(_NEGATION_RE.search(" ".join(words)))


def promises(text: str) -> list[str]:
    """Promise phrases in `text`, skipping negated ones such as 'no spot is guaranteed'."""
    return [m.group(0) for m in GUARANTEE_RE.finditer(text) if not _negated(text, m.start())]
