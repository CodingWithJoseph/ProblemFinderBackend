"""Conversational search interpretation without query execution."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION, get_settings
from app.models import ClusterItem
from app.prompts.search_interpreter import SEARCH_INTERPRETER_INSTRUCTIONS
from app.search_normalization import (
    DOMAIN_ALIASES,
    REFINEMENT_CONTROL_WORDS,
    SEARCH_STOP_WORDS,
    mentions_any,
    normalize_phrase,
    query_has_content_beyond_aliases,
    resolved_domain_aliases,
    search_tokens,
)
from app.search_contract import (
    ClusterSearchQuery,
    SearchFilterOptions,
    SearchInterpretationModelOutput,
    SearchInterpretationResponse,
    SearchInterpretRequest,
)
from app.services.search_interpreter_provider import (
    SearchInterpreterProviderFailure,
    get_search_interpreter_provider,
)

logger = logging.getLogger(__name__)

SEARCH_OPTION_LIMIT = 100


async def _distinct_values(db: AsyncSession, column) -> list[str]:
    cleaned = func.trim(column)
    rows = (await db.execute(
        select(cleaned)
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            column.isnot(None),
            cleaned != "",
        )
        .distinct()
        .order_by(cleaned.asc())
        .limit(SEARCH_OPTION_LIMIT)
    )).all()
    return [value for (value,) in rows if value]


async def load_search_filter_options(db: AsyncSession) -> SearchFilterOptions:
    """Read bounded canonical filter values for prompting and filter editing."""
    return SearchFilterOptions(
        opportunity_domains=await _distinct_values(db, ClusterItem.opportunity_domain),
        opportunity_types=await _distinct_values(db, ClusterItem.opportunity_type),
        sources=await _distinct_values(db, ClusterItem.platform),
        communities=await _distinct_values(db, ClusterItem.community),
    )


def _fallback_draft(request: SearchInterpretRequest) -> ClusterSearchQuery:
    if request.current_draft is None:
        base = ClusterSearchQuery()
    else:
        base = request.current_draft.model_copy(deep=True)
    values = base.model_dump()
    values.update({"query": request.message[:500], "offset": 0})
    return ClusterSearchQuery.model_validate(values)


def _canonicalize_values(
    values: list[str],
    available: list[str],
    label: str,
    assumptions: list[str],
) -> list[str]:
    canonical = {value.casefold(): value for value in available}
    normalized: list[str] = []
    for value in values:
        match = canonical.get(value.casefold())
        if match is not None:
            normalized.append(match)
        else:
            assumptions.append(
                f'No exact database option matched {label} "{value}", so it was not applied as a filter.'
            )
    return normalized


def _explicit_refinement_fields(
    message: str,
    options: SearchFilterOptions,
) -> set[str]:
    """Identify fields the latest message is allowed to change.

    The provider still interprets the values, but the server applies them as a
    patch over the current draft. This prevents an unrelated sort refinement
    from silently changing the minimum-post threshold or another filter.
    """
    normalized = normalize_phrase(message)
    tokens = set(search_tokens(message))
    reset_all = bool(re.search(r"\b(clear|remove|reset)\s+(all\s+)?filters?\b", normalized))
    if reset_all:
        return {
            "query",
            "opportunity_domains",
            "opportunity_types",
            "sources",
            "communities",
            "min_posts",
            "observed_after",
            "trending_only",
            "min_signal_score",
            "sort",
        }

    fields: set[str] = set()
    if "post" in tokens or "posts" in tokens:
        fields.add("min_posts")
    if tokens & {"sort", "rank", "order", "newest", "latest", "largest", "strongest", "best", "relevance"}:
        fields.add("sort")
    if "trending" in tokens:
        fields.update({"trending_only", "sort"})
    if "signal" in tokens and (
        tokens & {"above", "at", "least", "minimum", "over", "threshold"}
        or any(token.isdigit() for token in tokens)
        or "%" in message
    ):
        fields.add("min_signal_score")
    if tokens & {"after", "before", "date", "day", "days", "month", "months", "observed", "recent", "since", "week", "weeks"}:
        fields.add("observed_after")

    domain_aliases = [alias for alias in DOMAIN_ALIASES if mentions_any(message, [alias])]
    if tokens & {"domain", "industry", "sector"} or domain_aliases or mentions_any(message, options.opportunity_domains):
        fields.add("opportunity_domains")
    if "type" in tokens or mentions_any(message, options.opportunity_types):
        fields.add("opportunity_types")
    if tokens & {"platform", "source"} or mentions_any(message, options.sources):
        fields.add("sources")
    if tokens & {"community", "forum", "subreddit"} or mentions_any(message, options.communities):
        fields.add("communities")

    ignored_tokens = set(SEARCH_STOP_WORDS) | set(REFINEMENT_CONTROL_WORDS)
    ignored_tokens.update(token for value in (
        *options.opportunity_domains,
        *options.opportunity_types,
        *options.sources,
        *options.communities,
        *domain_aliases,
    ) for token in search_tokens(value))
    remaining = [
        token
        for token in search_tokens(message)
        if not token.isdigit() and token not in ignored_tokens
    ]
    if remaining or re.search(r"\b(clear|remove)\s+(the\s+)?(keyword|query|text search)\b", normalized):
        fields.add("query")
    return fields


def _apply_refinement_patch(
    candidate: ClusterSearchQuery,
    current: ClusterSearchQuery,
    message: str,
    options: SearchFilterOptions,
) -> ClusterSearchQuery:
    values = current.model_dump()
    for field in _explicit_refinement_fields(message, options):
        values[field] = getattr(candidate, field)
    values["limit"] = current.limit
    values["offset"] = 0
    return ClusterSearchQuery.model_validate(values)


def _validated_model_output(
    output: SearchInterpretationModelOutput,
    request: SearchInterpretRequest,
    options: SearchFilterOptions,
) -> SearchInterpretationModelOutput:
    server_assumptions: list[str] = []
    draft = output.draft.model_copy(deep=True)
    draft.opportunity_domains = _canonicalize_values(
        draft.opportunity_domains,
        options.opportunity_domains,
        "opportunity domain",
        server_assumptions,
    )
    draft.opportunity_types = _canonicalize_values(
        draft.opportunity_types,
        options.opportunity_types,
        "opportunity type",
        server_assumptions,
    )
    draft.sources = _canonicalize_values(draft.sources, options.sources, "source", server_assumptions)
    draft.communities = _canonicalize_values(
        draft.communities,
        options.communities,
        "community",
        server_assumptions,
    )

    aliases = resolved_domain_aliases(request.message, options.opportunity_domains)
    for alias, canonical in aliases:
        if canonical not in draft.opportunity_domains:
            draft.opportunity_domains.append(canonical)
            server_assumptions.append(
                f'Mapped "{alias}" to database domain "{canonical}".'
            )

    if request.current_draft is not None:
        draft = _apply_refinement_patch(
            draft,
            request.current_draft,
            request.message,
            options,
        )
    elif draft.query and aliases and not query_has_content_beyond_aliases(
        draft.query,
        [alias for alias, _ in aliases],
    ):
        # A request like "show me problems in healthcare" is completely
        # represented by the canonical domain filter. Keeping the conversational
        # filler as keyword text would broaden the SQL match unnecessarily.
        draft.query = None

    draft.limit = request.current_draft.limit if request.current_draft else 20
    draft.offset = 0
    if draft.query is None and draft.sort == "relevance":
        draft.sort = "newest"
    return SearchInterpretationModelOutput(
        draft=draft,
        assumptions=[*server_assumptions, *output.assumptions][:10],
        unsupported=output.unsupported,
        clarification_question=output.clarification_question,
    )


def _confirmation_for(draft: ClusterSearchQuery) -> str:
    def display(values: list[str]) -> str:
        shown = ", ".join(values[:3])
        remaining = len(values) - 3
        return f"{shown} (+{remaining} more)" if remaining > 0 else shown

    parts: list[str] = []
    if draft.query:
        parts.append(f'text matching "{draft.query}"')
    if draft.opportunity_domains:
        parts.append(f'domains: {display(draft.opportunity_domains)}')
    if draft.opportunity_types:
        parts.append(f'types: {display(draft.opportunity_types)}')
    if draft.sources:
        parts.append(f'sources: {display(draft.sources)}')
    if draft.communities:
        parts.append(f'communities: {display(draft.communities)}')
    if draft.min_posts > 1:
        parts.append(f'at least {draft.min_posts} posts')
    if draft.observed_after:
        parts.append(f'observed after {draft.observed_after.date().isoformat()}')
    if draft.trending_only:
        parts.append("trending only")
    if draft.min_signal_score is not None:
        parts.append(f'minimum signal score {draft.min_signal_score:g}')
    parts.append(f'sorted by {draft.sort.replace("_", " ")}')
    return "I understood this as " + "; ".join(parts) + ". Confirm these filters to run the database search."


def _response(
    output: SearchInterpretationModelOutput,
    options: SearchFilterOptions,
    *,
    fallback_used: bool,
) -> SearchInterpretationResponse:
    question = output.clarification_question
    if question is not None:
        confirmation = "I need one detail before I can prepare the database search."
    elif fallback_used:
        confirmation = (
            "I could not safely translate that into structured filters, so I kept it as a keyword search. "
            "Confirm it to continue."
        )
    else:
        confirmation = _confirmation_for(output.draft)
    return SearchInterpretationResponse(
        draft=output.draft,
        confirmation=confirmation,
        assumptions=output.assumptions,
        unsupported=output.unsupported,
        clarification_question=question,
        needs_clarification=question is not None,
        needs_confirmation=question is None,
        fallback_used=fallback_used,
        available_options=options,
    )


def _fallback_response(
    request: SearchInterpretRequest,
    options: SearchFilterOptions,
) -> SearchInterpretationResponse:
    return _response(
        SearchInterpretationModelOutput(
            draft=_fallback_draft(request),
            assumptions=["The request is being treated as keyword text because filter interpretation was unavailable."],
            unsupported=[],
            clarification_question=None,
        ),
        options,
        fallback_used=True,
    )


async def interpret_search(
    request: SearchInterpretRequest,
    uid: str,
    db: AsyncSession,
) -> SearchInterpretationResponse:
    """Interpret one message into a draft; never execute the resulting search."""
    try:
        options = await load_search_filter_options(db)
    except SQLAlchemyError:
        await db.rollback()
        logger.warning("search interpretation fallback provider=none category=options_unavailable")
        return _fallback_response(request, SearchFilterOptions())

    settings = get_settings()
    prompt_payload = {
        "current_utc": datetime.now(timezone.utc).isoformat(),
        "current_draft": request.current_draft.model_dump(mode="json") if request.current_draft else None,
        "available_options": options.model_dump(mode="json"),
        "latest_user_message": request.message,
    }
    provider_name = settings.search_interpreter_provider.strip().lower()
    if provider_name not in {"local", "groq"}:
        provider_name = "invalid"

    try:
        # User identity stays inside the application; neither adapter needs it.
        _ = uid
        provider = get_search_interpreter_provider(settings)
        output = await provider.generate(
            instructions=SEARCH_INTERPRETER_INSTRUCTIONS,
            payload=prompt_payload,
        )
        output = _validated_model_output(output, request, options)
        return _response(output, options, fallback_used=False)
    except SearchInterpreterProviderFailure as exc:
        logger.warning(
            "search interpretation fallback provider=%s category=%s",
            provider_name,
            exc.category,
        )
        return _fallback_response(request, options)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "search interpretation fallback provider=%s category=timeout",
            provider_name,
        )
        return _fallback_response(request, options)
    except Exception:
        logger.warning(
            "search interpretation fallback provider=%s category=unexpected",
            provider_name,
        )
        return _fallback_response(request, options)
