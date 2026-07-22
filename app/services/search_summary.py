"""Generate summaries grounded only in authoritative Search evidence."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION, Settings
from app.models import Cluster, ClusterItem, SearchRun, SearchSession
from app.prompts.search_summary import SEARCH_SUMMARY_INSTRUCTIONS
from app.search_summary_contract import (
    SearchSummaryEvidence,
    SearchSummaryModelOutput,
    SearchSummaryRequest,
    SearchSummaryResponse,
)
from app.services.search_interpreter_provider import (
    SearchInterpreterProviderFailure,
    inline_local_schema_refs,
    resolve_search_interpreter_provider_config,
    strict_json_schema,
)
from app.services.web_search import search_web_evidence
from app.web_search_contract import WebSearchRequest


SUMMARY_SCHEMA_NAME = "search_evidence_summary"
SUMMARY_MAX_COMPLETION_TOKENS = 1_200
SUMMARY_ITEMS_PER_CLUSTER = 3


class SearchSummaryFailure(RuntimeError):
    """Summary failure represented only by a fixed, safe category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] if cleaned else None


def _date(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


async def load_database_summary_evidence(
    cluster_ids: list[int],
    db: AsyncSession,
) -> tuple[list[dict[str, Any]], list[SearchSummaryEvidence]]:
    """Reload bounded evidence from the database; never trust client snippets."""
    rows = await db.execute(
        select(Cluster).where(
            Cluster.id.in_(cluster_ids),
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    clusters = list(rows.scalars().all())
    clusters_by_id = {cluster.id: cluster for cluster in clusters}
    if set(clusters_by_id) != set(cluster_ids):
        raise SearchSummaryFailure("clusters_not_found")

    evidence_payload: list[dict[str, Any]] = []
    references: list[SearchSummaryEvidence] = []
    for cluster_id in cluster_ids:
        cluster = clusters_by_id[cluster_id]
        cluster_citation = f"cluster-{cluster_id}"
        cluster_label = _clean(cluster.name, 300) or f"Problem cluster {cluster_id}"
        evidence_payload.append({
            "citation_id": cluster_citation,
            "source_type": "database_cluster",
            "name": cluster_label,
            "summary": _clean(cluster.summary, 1_000),
            "signal_score": cluster.signal_score,
            "first_seen": _date(cluster.first_seen),
            "last_seen": _date(cluster.last_seen_date),
        })
        references.append(SearchSummaryEvidence(
            citation_id=cluster_citation,
            source_type="database_cluster",
            label=cluster_label,
            cluster_id=cluster_id,
        ))

        item_rows = await db.execute(
            select(ClusterItem)
            .where(
                ClusterItem.cluster_id == cluster_id,
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(ClusterItem.score.desc().nullslast())
            .limit(SUMMARY_ITEMS_PER_CLUSTER)
        )
        for index, item in enumerate(item_rows.scalars().all(), start=1):
            item_citation = f"item-{cluster_id}-{index}"
            item_label = _clean(item.title, 300) or f"Evidence item {index}"
            evidence_payload.append({
                "citation_id": item_citation,
                "source_type": "database_item",
                "cluster_citation_id": cluster_citation,
                "title": item_label,
                "problem_statement": _clean(item.problem_statement, 1_000),
                "solution_angle": _clean(item.solution_angle, 1_000),
                "platform": _clean(item.platform, 100),
                "community": _clean(item.community, 100),
                "observed_at": _date(item.posted_at or item.scraped_at),
            })
            references.append(SearchSummaryEvidence(
                citation_id=item_citation,
                source_type="database_item",
                label=item_label,
                cluster_id=cluster_id,
            ))

    return evidence_payload, references


async def validate_confirmed_search_run(
    request: SearchSummaryRequest,
    uid: str,
    db: AsyncSession,
) -> None:
    run = await db.scalar(
        select(SearchRun)
        .join(SearchSession, SearchSession.id == SearchRun.session_id)
        .where(
            SearchRun.id == str(request.search_run_id),
            SearchSession.user_id == uid,
        )
    )
    if run is None:
        raise SearchSummaryFailure("search_run_not_found")

    try:
        confirmed_cluster_ids = {int(value) for value in run.result_cluster_ids}
    except (TypeError, ValueError):
        raise SearchSummaryFailure("invalid_search_run") from None
    if not set(request.cluster_ids).issubset(confirmed_cluster_ids):
        raise SearchSummaryFailure("invalid_cluster_selection")


async def _generate_summary_output(
    evidence_payload: list[dict[str, Any]],
    settings: Settings,
    *,
    client_factory: Any = AsyncOpenAI,
) -> tuple[SearchSummaryModelOutput, str]:
    try:
        config = resolve_search_interpreter_provider_config(settings)
        client = client_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except SearchInterpreterProviderFailure as exc:
        raise SearchSummaryFailure(exc.category) from None
    except Exception:
        raise SearchSummaryFailure("invalid_configuration") from None

    schema = strict_json_schema(SearchSummaryModelOutput)
    if config.name == "local":
        schema = inline_local_schema_refs(schema)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": SUMMARY_SCHEMA_NAME,
            "strict": True,
            "schema": schema,
        },
    }
    completion_limit = (
        {"max_tokens": SUMMARY_MAX_COMPLETION_TOKENS}
        if config.name == "local"
        else {"max_completion_tokens": SUMMARY_MAX_COMPLETION_TOKENS}
    )

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": SEARCH_SUMMARY_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": json.dumps({"evidence": evidence_payload}, ensure_ascii=False),
                    },
                ],
                response_format=response_format,
                stream=False,
                **completion_limit,
            ),
            timeout=config.timeout_seconds + 0.5,
        )
    except (asyncio.TimeoutError, APITimeoutError):
        raise SearchSummaryFailure("timeout") from None
    except (APIConnectionError, ConnectionError):
        raise SearchSummaryFailure("connection") from None
    except Exception:
        raise SearchSummaryFailure("provider_error") from None

    try:
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise SearchSummaryFailure("refusal")
        content = message.content
        if not isinstance(content, str) or not content.strip():
            raise SearchSummaryFailure("empty_response")
        parsed = json.loads(content)
        output = SearchSummaryModelOutput.model_validate(parsed)
    except SearchSummaryFailure:
        raise
    except json.JSONDecodeError:
        raise SearchSummaryFailure("invalid_json") from None
    except ValidationError:
        raise SearchSummaryFailure("schema_invalid") from None
    except (AttributeError, IndexError, TypeError):
        raise SearchSummaryFailure("invalid_response") from None
    return output, config.name


