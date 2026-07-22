from datetime import datetime, timedelta, timezone

from app.models import SearchRun, SearchSession, SearchTurn
from conftest import FakeResult, TEST_UID


NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def make_session(**overrides):
    values = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": TEST_UID,
        "title": "Invoice research",
        "saved_at": None,
        "archived_at": None,
        "expires_at": NOW + timedelta(days=30),
        "last_activity_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SearchSession(**values)


def draft():
    return {
        "query": "late invoice payments",
        "opportunity_domains": ["fintech"],
        "opportunity_types": ["software"],
        "sources": ["reddit"],
        "communities": ["r/freelance"],
        "min_posts": 5,
        "observed_after": None,
        "trending_only": False,
        "min_signal_score": 0.7,
        "sort": "signal_score",
        "limit": 20,
        "offset": 0,
    }


def interpretation():
    return {
        "draft": draft(),
        "confirmation": "Confirm these filters before I search the database.",
        "assumptions": ["Payment delays were interpreted as invoicing problems."],
        "unsupported": [],
        "clarification_question": None,
        "needs_clarification": False,
        "needs_confirmation": True,
        "fallback_used": False,
        "available_options": {
            "opportunity_domains": ["fintech"],
            "opportunity_types": ["software"],
            "sources": ["reddit"],
            "communities": ["r/freelance"],
        },
    }


async def test_create_session_is_owned_and_expires(client, fake_db, auth_headers):
    response = await client.post(
        "/clusters/search/sessions",
        headers=auth_headers,
        json={},
    )

    assert response.status_code == 201
    assert response.json()["title"] == "New search"
    assert response.json()["saved"] is False
    session = next(item for item in fake_db.added if isinstance(item, SearchSession))
    assert session.user_id == TEST_UID
    assert timedelta(days=29) < session.expires_at - session.created_at <= timedelta(days=30)
    assert fake_db.commit_count == 1


async def test_list_recent_sessions_purges_expired_rows(client, fake_db, auth_headers):
    session = make_session()
    fake_db.stub(execute=[FakeResult(), FakeResult(rows=[session])])

    response = await client.get(
        "/clusters/search/sessions?view=recent",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == session.id
    assert response.json()[0]["expires_at"] is not None
    assert fake_db.commit_count == 1


async def test_get_session_returns_turns_and_runs(client, fake_db, auth_headers):
    session = make_session(saved_at=NOW, expires_at=None)
    turn = SearchTurn(
        id="22222222-2222-2222-2222-222222222222",
        session_id=session.id,
        user_message="Find invoice problems",
        interpretation=interpretation(),
        created_at=NOW,
    )
    run = SearchRun(
        id="33333333-3333-3333-3333-333333333333",
        session_id=session.id,
        draft=draft(),
        result_cluster_ids=["42", "43"],
        result_count=2,
        created_at=NOW,
    )
    fake_db.stub(
        scalar=[session],
        execute=[FakeResult(rows=[turn]), FakeResult(rows=[run])],
    )

    response = await client.get(
        f"/clusters/search/sessions/{session.id}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["turns"][0]["user_message"] == "Find invoice problems"
    assert response.json()["runs"][0]["result_cluster_ids"] == ["42", "43"]


async def test_update_session_can_rename_save_and_archive(client, fake_db, auth_headers):
    session = make_session()
    fake_db.stub(scalar=[session])

    response = await client.patch(
        f"/clusters/search/sessions/{session.id}",
        headers=auth_headers,
        json={"title": "Saved invoice research", "saved": True, "archived": True},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Saved invoice research"
    assert response.json()["saved"] is True
    assert response.json()["archived"] is True
    assert response.json()["expires_at"] is None
    assert fake_db.commit_count == 1


async def test_add_turn_derives_title_and_stores_validated_interpretation(client, fake_db, auth_headers):
    session = make_session(title="New search")
    fake_db.stub(scalar=[session])

    response = await client.post(
        f"/clusters/search/sessions/{session.id}/turns",
        headers=auth_headers,
        json={
            "user_message": "Find recurring late invoice problems for freelancers",
            "interpretation": interpretation(),
        },
    )

    assert response.status_code == 201
    turn = next(item for item in fake_db.added if isinstance(item, SearchTurn))
    assert turn.interpretation["draft"]["query"] == "late invoice payments"
    assert session.title == "Find recurring late invoice problems for freelancers"
    assert response.json()["interpretation"]["needs_confirmation"] is True


async def test_add_run_stores_ids_not_result_payloads(client, fake_db, auth_headers):
    session = make_session()
    fake_db.stub(scalar=[session])

    response = await client.post(
        f"/clusters/search/sessions/{session.id}/runs",
        headers=auth_headers,
        json={
            "draft": draft(),
            "result_cluster_ids": ["42", "42", "43"],
            "result_count": 200,
        },
    )

    assert response.status_code == 201
    run = next(item for item in fake_db.added if isinstance(item, SearchRun))
    assert run.result_cluster_ids == ["42", "43"]
    assert run.result_count == 200
    assert not hasattr(run, "results")


async def test_delete_session_is_hard_delete(client, fake_db, auth_headers):
    session = make_session()
    fake_db.stub(scalar=[session])

    response = await client.delete(
        f"/clusters/search/sessions/{session.id}",
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert fake_db.deleted == [session]
    assert fake_db.commit_count == 1


async def test_session_endpoints_do_not_expose_other_users_records(client, fake_db, auth_headers):
    fake_db.stub(scalar=[None])

    response = await client.get(
        "/clusters/search/sessions/11111111-1111-1111-1111-111111111111",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Search session not found"
