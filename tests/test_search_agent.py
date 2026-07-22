import copy
import json
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.search_contract import ClusterSearchQuery, SearchInterpretationModelOutput
from app.services import search_agent
from app.services.search_interpreter_provider import SearchAgentToolCall
from conftest import FakeResult


def tool_call(sequence: int, name: str, arguments: dict) -> SearchAgentToolCall:
    return SearchAgentToolCall(
        call_id=f"call-{sequence}",
        name=name,
        arguments=arguments,
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
    )


class FakeAgentProvider:
    name = "local"

    def __init__(self, calls=None, error: Exception | None = None):
        self.pending = list(calls or [])
        self.error = error
        self.requests = []

    async def next_agent_tool_call(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        return self.pending.pop(0)


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(
        search_agent,
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


def prepared_output(*, clarification_question=None) -> dict:
    return SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(
            query="late invoice payments",
            opportunity_domains=["FINTECH"],
            sources=["REDDIT"],
            min_posts=5,
            sort="relevance",
            limit=50,
            offset=100,
        ),
        assumptions=["Interpreted payment delays as invoicing problems."],
        unsupported=[],
        clarification_question=clarification_question,
    ).model_dump(mode="json")


async def test_search_agent_inspects_options_then_prepares_confirmable_draft(
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
    provider = FakeAgentProvider(calls=[
        tool_call(1, search_agent.FILTER_OPTIONS_TOOL, {}),
        tool_call(2, search_agent.PREPARE_DRAFT_TOOL, prepared_output()),
    ])
    use_provider(monkeypatch, provider)

    response = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "find fintech invoice problems on Reddit"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_used"] is False
    assert body["needs_confirmation"] is True
    assert body["stop_reason"] == "confirmation_required"
    assert body["draft"]["opportunity_domains"] == ["fintech"]
    assert body["draft"]["sources"] == ["reddit"]
    assert body["draft"]["limit"] == 20
    assert body["draft"]["offset"] == 0
    assert body["steps"] == [
        {"sequence": 1, "action": "inspect_filter_options", "outcome": "completed"},
        {"sequence": 2, "action": "prepare_search_draft", "outcome": "completed"},
    ]
    assert execute_count == 4  # Metadata only; no cluster search was executed.
    second_messages = provider.requests[1]["messages"]
    assert "available_options" in second_messages[-1]["content"]
    assert "test-uid" not in json.dumps(provider.requests)


async def test_search_agent_rejects_draft_until_options_are_inspected(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    provider = FakeAgentProvider(calls=[
        tool_call(1, search_agent.PREPARE_DRAFT_TOOL, prepared_output()),
        tool_call(2, search_agent.FILTER_OPTIONS_TOOL, {}),
        tool_call(3, search_agent.PREPARE_DRAFT_TOOL, prepared_output()),
    ])
    use_provider(monkeypatch, provider)

    response = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "find fintech invoice problems"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_used"] is False
    assert [step["outcome"] for step in body["steps"]] == ["rejected", "completed", "completed"]
    assert "filter_options_required" in provider.requests[1]["messages"][-1]["content"]


async def test_search_agent_stops_for_clarification(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    stub_filter_options(fake_db)
    provider = FakeAgentProvider(calls=[
        tool_call(1, search_agent.FILTER_OPTIONS_TOOL, {}),
        tool_call(
            2,
            search_agent.PREPARE_DRAFT_TOOL,
            prepared_output(clarification_question="Should recent mean 30 or 90 days?"),
        ),
    ])
    use_provider(monkeypatch, provider)

    response = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "find recent fintech invoice problems"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "clarification_required"
    assert body["needs_clarification"] is True
    assert body["needs_confirmation"] is False


async def test_search_agent_falls_back_at_step_limit(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    execute_count = 0
    original_execute = fake_db.execute

    async def count_execute(statement, params=None):
        nonlocal execute_count
        execute_count += 1
        return await original_execute(statement, params)

    monkeypatch.setattr(fake_db, "execute", count_execute)
    provider = FakeAgentProvider(calls=[
        tool_call(1, "search_the_web", {}),
        tool_call(2, "execute_search", {}),
        tool_call(3, "summarize_results", {}),
    ])
    use_provider(monkeypatch, provider)

    response = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "find fintech invoice problems"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_used"] is True
    assert body["stop_reason"] == "step_limit"
    assert len(body["steps"]) == search_agent.SEARCH_AGENT_MAX_STEPS
    assert {step["action"] for step in body["steps"]} == {"unsupported_tool"}
    assert execute_count == 0


async def test_search_agent_falls_back_when_options_are_unavailable(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
):
    fake_db.stub(execute=[SQLAlchemyError("SENSITIVE_DATABASE_ERROR")])
    provider = FakeAgentProvider(calls=[
        tool_call(1, search_agent.FILTER_OPTIONS_TOOL, {}),
    ])
    use_provider(monkeypatch, provider)

    response = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "find fintech invoice problems"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fallback_used"] is True
    assert body["stop_reason"] == "fallback"
    assert body["steps"][0]["action"] == "inspect_filter_options"
    assert body["steps"][0]["outcome"] == "rejected"


async def test_search_agent_logs_only_sanitized_provider_failure(
    client,
    fake_db,
    auth_headers,
    monkeypatch,
    caplog,
):
    provider = FakeAgentProvider(error=RuntimeError("SENSITIVE_MODEL_RESPONSE"))
    use_provider(monkeypatch, provider)

    with caplog.at_level(logging.WARNING, logger=search_agent.__name__):
        response = await client.post(
            "/clusters/search/agent",
            headers=auth_headers,
            json={"message": "SENSITIVE_USER_PROMPT"},
        )

    assert response.status_code == 200
    assert response.json()["fallback_used"] is True
    assert "category=unexpected" in caplog.text
    assert "SENSITIVE_USER_PROMPT" not in caplog.text
    assert "SENSITIVE_MODEL_RESPONSE" not in caplog.text
    assert "test-uid" not in caplog.text


async def test_search_agent_rejects_invalid_input(client, auth_headers):
    too_short = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "x"},
    )
    assert too_short.status_code == 422

    forbidden = await client.post(
        "/clusters/search/agent",
        headers=auth_headers,
        json={"message": "invoice problems", "auto_execute": True},
    )
    assert forbidden.status_code == 422
