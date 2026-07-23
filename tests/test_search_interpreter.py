import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.search_contract import (
    ClusterSearchQuery,
    SearchFilterOptions,
    SearchInterpretationModelOutput,
    SearchInterpretRequest,
)
from app.services import search_interpreter
from app.services.search_interpreter_provider import SearchInterpreterProviderFailure
from conftest import FakeResult


class FakeProvider:
    name = "local"

    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.output


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(
        search_interpreter,
        "get_search_interpreter_provider",
        lambda settings: provider,
    )


def stub_filter_options(fake_db):
    fake_db.stub(execute=[
        FakeResult(rows=[("devtools",), ("fintech",)]),
        FakeResult(rows=[("service",), ("software",)]),
        FakeResult(rows=[("hackernews",), ("reddit",)]),
        FakeResult(rows=[("r/freelance",), ("r/startups",)]),
    ])


def test_validation_maps_healthcare_alias_to_live_domain_and_removes_filler_query():
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(query="problems in healthcare"),
        assumptions=[],
        unsupported=[],
        clarification_question=None,
    )

    validated = search_interpreter._validated_model_output(
        output,
        SearchInterpretRequest(message="Show me problems in healthcare"),
        SearchFilterOptions(opportunity_domains=["Healthcare and Social Care"]),
    )

    assert validated.draft.query is None
    assert validated.draft.opportunity_domains == ["Healthcare and Social Care"]
    assert validated.assumptions == [
        'Mapped "healthcare" to database domain "Healthcare and Social Care".'
    ]


def test_validation_applies_refinement_as_patch_and_preserves_unmentioned_filters():
    current = ClusterSearchQuery(
        query="healthcare staffing",
        sources=["reddit"],
        min_posts=10,
        sort="relevance",
    )
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(
            query="healthcare staffing",
            sources=["reddit"],
            min_posts=20,
            sort="signal_score",
        ),
        assumptions=[],
        unsupported=[],
        clarification_question=None,
    )

    validated = search_interpreter._validated_model_output(
        output,
        SearchInterpretRequest(
            message="Sort them by strongest signal",
            current_draft=current,
        ),
        SearchFilterOptions(sources=["reddit"]),
    )

    assert validated.draft.query == "healthcare staffing"
    assert validated.draft.sources == ["reddit"]
    assert validated.draft.min_posts == 10
    assert validated.draft.sort == "signal_score"


def test_validation_allows_explicit_minimum_post_refinement():
    current = ClusterSearchQuery(query="healthcare", min_posts=10)
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(query="least 25 posts", min_posts=25),
        assumptions=[],
        unsupported=[],
        clarification_question=None,
    )

    validated = search_interpreter._validated_model_output(
        output,
        SearchInterpretRequest(
            message="Require at least 25 posts",
            current_draft=current,
        ),
        SearchFilterOptions(),
    )

    assert validated.draft.min_posts == 25
    assert validated.draft.query == "healthcare"


