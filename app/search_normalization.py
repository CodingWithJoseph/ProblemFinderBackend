"""Deterministic normalization helpers for conversational Search.

The model may suggest filters, but these helpers keep common language and
database text matching predictable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[+#.-][a-z0-9]+)*", re.IGNORECASE)

# These words describe the Search interaction or the fact that the records are
# problems. They should not make nearly every cluster a text match.
SEARCH_STOP_WORDS = frozenset({
    "a",
    "about",
    "all",
    "an",
    "and",
    "are",
    "at",
    "best",
    "by",
    "for",
    "find",
    "finding",
    "from",
    "give",
    "in",
    "into",
    "issue",
    "issues",
    "look",
    "looking",
    "me",
    "of",
    "on",
    "opportunities",
    "opportunity",
    "or",
    "problem",
    "problems",
    "related",
    "result",
    "results",
    "search",
    "show",
    "that",
    "the",
    "them",
    "to",
    "want",
    "with",
})

REFINEMENT_CONTROL_WORDS = frozenset({
    "after",
    "archive",
    "clear",
    "exclude",
    "highest",
    "include",
    "last",
    "largest",
    "latest",
    "least",
    "minimum",
    "newest",
    "only",
    "order",
    "posts",
    "post",
    "rank",
    "relevance",
    "remove",
    "require",
    "requirement",
    "since",
    "sort",
    "source",
    "without",
})


def normalize_phrase(value: str) -> str:
    return " ".join(TOKEN_PATTERN.findall(value.casefold()))


def search_tokens(value: str) -> list[str]:
    return TOKEN_PATTERN.findall(value.casefold())


def meaningful_search_terms(value: str) -> list[str]:
    """Return stable, de-duplicated terms that carry search meaning."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in search_tokens(value):
        if len(token) < 2 or token in SEARCH_STOP_WORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def phrase_is_present(message: str, phrase: str) -> bool:
    normalized_message = f" {normalize_phrase(message)} "
    normalized_phrase = normalize_phrase(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in normalized_message


def mentions_any(message: str, values: Iterable[str]) -> bool:
    return any(phrase_is_present(message, value) for value in values if value)
