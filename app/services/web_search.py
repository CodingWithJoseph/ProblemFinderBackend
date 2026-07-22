"""Gated Groq web-search adapter that returns citations, never generated prose."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from app.config import Settings
from app.web_search_contract import WebSearchRequest, WebSearchResponse, WebSearchSource


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
ALLOWED_WEB_SEARCH_MODELS = frozenset({"groq/compound-mini", "groq/compound"})
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class WebSearchFailure(RuntimeError):
    """Provider failure represented only by a fixed, safe category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class WebSearchConfig:
    model: str
    timeout_seconds: float
    api_key: str = field(repr=False)


def resolve_web_search_config(settings: Settings) -> WebSearchConfig:
    if not settings.search_web_enabled:
        raise WebSearchFailure("disabled")

    api_key = settings.groq_api_key.strip()
    if not api_key:
        raise WebSearchFailure("missing_credentials")

    model = settings.search_web_model.strip()
    if model not in ALLOWED_WEB_SEARCH_MODELS:
        raise WebSearchFailure("invalid_configuration")

    timeout_seconds = max(1.0, min(float(settings.search_web_timeout_seconds), 30.0))
    return WebSearchConfig(model=model, timeout_seconds=timeout_seconds, api_key=api_key)


def _value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    value = getattr(source, key, None)
    if value is not None:
        return value
    extra = getattr(source, "model_extra", None)
    return extra.get(key) if isinstance(extra, dict) else None


def _safe_hostname(hostname: str) -> bool:
    value = hostname.rstrip(".").casefold()
    if not value or value == "localhost" or value.endswith((".localhost", ".local")):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return "." in value
    return False


def normalize_source_url(raw_url: Any) -> str | None:
    if not isinstance(raw_url, str) or len(raw_url) > 2_000:
        return None
    if any(ord(character) < 32 for character in raw_url):
        return None
    try:
        parsed = urlsplit(raw_url.strip())
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            return None
        if parsed.username or parsed.password or not _safe_hostname(parsed.hostname):
            return None
        port = parsed.port
    except ValueError:
        return None

    hostname = parsed.hostname.rstrip(".").casefold()
    netloc = hostname if port in {None, 443} else f"{hostname}:{port}"
    query = urlencode([
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_QUERY_KEYS
    ])
    return urlunsplit(("https", netloc, parsed.path or "/", query, ""))


def _normalized_sources(response: Any, limit: int) -> list[WebSearchSource]:
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        raise WebSearchFailure("invalid_response") from None

    executed_tools = _value(message, "executed_tools")
    if not isinstance(executed_tools, list):
        raise WebSearchFailure("invalid_response")

    sources: list[WebSearchSource] = []
    seen_urls: set[str] = set()
    for tool in executed_tools[:10]:
        search_results = _value(tool, "search_results")
        results = _value(search_results, "results")
        if not isinstance(results, list):
            continue
        for item in results[:25]:
            normalized_url = normalize_source_url(_value(item, "url"))
            if not normalized_url or normalized_url in seen_urls:
                continue

            raw_title = _value(item, "title")
            raw_snippet = _value(item, "content")
            title = raw_title.strip()[:300] if isinstance(raw_title, str) else ""
            snippet = raw_snippet.strip()[:1_000] if isinstance(raw_snippet, str) else ""
            if not title:
                continue

            raw_score = _value(item, "score")
            try:
                score = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                score = None
            if score is not None and not 0 <= score <= 1:
                score = None

            seen_urls.add(normalized_url)
            sources.append(WebSearchSource(
                citation_id=f"web-{len(sources) + 1}",
                title=title,
                url=normalized_url,
                snippet=snippet,
                score=score,
            ))
            if len(sources) >= limit:
                return sources
    return sources


async def search_web_evidence(
    request: WebSearchRequest,
    settings: Settings,
    *,
    client_factory: Any = AsyncOpenAI,
) -> WebSearchResponse:
    """Run one constrained provider search after explicit user confirmation."""
    config = resolve_web_search_config(settings)
    try:
        client = client_factory(
            api_key=config.api_key,
            base_url=GROQ_BASE_URL,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=config.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Use web search once for the supplied research query. "
                            "Do not use code execution or visit-website tools."
                        ),
                    },
                    {"role": "user", "content": request.query},
                ],
                extra_body={
                    "compound_custom": {"tools": {"enabled_tools": ["web_search"]}},
                },
                stream=False,
            ),
            timeout=config.timeout_seconds + 0.5,
        )
    except (asyncio.TimeoutError, APITimeoutError):
        raise WebSearchFailure("timeout") from None
    except (APIConnectionError, ConnectionError):
        raise WebSearchFailure("connection") from None
    except WebSearchFailure:
        raise
    except Exception:
        raise WebSearchFailure("provider_error") from None

    sources = _normalized_sources(response, request.max_results)
    if not sources:
        raise WebSearchFailure("no_sources")
    return WebSearchResponse(query=request.query, sources=sources)