def _validate_grounding(
    output: SearchSummaryModelOutput,
    references: list[SearchSummaryEvidence],
) -> None:
    available_ids = {reference.citation_id for reference in references}
    database_ids = {
        reference.citation_id
        for reference in references
        if reference.source_type in {"database_cluster", "database_item"}
    }
    used_ids = {
        citation_id
        for finding in output.findings
        for citation_id in finding.citation_ids
    }
    used_ids.update(output.overview_citation_ids)
    if not used_ids.issubset(available_ids) or not used_ids.intersection(database_ids):
        raise SearchSummaryFailure("invalid_citations")


async def generate_grounded_search_summary(
    request: SearchSummaryRequest,
    uid: str,
    settings: Settings,
    db: AsyncSession,
    *,
    client_factory: Any = AsyncOpenAI,
) -> SearchSummaryResponse:
    await validate_confirmed_search_run(request, uid, db)
    evidence_payload, references = await load_database_summary_evidence(request.cluster_ids, db)

    if request.web_query:
        web_response = await search_web_evidence(
            WebSearchRequest(query=request.web_query, confirmed=True),
            settings,
        )
        for source in web_response.sources:
            evidence_payload.append({
                "citation_id": source.citation_id,
                "source_type": "web",
                "title": source.title,
                "snippet": source.snippet,
            })
            references.append(SearchSummaryEvidence(
                citation_id=source.citation_id,
                source_type="web",
                label=source.title,
                url=source.url,
            ))

    output, provider_name = await _generate_summary_output(
        evidence_payload,
        settings,
        client_factory=client_factory,
    )
    _validate_grounding(output, references)
    return SearchSummaryResponse(
        **output.model_dump(),
        evidence=references,
        provider=provider_name,
    )
