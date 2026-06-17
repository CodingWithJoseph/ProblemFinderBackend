from conftest import FakeResult, TEST_UID, make_team, make_team_member
from app.models import Team, TeamMember


async def test_create_team_current_user_becomes_owner(client, fake_db, auth_headers):
    resp = await client.post(
        "/teams",
        json={"name": "Launch Lab", "description": "Collaborative research"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == "Launch Lab"
    assert body["role"] == "owner"
    assert body["members"][0]["role"] == "owner"
    assert body["members"][0]["userId"] == TEST_UID
    assert any(isinstance(obj, Team) for obj in fake_db.added)
    assert any(isinstance(obj, TeamMember) and obj.role == "owner" for obj in fake_db.added)
    assert fake_db.commit_count == 1


async def test_list_teams(client, fake_db, auth_headers):
    team = make_team()
    fake_db.stub(execute=[FakeResult(rows=[team])])

    resp = await client.get("/teams", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == team.id
    assert resp.json()["data"][0]["role"] == "owner"


async def test_add_member(client, fake_db, auth_headers):
    team = make_team()
    fake_db.stub(execute=[FakeResult(rows=[team])])

    resp = await client.post(
        f"/teams/{team.id}/members",
        json={"email": "new@example.com", "display_name": "New Person", "role": "member"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    member = resp.json()["data"]
    assert member["email"] == "new@example.com"
    assert member["displayName"] == "New Person"
    assert member["status"] == "invited"
    assert any(isinstance(obj, TeamMember) and obj.email == "new@example.com" for obj in fake_db.added)


async def test_update_member_role(client, fake_db, auth_headers):
    team = make_team()
    member = make_team_member(team_id=team.id, role="member")
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[member])])

    resp = await client.patch(
        f"/teams/{team.id}/members/{member.id}",
        json={"role": "admin"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["role"] == "admin"
    assert member.role == "admin"
    assert fake_db.commit_count == 1


async def test_remove_member(client, fake_db, auth_headers):
    team = make_team()
    member = make_team_member(team_id=team.id, role="member")
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[member])])

    resp = await client.delete(f"/teams/{team.id}/members/{member.id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert fake_db.deleted == [member]
    assert fake_db.commit_count == 1


async def test_unauthorized_user_cannot_manage_another_users_team(client, fake_db, auth_headers):
    team = make_team(owner_user_id="someone-else")
    fake_db.stub(execute=[FakeResult(rows=[team])])

    resp = await client.patch(
        f"/teams/{team.id}",
        json={"name": "Not mine"},
        headers=auth_headers,
    )

    assert resp.status_code == 403
    assert fake_db.commit_count == 0
