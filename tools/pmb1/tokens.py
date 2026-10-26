"""The ONE token counter every substrate, track and control shares."""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def count_tokens(text: str) -> int:
    """Whitespace-delimited words. A model-free proxy, identical for every
    substrate so budget comparisons are apples to apples."""
    return len(text.split())


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())
