from datetime import datetime, timezone

import pytest

from app.auth import get_uid
from app.browse_contract import BrowseCategory, BrowseSort
from app.browse_cursor import (
    BrowseCursor,
    decode_browse_cursor,
    encode_browse_cursor,
)
from app.config import PIPELINE_VERSION
from conftest import FakeResult, make_cluster as _make_cluster


def make_cluster(**kwargs):
    kwargs.setdefault("discovery_category", "uncategorized")
    return _make_cluster(**kwargs)


@pytest.mark.parametrize("date", [datetime(2026, 10, 10, tzinfo=timezone.utc), None])
async def test_browse_page_returns_cards_and_cursor(client, fake_db, date):
    first = make_cluster(id=30, name="First", summary="First summary")
    second = make_cluster(id=20)
    extra = make_cluster(id=10)
    fake_db.stub(execute=[
        FakeResult(rows=[
            (first, 3, date, "First problem"),
            (second, 2, date, "Second problem"),
            (extra, 1, date, "Extra problem"),
        ]),
        FakeResult(rows=[("30",)]),
    ])

    response = await client.get("/clusters/browse/page", params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"clusters", "nextCursor"}
    assert [cluster["id"] for cluster in body["clusters"]] == [30, 20]
    assert body["clusters"][0] == {
        "id": 30,
        "name": "First",
        "summary": "First summary",
        "category": "uncategorized",
        "problemStatement": "First problem",
        "postCount": 3,
        "isWatched": True,
        "date": "2026-10-10T00:00:00Z" if date else None,
    }
    assert body["clusters"][1]["isWatched"] is False
    assert isinstance(body["nextCursor"], str)

    cursor = decode_browse_cursor(
        body["nextCursor"],
        category=BrowseCategory.ALL,
        sort=BrowseSort.NEWEST,
        pipeline_version=PIPELINE_VERSION,
    )
    assert cursor.cluster_id == 20
    assert cursor.post_count == 2
    assert cursor.last_observed == date


@pytest.mark.parametrize("row_count", [0, 1, 2])
async def test_browse_last_page_has_no_cursor(client, fake_db, row_count):
    rows = [(make_cluster(id=30 - i), 1, None, "Valid problem") for i in range(row_count)]
    results = [FakeResult(rows=rows)]
    if rows:
        results.append(FakeResult(rows=[]))
    fake_db.stub(execute=results)

    response = await client.get("/clusters/browse/page", params={"limit": 2})

    assert response.status_code == 200
    assert len(response.json()["clusters"]) == row_count
    assert response.json()["nextCursor"] is None


@pytest.mark.parametrize("params", [
    {"limit": 0},
    {"limit": 51},
    {"limit": "invalid"},
    {"sort": "unknown"},
    {"category": "unknown"},
    {"cursor": ""},
    {"cursor": "!invalid!"},
    {"cursor": "e30"},
    {"cursor": "a" * 2049},
])
async def test_browse_rejects_invalid_input(client, fake_db, params):
    fake_db.stub(execute=[AssertionError("Invalid input must not query the database")])

    response = await client.get("/clusters/browse/page", params=params)

    assert response.status_code == 422


@pytest.mark.parametrize("mismatch", ["sort", "pipeline"])
async def test_browse_rejects_incompatible_cursor(client, fake_db, mismatch):
    cursor = BrowseCursor(
        version=1,
        category=BrowseCategory.ALL,
        sort=BrowseSort.OLDEST if mismatch == "sort" else BrowseSort.NEWEST,
        pipeline_version="incompatible" if mismatch == "pipeline" else PIPELINE_VERSION,
        cluster_id=20,
        post_count=2,
        last_observed=None,
    )
    fake_db.stub(execute=[AssertionError("Invalid cursor must not query the database")])

    response = await client.get(
        "/clusters/browse/page",
        params={"cursor": encode_browse_cursor(cursor)},
    )

    assert response.status_code == 422


async def test_browse_requires_authentication(client, app, fake_db):
    app.dependency_overrides.pop(get_uid)
    fake_db.stub(execute=[AssertionError("Unauthenticated request must not query the database")])

    response = await client.get("/clusters/browse/page")

    assert response.status_code == 401
