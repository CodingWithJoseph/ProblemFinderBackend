"""Tests for the /problems routes.

Focus:
* POST creates a problem and schedules the embedding background task.
* PATCH re-embeds only when title/description changes.
* GET /{id}/similar-posts returns opportunities ranked by embedding distance.
* GET /similar returns problem pairs whose cosine similarity exceeds the threshold.
"""

from unittest.mock import AsyncMock

from app.routes import problems as prob_module
from conftest import FakeResult, make_problem, opportunity_row


async def test_create_problem_schedules_embedding_task(client, fake_db, auth_headers, monkeypatch):
    embed_mock = AsyncMock()
    monkeypatch.setattr(prob_module, "_embed_problem", embed_mock)

    resp = await client.post(
        "/problems",
        headers=auth_headers,
        json={"pipeline_id": "11111111-1111-1111-1111-111111111111", "title": "Invoices never match"},
    )
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert data["title"] == "Invoices never match"
    assert data["embeddingReady"] is False  # embedding not computed yet

    # The embedding is computed out-of-band via a background task.
    embed_mock.assert_awaited_once()
    assert fake_db.commit_count >= 1
    assert len(fake_db.added) == 1


async def test_patch_problem_reembeds_on_title_change(client, fake_db, auth_headers, monkeypatch):
    problem = make_problem(title="Old title", embedding=[0.1, 0.2, 0.3])
    fake_db.stub(execute=[FakeResult(rows=[problem])])

    embed_mock = AsyncMock()
    monkeypatch.setattr(prob_module, "_embed_problem", embed_mock)

    resp = await client.patch(
        f"/problems/{problem.id}",
        headers=auth_headers,
        json={"title": "New title"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "New title"
    embed_mock.assert_awaited_once()


async def test_patch_problem_no_reembed_on_position_only(client, fake_db, auth_headers, monkeypatch):
    problem = make_problem(title="Stable title", position=0)
    fake_db.stub(execute=[FakeResult(rows=[problem])])

    embed_mock = AsyncMock()
    monkeypatch.setattr(prob_module, "_embed_problem", embed_mock)

    resp = await client.patch(
        f"/problems/{problem.id}",
        headers=auth_headers,
        json={"position": 3},
    )
    assert resp.status_code == 200
    # Position-only change leaves the embedding valid -> no re-embed.
    embed_mock.assert_not_awaited()


async def test_similar_posts_returns_opportunities(client, fake_db, auth_headers):
    problem = make_problem(embedding=[1.0, 0.0, 0.0])
    row = opportunity_row(cluster_id=None)
    row["similarity"] = 0.93
    fake_db.stub(
        execute=[
            FakeResult(rows=[problem]),     # problem lookup
            FakeResult(mappings=[row]),     # vector-distance search
        ]
    )

    resp = await client.get(f"/problems/{problem.id}/similar-posts?limit=10", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["embeddingReady"] is True
    assert len(body["data"]) == 1
    item = body["data"][0]
    assert item["similarity"] == 0.93
    assert item["sourceScore"] == row["source_score"]
    assert "signalScore" not in item
    assert "intensityScore" not in item


async def test_similar_posts_not_ready_when_no_embedding(client, fake_db, auth_headers):
    problem = make_problem(embedding=None)
    fake_db.stub(execute=[FakeResult(rows=[problem])])

    resp = await client.get(f"/problems/{problem.id}/similar-posts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"data": [], "embeddingReady": False}


async def test_similar_posts_problem_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get(
        "/problems/22222222-2222-2222-2222-222222222222/similar-posts",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_similar_problems_returns_pairs_above_threshold(client, fake_db, auth_headers):
    pipeline_id = "33333333-3333-3333-3333-333333333333"
    p1 = make_problem(pipeline_id=pipeline_id, title="Reconcile invoices", embedding=[1.0, 0.0, 0.0])
    p2 = make_problem(pipeline_id=pipeline_id, title="Invoice reconciliation", embedding=[1.0, 0.0, 0.0])
    p3 = make_problem(pipeline_id=pipeline_id, title="Totally unrelated", embedding=[0.0, 1.0, 0.0])
    fake_db.stub(execute=[FakeResult(rows=[p1, p2, p3])])

    resp = await client.get(f"/problems/similar?pipeline_id={pipeline_id}", headers=auth_headers)
    assert resp.status_code == 200

    pairs = resp.json()["data"]
    # Only the near-identical pair (cosine 1.0) clears the 0.85 threshold.
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["similarity"] == 1.0
    assert {pair["a"], pair["b"]} == {p1.id, p2.id}
    assert {pair["aTitle"], pair["bTitle"]} == {"Reconcile invoices", "Invoice reconciliation"}
