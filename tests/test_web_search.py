from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.routes import clusters as cluster_routes
from app.services.web_search import (
    GROQ_BASE_URL,
    WebSearchFailure,
    normalize_source_url,
    resolve_web_search_config,
    search_web_evidence,
)
from app.web_search_contract import WebSearchRequest, WebSearchResponse, WebSearchSource


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


def provider_response(results):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="Generated prose that the application must ignore.",
                executed_tools=[{"search_results": {"results": results}}],
            )
        )]
    )


async def test_web_search_route_is_hidden_when_disabled(client, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "search_web_enabled", False)

    response = await client.post(
        "/clusters/search/web",
        headers=auth_headers,
        json={"query": "recent healthcare workflow problems", "confirmed": True},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


async def test_web_search_route_requires_explicit_confirmation(client, auth_headers):
    response = await client.post(
        "/clusters/search/web",
        headers=auth_headers,
        json={"query": "recent healthcare workflow problems", "confirmed": False},
    )

    assert response.status_code == 422


async def test_web_search_route_returns_only_normalized_sources(
    client,
    auth_headers,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "search_web_enabled", True)

    async def fake_search(body, current_settings):
        assert body.confirmed is True
        assert current_settings is settings
        return WebSearchResponse(
            query=body.query,
            sources=[WebSearchSource(
                citation_id="web-1",
                title="Healthcare source",
                url="https://example.com/evidence",
                snippet="Evidence snippet",
                score=0.9,
            )],
        )

    monkeypatch.setattr(cluster_routes, "search_web_evidence", fake_search)
    response = await client.post(
        "/clusters/search/web",
        headers=auth_headers,
        json={"query": "recent healthcare workflow problems", "confirmed": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "recent healthcare workflow problems",
        "sources": [{
            "citation_id": "web-1",
            "title": "Healthcare source",
            "url": "https://example.com/evidence",
            "snippet": "Evidence snippet",
            "score": 0.9,
        }],
        "provider": "groq_web_search",
    }


async def test_provider_search_uses_only_web_tool_and_sanitizes_citations(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "search_web_enabled", True)
    monkeypatch.setattr(settings, "groq_api_key", "secret-test-key")
    monkeypatch.setattr(settings, "search_web_model", "groq/compound-mini")
    response = provider_response([
        {
            "title": "Primary source",
            "url": "https://Example.com/article?utm_source=test&id=7#section",
            "content": "  Current evidence.  ",
            "score": 0.91,
        },
        {
            "title": "Duplicate source",
            "url": "https://example.com/article?id=7",
            "content": "Duplicate",
            "score": 0.8,
        },
        {
            "title": "Unsafe local source",
            "url": "https://127.0.0.1/internal",
            "content": "Must be rejected",
            "score": 0.7,
        },
        {
            "title": "Secondary source",
            "url": "https://news.example.org/story?gclid=tracking",
            "content": "More evidence",
            "score": "0.82",
        },
    ])
    fake_client = FakeClient(response)
    factory_kwargs = {}

    def client_factory(**kwargs):
        factory_kwargs.update(kwargs)
        return fake_client

    result = await search_web_evidence(
        WebSearchRequest(query="healthcare workflow problems", confirmed=True),
        settings,
        client_factory=client_factory,
    )

    assert factory_kwargs["base_url"] == GROQ_BASE_URL
    assert factory_kwargs["api_key"] == "secret-test-key"
    assert [source.url for source in result.sources] == [
        "https://example.com/article?id=7",
        "https://news.example.org/story",
    ]
    assert result.sources[0].snippet == "Current evidence."
    assert result.sources[1].score == 0.82
    request = fake_client.chat.completions.requests[0]
    assert request["model"] == "groq/compound-mini"
    assert request["extra_body"] == {
        "compound_custom": {"tools": {"enabled_tools": ["web_search"]}},
    }
    assert "secret-test-key" not in str(request)
    assert not hasattr(result, "content")


def test_web_search_configuration_fails_closed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "search_web_enabled", True)
    monkeypatch.setattr(settings, "groq_api_key", "")
    with pytest.raises(WebSearchFailure, match="missing_credentials"):
        resolve_web_search_config(settings)

    monkeypatch.setattr(settings, "groq_api_key", "secret-test-key")
    monkeypatch.setattr(settings, "search_web_model", "untrusted/model")
    with pytest.raises(WebSearchFailure, match="invalid_configuration"):
        resolve_web_search_config(settings)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/insecure",
        "https://localhost/private",
        "https://10.0.0.1/private",
        "https://user:password@example.com/private",
        "https://example.com/unsafe\nheader",
        "javascript:alert(1)",
    ],
)
def test_source_url_normalizer_rejects_unsafe_links(url):
    assert normalize_source_url(url) is None
