from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline


async def test_pipeline_list_exposes_release_card_contract(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.get("/pipeline", headers=auth_headers)

    assert response.status_code == 200
    row = response.json()["data"][0]
    assert set(row) == {
        "id",
        "userId",
        "name",
        "projectName",
        "displayName",
        "postIds",
        "sourceClusterId",
        "stage",
        "launchedAt",
        "removedAt",
        "createdAt",
        "updatedAt",
    }


async def test_pipeline_list_includes_launched_cards(client, fake_db, auth_headers):
    card = make_pipeline(launched_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.get("/pipeline", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["data"][0]["launchedAt"] is not None


async def test_display_name_falls_back_to_cluster_name(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42", project_name=None)
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.get(f"/pipeline/{card.id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["data"]["displayName"] == "Cluster 42"


async def test_patch_sets_project_name(client, fake_db, auth_headers):
    card = make_pipeline(name="Cluster 42")
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.patch(
        f"/pipeline/{card.id}",
        json={"project_name": "Invoice Ninja"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["displayName"] == "Invoice Ninja"


async def test_patch_stage_updates_released_pipeline_lifecycle(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline(stage="watching")
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.patch(
        f"/pipeline/{card.id}",
        json={"stage": "exploring"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert card.stage == "exploring"


async def test_launch_moves_card_to_launched_surface(client, fake_db, auth_headers):
    card = make_pipeline(name="PC Boot Failures")
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.post(
        f"/pipeline/{card.id}/launch",
        json={"product_name": card.name},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["launchedAt"] is not None
