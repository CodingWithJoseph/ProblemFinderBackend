import json
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.models import Cluster, ClusterItem, SearchRun
from app.routes import clusters as cluster_routes
from app.search_summary_contract import (
    SearchSummaryEvidence,
    SearchSummaryFinding,
    SearchSummaryRequest,
    SearchSummaryResponse,
)
from app.services import search_summary
from app.services.search_summary import SearchSummaryFailure, generate_grounded_search_summary
from app.web_search_contract import WebSearchResponse, WebSearchSource
from conftest import FakeResult


SEARCH_RUN_ID = "22222222-2222-2222-2222-222222222222"


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content, refusal=None))]
        )


class FakeClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


def summary_content(*citation_ids):
    return json.dumps({
        "overview": "Teams repeatedly report a costly coordination problem.",
        "overview_citation_ids": [citation_ids[0]],
        "findings": [{
            "text": "The problem appears across multiple evidence records.",
            "citation_ids": list(citation_ids),
        }],
        "limitations": ["The evidence sample is bounded."],
    })


def stub_cluster_evidence(fake_db, *, result_cluster_ids=None):
    run = SearchRun(
        id=SEARCH_RUN_ID,
        session_id="33333333-3333-3333-3333-333333333333",
        draft={},
        result_cluster_ids=result_cluster_ids or ["42"],
        result_count=1,
    )
    cluster = Cluster(
        id=42,
        name="Healthcare teams struggle with handoffs",
        summary="Care teams lose context during shift changes.",
        signal_score=0.88,
        pipeline_version="v2",
    )
    item = ClusterItem(
        id="11111111-1111-1111-1111-111111111111",
        cluster_id=42,
        pipeline_version="v2",
        title="Shift handoff notes are fragmented",
        body="RAW_BODY_MUST_NOT_BE_SENT",
        problem_statement="Nurses repeat work because context is scattered.",
        solution_angle="Create a structured handoff workspace.",
        platform="reddit",
        community="r/healthIT",
        score=12,
    )
    fake_db.stub(
        scalar=[run],
        execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item])],
    )


