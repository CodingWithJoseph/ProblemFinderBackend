import json
from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

from app.search_contract import ClusterSearchQuery, SearchInterpretationModelOutput
from app.services import search_interpreter
from conftest import FakeResult


class FakeResponses:
    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.output)


class FakeOpenAIClient:
    def __init__(self, output=None, error: Exception | None = None):
        self.responses = FakeResponses(output=output, error=error)


def stub_filter_options(fake_db):
    fake_db.stub(execute=[
        FakeResult(rows=[("devtools",), ("fintech",)]),
        FakeResult(rows=[("service",), ("software",)]),
        FakeResult(rows=[("hackernews",), ("reddit",)]),
        FakeResult(rows=[("r/freelance",), ("r/startups",)]),
    ])


async def test_interpret_search_returns_canonical_confirmable_draft(
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
    fake_openai = FakeOpenAIClient(output=model_output)
    monkeypatch.setattr(search_interpreter, "openai_client", fake_openai)

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

    call = fake_openai.responses.kwargs
    assert call["model"] == "gpt-5.6-luna"
    assert call["store"] is False
    assert call["safety_identifier"].startswith("pf_")
    assert "test-uid" not in call["safety_identifier"]
    assert call["text_format"] is SearchInterpretationModelOutput
    payload = json.loads(call["input"])
    assert payload["current_utc"].endswith("+00:00")
    assert payload["latest_user_message"] == "only show strong fintech software problems from Reddit"
    assert payload["current_draft"]["offset"] == 30
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
    monkeypatch.setattr(search_interpreter, "openai_client", FakeOpenAIClient(output=output))

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


async def test_interpret_search_falls_back_without_losing_existing_filters(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    monkeypatch.setattr(
        search_interpreter,
        "openai_client",
        FakeOpenAIClient(error=RuntimeError("provider unavailable")),
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


async def test_interpret_search_falls_back_when_api_key_is_missing(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    fake_openai = FakeOpenAIClient(
        error=AssertionError("OpenAI should not be called without an API key")
    )
    monkeypatch.setattr(search_interpreter, "openai_client", fake_openai)
    monkeypatch.setattr(
        search_interpreter,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            search_interpreter_model="gpt-5.6-luna",
            search_interpreter_timeout_seconds=8.0,
        ),
    )

    resp = await client.post(
        "/clusters/search/interpret",
        headers=auth_headers,
        json={"message": "fintech invoice problems"},
    )

    assert resp.status_code == 200
    assert resp.json()["fallback_used"] is True
    assert fake_openai.responses.kwargs is None


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
