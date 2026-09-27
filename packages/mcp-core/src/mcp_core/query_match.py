"""Whether a shop's fuzzy results name the query at all."""

from __future__ import annotations

import re
from collections.abc import Iterable


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def names_query(text: str, haystacks: Iterable[str]) -> bool:
    """Whether a haystack has a word starting with a query stem; 5 letters cover Russian endings."""
    words = re.findall(r"\w+", _fold(text))
    stems = [word[:5] for word in words if len(word) >= 3] or words
    # A letter stem must start a word («бра» is not in «абразивный»); digits may follow a model prefix.
    patterns = [re.compile(re.escape(s) if s[0].isdigit() else r"(?<![^\W\d_])" + re.escape(s)) for s in stems]
    return any(pattern.search(_fold(hay)) for hay in haystacks for pattern in patterns)