async def test_summary_route_is_hidden_when_disabled(client, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "search_summaries_enabled", False)

    response = await client.post(
        "/clusters/search/summary",
        headers=auth_headers,
        json={"search_run_id": SEARCH_RUN_ID, "cluster_ids": [42], "confirmed": True},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


async def test_summary_route_requires_confirmation(client, auth_headers):
    response = await client.post(
        "/clusters/search/summary",
        headers=auth_headers,
        json={"search_run_id": SEARCH_RUN_ID, "cluster_ids": [42], "confirmed": False},
    )

    assert response.status_code == 422


async def test_summary_route_returns_grounded_contract(client, auth_headers, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "search_summaries_enabled", True)

    async def fake_summary(body, uid, current_settings, db):
        assert body.cluster_ids == [42]
        assert uid == "test-uid"
        assert current_settings is settings
        assert db is not None
        return SearchSummaryResponse(
            overview="Grounded overview",
            overview_citation_ids=["cluster-42"],
            findings=[SearchSummaryFinding(text="Grounded finding", citation_ids=["cluster-42"])],
            limitations=[],
            evidence=[SearchSummaryEvidence(
                citation_id="cluster-42",
                source_type="database_cluster",
                label="Healthcare handoffs",
                cluster_id=42,
            )],
            provider="local",
        )

    monkeypatch.setattr(cluster_routes, "generate_grounded_search_summary", fake_summary)
    response = await client.post(
        "/clusters/search/summary",
        headers=auth_headers,
        json={"search_run_id": SEARCH_RUN_ID, "cluster_ids": [42], "confirmed": True},
    )

    assert response.status_code == 200
    assert response.json()["findings"][0]["citation_ids"] == ["cluster-42"]
    assert response.json()["evidence"][0]["cluster_id"] == 42


async def test_summary_reloads_database_evidence_and_validates_citations(
    fake_db,
    monkeypatch,
):
    stub_cluster_evidence(fake_db)
    settings = get_settings()
    monkeypatch.setattr(settings, "search_interpreter_provider", "local")
    fake_client = FakeClient(summary_content("cluster-42", "item-42-1"))

    response = await generate_grounded_search_summary(
        SearchSummaryRequest(search_run_id=SEARCH_RUN_ID, cluster_ids=[42], confirmed=True),
        "test-uid",
        settings,
        fake_db,
        client_factory=lambda **kwargs: fake_client,
    )

    assert response.provider == "local"
    assert [evidence.citation_id for evidence in response.evidence] == [
        "cluster-42",
        "item-42-1",
    ]
    provider_request = fake_client.chat.completions.requests[0]
    supplied = json.loads(provider_request["messages"][1]["content"])
    assert supplied["evidence"][0]["summary"] == "Care teams lose context during shift changes."
    assert supplied["evidence"][1]["problem_statement"] == (
        "Nurses repeat work because context is scattered."
    )
    assert "RAW_BODY_MUST_NOT_BE_SENT" not in json.dumps(supplied)
    assert "test-uid" not in json.dumps(supplied)
    assert provider_request["response_format"]["json_schema"]["strict"] is True


async def test_summary_rejects_model_citations_outside_server_evidence(fake_db, monkeypatch):
    stub_cluster_evidence(fake_db)
    settings = get_settings()
    monkeypatch.setattr(settings, "search_interpreter_provider", "local")
    fake_client = FakeClient(summary_content("invented-source"))

    with pytest.raises(SearchSummaryFailure, match="invalid_citations"):
        await generate_grounded_search_summary(
            SearchSummaryRequest(search_run_id=SEARCH_RUN_ID, cluster_ids=[42], confirmed=True),
            "test-uid",
            settings,
            fake_db,
            client_factory=lambda **kwargs: fake_client,
        )


async def test_summary_fails_before_provider_when_cluster_is_missing(fake_db):
    stub_cluster_evidence(fake_db, result_cluster_ids=["404"])
    fake_db.execute_results = [FakeResult(rows=[])]

    with pytest.raises(SearchSummaryFailure, match="clusters_not_found"):
        await generate_grounded_search_summary(
            SearchSummaryRequest(search_run_id=SEARCH_RUN_ID, cluster_ids=[404], confirmed=True),
            "test-uid",
            get_settings(),
            fake_db,
            client_factory=lambda **kwargs: pytest.fail("provider must not be called"),
        )


async def test_summary_rejects_clusters_outside_the_owned_confirmed_run(fake_db):
    stub_cluster_evidence(fake_db, result_cluster_ids=["99"])

    with pytest.raises(SearchSummaryFailure, match="invalid_cluster_selection"):
        await generate_grounded_search_summary(
            SearchSummaryRequest(search_run_id=SEARCH_RUN_ID, cluster_ids=[42], confirmed=True),
            "test-uid",
            get_settings(),
            fake_db,
            client_factory=lambda **kwargs: pytest.fail("provider must not be called"),
        )

    assert len(fake_db.execute_results) == 2  # Database evidence was never loaded.


async def test_summary_does_not_expose_an_unowned_search_run(fake_db):
    fake_db.stub(scalar=[None])

    with pytest.raises(SearchSummaryFailure, match="search_run_not_found"):
        await generate_grounded_search_summary(
            SearchSummaryRequest(search_run_id=SEARCH_RUN_ID, cluster_ids=[42], confirmed=True),
            "different-user",
            get_settings(),
            fake_db,
            client_factory=lambda **kwargs: pytest.fail("provider must not be called"),
        )


async def test_summary_can_add_only_server_normalized_web_evidence(fake_db, monkeypatch):
    stub_cluster_evidence(fake_db)
    settings = get_settings()
    monkeypatch.setattr(settings, "search_interpreter_provider", "local")
    fake_client = FakeClient(summary_content("cluster-42", "web-1"))

    async def fake_web_search(request, current_settings):
        assert request.query == "current healthcare handoff research"
        assert request.confirmed is True
        assert current_settings is settings
        return WebSearchResponse(
            query=request.query,
            sources=[WebSearchSource(
                citation_id="web-1",
                title="Current research",
                url="https://example.org/research",
                snippet="Recent external evidence.",
                score=0.9,
            )],
        )

    monkeypatch.setattr(search_summary, "search_web_evidence", fake_web_search)
    response = await generate_grounded_search_summary(
        SearchSummaryRequest(
            search_run_id=SEARCH_RUN_ID,
            cluster_ids=[42],
            confirmed=True,
            web_query="current healthcare handoff research",
        ),
        "test-uid",
        settings,
        fake_db,
        client_factory=lambda **kwargs: fake_client,
    )

    web_evidence = next(item for item in response.evidence if item.source_type == "web")
    assert web_evidence.citation_id == "web-1"
    assert web_evidence.url == "https://example.org/research"
    supplied = json.loads(fake_client.chat.completions.requests[0]["messages"][1]["content"])
    assert supplied["evidence"][-1] == {
        "citation_id": "web-1",
        "source_type": "web",
        "title": "Current research",
        "snippet": "Recent external evidence.",
    }