async def test_interpret_search_returns_canonical_confirmable_draft_without_running_search(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    execute_count = 0
    original_execute = fake_db.execute

    async def count_execute(statement, params=None):
        nonlocal execute_count
        execute_count += 1
        return await original_execute(statement, params)

    monkeypatch.setattr(fake_db, "execute", count_execute)
    model_output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(
            query="late invoice payments",
            opportunity_domains=["FINTECH", "not-a-domain"],
            opportunity_types=["software"],
            sources=["REDDIT"],
            communities=["r/freelance"],
            min_posts=5,
            trending_only=True,
            min_signal_score=0.7,
            sort="signal_score",
            limit=50,
            offset=999,
        ),
        assumptions=["Interpreted payment delays as invoicing problems."],
        unsupported=[],
        clarification_question=None,
    )
    provider = FakeProvider(output=model_output)
    use_provider(monkeypatch, provider)

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={
            "message": "only show strong fintech software problems from Reddit",
            "current_draft": {
                "query": "invoice problems",
                "limit": 10,
                "offset": 30,
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback_used"] is False
    assert body["needs_confirmation"] is True
    assert body["needs_clarification"] is False
    assert body["draft"]["opportunity_domains"] == ["fintech"]
    assert body["draft"]["opportunity_types"] == ["software"]
    assert body["draft"]["sources"] == ["reddit"]
    assert body["draft"]["limit"] == 10
    assert body["draft"]["offset"] == 0
    assert "not-a-domain" in body["assumptions"][0]
    assert "Confirm these filters" in body["confirmation"]
    assert body["available_options"]["opportunity_domains"] == ["devtools", "fintech"]

    payload = provider.calls[0]["payload"]
    assert payload["current_utc"].endswith("+00:00")
    assert payload["latest_user_message"] == "only show strong fintech software problems from Reddit"
    assert payload["current_draft"]["offset"] == 30
    assert "test-uid" not in json.dumps(provider.calls)
    assert execute_count == 4  # Filter-option reads only; interpretation never runs search.


async def test_interpret_search_surfaces_one_clarification(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(query="payment problems"),
        assumptions=[],
        unsupported=[],
        clarification_question="Should recent mean the last 30 or 90 days?",
    )
    use_provider(monkeypatch, FakeProvider(output=output))

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "find recent payment problems"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is True
    assert body["needs_confirmation"] is False
    assert body["clarification_question"] == "Should recent mean the last 30 or 90 days?"
    assert body["confirmation"] == "I need one detail before I can prepare the database search."


@pytest.mark.parametrize("category", ["invalid_json", "schema_invalid", "timeout", "connection"])
async def test_interpret_search_falls_back_for_provider_failures(
    category,
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    use_provider(
        monkeypatch,
        FakeProvider(error=SearchInterpreterProviderFailure(category)),
    )

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={
            "message": "only accounting firms",
            "current_draft": {
                "query": "invoice problems",
                "opportunity_domains": ["fintech"],
                "min_posts": 4,
                "limit": 15,
                "offset": 45,
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback_used"] is True
    assert body["needs_confirmation"] is True
    assert body["draft"]["query"] == "only accounting firms"
    assert body["draft"]["opportunity_domains"] == ["fintech"]
    assert body["draft"]["min_posts"] == 4
    assert body["draft"]["limit"] == 15
    assert body["draft"]["offset"] == 0


async def test_interpret_search_falls_back_when_filter_options_fail(
    client,
    fake_db,
    auth_headers,
):
    fake_db.stub(execute=[SQLAlchemyError("database unavailable")])

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "fintech invoice problems"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback_used"] is True
    assert body["draft"]["query"] == "fintech invoice problems"
    assert body["available_options"]["opportunity_domains"] == []


async def test_interpret_search_falls_back_when_groq_key_is_missing(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    monkeypatch.setattr(
        search_interpreter,
        "get_settings",
        lambda: SimpleNamespace(
            search_interpreter_provider="groq",
            search_interpreter_base_url="https://api.groq.com/openai/v1",
            search_interpreter_model="openai/gpt-oss-20b",
            search_interpreter_timeout_seconds=8.0,
            groq_api_key="",
        ),
    )

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "fintech invoice problems"},
    )

    assert resp.status_code == 200
    assert resp.json()["fallback_used"] is True


async def test_interpret_search_logs_only_sanitized_failure_category(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
    caplog,
):
    stub_filter_options(fake_db)
    use_provider(monkeypatch, FakeProvider(error=RuntimeError("SENSITIVE_MODEL_RESPONSE")))

    with caplog.at_level(logging.WARNING, logger=search_interpreter.__name__):
        resp = await client.post(
            "/clusters/search/interpret",
            headers=auth_headers,
            json={"message": "SENSITIVE_USER_PROMPT"},
        )

    assert resp.status_code == 200
    assert resp.json()["fallback_used"] is True
    assert "category=unexpected" in caplog.text
    assert "SENSITIVE_USER_PROMPT" not in caplog.text
    assert "SENSITIVE_MODEL_RESPONSE" not in caplog.text
    assert "test-uid" not in caplog.text


async def test_search_filter_options_endpoint(client, fake_db, auth_headers):
    stub_filter_options(fake_db)

    resp = await client.get("/clusters/search/options", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "opportunity_domains": ["devtools", "fintech"],
        "opportunity_types": ["service", "software"],
        "sources": ["hackernews", "reddit"],
        "communities": ["r/freelance", "r/startups"],
    }


async def test_search_filter_options_returns_503_on_database_error(client, fake_db, auth_headers):
    fake_db.stub(execute=[SQLAlchemyError("database unavailable")])

    resp = await client.get("/clusters/search/options", headers=auth_headers)

    assert resp.status_code == 503


async def test_interpret_search_rejects_invalid_input(client, auth_headers):
    too_short = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "x"},
    )
    assert too_short.status_code == 422

    unknown = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "invoice problems", "execute": True},
    )
    assert unknown.status_code == 422
