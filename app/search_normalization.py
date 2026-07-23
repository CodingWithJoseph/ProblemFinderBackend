"""Deterministic normalization helpers for conversational Search.

The model may suggest filters, but these helpers keep common language and
database text matching predictable. Alias targets are deliberately bounded to
known product vocabulary and are only returned when the target is present in
the database's canonical filter options.
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
    "score",
    "signal",
    "since",
    "sort",
    "source",
    "strongest",
    "trending",
    "without",
})

# Alias -> preferred canonical labels. A target is used only when an exact
# case-insensitive match exists in the live filter options.
DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "healthcare": ("Healthcare and Social Care", "Healthcare"),
    "health care": ("Healthcare and Social Care", "Healthcare"),
    "health system": ("Healthcare and Social Care", "Healthcare"),
    "health systems": ("Healthcare and Social Care", "Healthcare"),
    "hospital": ("Healthcare and Social Care", "Healthcare"),
    "hospitals": ("Healthcare and Social Care", "Healthcare"),
    "medical": ("Healthcare and Social Care", "Healthcare"),
}


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


def resolved_domain_aliases(message: str, available: list[str]) -> list[tuple[str, str]]:
    """Resolve positive domain aliases to canonical live option values."""
    canonical = {normalize_phrase(value): value for value in available}
    normalized_message = normalize_phrase(message)
    message_tokens = normalized_message.split()
    resolved: list[tuple[str, str]] = []

    for alias, targets in DOMAIN_ALIASES.items():
        normalized_alias = normalize_phrase(alias)
        if not phrase_is_present(normalized_message, normalized_alias):
            continue

        alias_tokens = normalized_alias.split()
        try:
            start = next(
                index
                for index in range(len(message_tokens) - len(alias_tokens) + 1)
                if message_tokens[index:index + len(alias_tokens)] == alias_tokens
            )
        except StopIteration:
            continue
        prefix = set(message_tokens[max(0, start - 3):start])
        if prefix & {"exclude", "not", "remove", "without"}:
            continue

        for target in targets:
            match = canonical.get(normalize_phrase(target))
            if match is not None and all(existing != match for _, existing in resolved):
                resolved.append((alias, match))
                break

    return resolved


def query_has_content_beyond_aliases(query: str, aliases: Iterable[str]) -> bool:
    alias_tokens = {
        token
        for alias in aliases
        for token in search_tokens(alias)
    }
    return any(term not in alias_tokens for term in meaningful_search_terms(query))
