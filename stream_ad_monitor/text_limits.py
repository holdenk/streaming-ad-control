"""X-compatible weighted text length.

X does not measure a post the way :func:`len` does. Its twitter-text spec
gives every code point a weight — 1 across the Latin and general-punctuation
ranges that cover most Western text, 2 for everything else (CJK, emoji) — and
bills every URL at a flat 23 no matter how long it is, because URLs are
rewritten to t.co links. A post fits when the weighted total is at most 280.

That gap matters for announcements, which are a title plus links: a
140-character CJK title already weighs 280 on its own, so a ``len()`` check
waves through a post that X rejects outright, and the announcement is lost.

This is a close approximation of twitter-text's default configuration rather
than a port of it. The one known divergence is URL detection: only URLs with
an explicit scheme are recognised, so a bare ``example.com`` in a title counts
as 11 rather than 23. Titles rarely carry those, and the alternative — pulling
in twitter-text — is a lot of surface for one truncation call.
"""

from __future__ import annotations

import re
from typing import Iterator, Tuple

# Every URL costs this, whatever its length (t.co rewriting).
URL_WEIGHT = 23
# Code-point ranges that weigh 1; everything outside them weighs 2.
# From twitter-text's default weighted ranges.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_URL_RE = re.compile(r"https?://\S+")

_ELLIPSIS = "…"


def char_weight(char: str) -> int:
    """Return the twitter-text weight of a single character."""
    point = ord(char)
    return 1 if any(lo <= point <= hi for lo, hi in _LIGHT_RANGES) else 2


def _tokens(text: str) -> Iterator[Tuple[str, int]]:
    """Yield (chunk, weight) pairs, URLs kept whole at their flat weight."""
    position = 0
    for match in _URL_RE.finditer(text):
        for char in text[position : match.start()]:
            yield char, char_weight(char)
        yield match.group(0), URL_WEIGHT
        position = match.end()
    for char in text[position:]:
        yield char, char_weight(char)


def weighted_length(text: str) -> int:
    """Return the length of *text* as X counts it."""
    return sum(weight for _chunk, weight in _tokens(text))


def truncate_weighted(text: str, limit: int) -> str:
    """Trim *text* to a weighted length of at most *limit*.

    URLs are indivisible — a half-URL is worse than no URL — so truncation
    stops before one rather than cutting into it.
    """
    if limit <= 0 or weighted_length(text) <= limit:
        return text

    budget = limit - weighted_length(_ELLIPSIS)
    kept: list = []
    used = 0
    for chunk, weight in _tokens(text):
        if used + weight > budget:
            break
        kept.append(chunk)
        used += weight
    return "".join(kept).rstrip() + _ELLIPSIS
