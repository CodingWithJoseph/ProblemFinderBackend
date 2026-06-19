from datetime import datetime, timedelta, timezone

from conftest import FakeResult, TEST_UID, make_team, make_team_member
from app.models import Team, TeamMember

_FUTURE = datetime.now(timezone.utc) + timedelta(hours=24)
_PAST = datetime.now(timezone.utc) - timedelta(hours=24)


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
        json={"email": "new.person@example.com", "role": "member"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    member = resp.json()["data"]
    assert member["email"] == "new.person@example.com"
    assert member["displayName"] == "New Person"
    assert member["status"] == "invited"
    assert any(isinstance(obj, TeamMember) and obj.email == "new.person@example.com" for obj in fake_db.added)


async def test_add_member_rejects_duplicate_email(client, fake_db, auth_headers, monkeypatch):
    team = make_team()
    existing = make_team_member(team_id=team.id, status="invited", email="dupe@example.com", user_id=None)
    # team lookup, then the duplicate-check finds an existing pending invite.
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[existing])])
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("app.routes.teams.email_service.send", fake_send)

    resp = await client.post(
        f"/teams/{team.id}/members",
        json={"email": "DUPE@example.com", "role": "member"},  # case-insensitive match
        headers=auth_headers,
    )

    assert resp.status_code == 409
    assert "invited" in resp.json()["detail"]
    assert sent == []  # no second email
    assert not any(isinstance(obj, TeamMember) for obj in fake_db.added)


async def test_add_member_email_invite_mints_token_and_sends_email(client, fake_db, auth_headers, monkeypatch):
    team = make_team()
    fake_db.stub(execute=[FakeResult(rows=[team])])
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("app.routes.teams.email_service.send", fake_send)

    resp = await client.post(
        f"/teams/{team.id}/members",
        json={"email": "new.person@example.com", "role": "member"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    member_obj = next(o for o in fake_db.added if isinstance(o, TeamMember))
    assert member_obj.invite_token
    assert member_obj.invite_token_expires_at is not None
    assert len(sent) == 1
    assert sent[0]["to"] == "new.person@example.com"
    # The accept link (carrying the token) must be in the email body.
    assert member_obj.invite_token in sent[0]["text"]
    # ...and surfaced on the response so it can be copied/pasted.
    assert resp.json()["data"]["inviteUrl"].endswith(member_obj.invite_token)


async def test_get_team_includes_invite_url_for_owner(client, fake_db, auth_headers):
    team = make_team()  # owner = TEST_UID
    invited = make_team_member(team_id=team.id, status="invited", email="p@example.com", user_id=None, invite_token="tok-xyz")
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[invited])])

    resp = await client.get(f"/teams/{team.id}", headers=auth_headers)

    assert resp.status_code == 200
    member = resp.json()["data"]["members"][0]
    assert member["inviteUrl"].endswith("tok-xyz")


async def test_get_team_hides_invite_url_from_plain_member(client, fake_db, auth_headers):
    team = make_team(owner_user_id="someone-else")
    viewer = make_team_member(team_id=team.id, user_id=TEST_UID, role="member", status="active")
    invited = make_team_member(team_id=team.id, status="invited", email="p@example.com", user_id=None, invite_token="tok-xyz")
    # team lookup, membership (role resolution), then members list.
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[viewer]), FakeResult(rows=[viewer, invited])])

    resp = await client.get(f"/teams/{team.id}", headers=auth_headers)

    assert resp.status_code == 200
    invited_payload = next(m for m in resp.json()["data"]["members"] if m["status"] == "invited")
    assert "inviteUrl" not in invited_payload


async def test_add_member_with_user_id_does_not_invite(client, fake_db, auth_headers, monkeypatch):
    team = make_team()
    fake_db.stub(execute=[FakeResult(rows=[team])])
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("app.routes.teams.email_service.send", fake_send)

    resp = await client.post(
        f"/teams/{team.id}/members",
        json={"user_id": "linked-uid", "role": "member"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    member_obj = next(o for o in fake_db.added if isinstance(o, TeamMember))
    assert member_obj.invite_token is None
    assert sent == []


async def test_get_invite_preview(client, fake_db):
    team = make_team()
    member = make_team_member(
        team_id=team.id, status="invited", email="x@example.com",
        invite_token="tok123", invite_token_expires_at=_FUTURE, user_id=None,
    )
    fake_db.stub(execute=[FakeResult(rows=[member]), FakeResult(rows=[team])])

    resp = await client.get("/invites/tok123")  # public, no auth

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["teamName"] == team.name
    assert data["email"] == "x@example.com"
    assert data["expired"] is False


async def test_get_invite_unknown_token(client, fake_db):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get("/invites/nope")
    assert resp.status_code == 404


async def test_accept_invite_binds_user(client, fake_db, auth_headers):
    team = make_team()
    member = make_team_member(
        team_id=team.id, status="invited", email="x@example.com",
        invite_token="tok", invite_token_expires_at=_FUTURE, user_id=None,
    )
    fake_db.stub(execute=[FakeResult(rows=[member]), FakeResult(rows=[team])])

    resp = await client.post("/invites/tok/accept", headers=auth_headers)

    assert resp.status_code == 200
    assert member.status == "active"
    assert member.user_id == TEST_UID
    assert member.invite_token is None
    assert fake_db.commit_count == 1


async def test_accept_invite_expired(client, fake_db, auth_headers):
    member = make_team_member(
        status="invited", invite_token="tok", invite_token_expires_at=_PAST, user_id=None,
    )
    fake_db.stub(execute=[FakeResult(rows=[member])])

    resp = await client.post("/invites/tok/accept", headers=auth_headers)

    assert resp.status_code == 410
    assert member.status == "invited"


async def test_accept_invite_already_accepted(client, fake_db, auth_headers):
    member = make_team_member(status="active", invite_token="tok", invite_token_expires_at=_FUTURE)
    fake_db.stub(execute=[FakeResult(rows=[member])])

    resp = await client.post("/invites/tok/accept", headers=auth_headers)

    assert resp.status_code == 409


async def test_resend_invite(client, fake_db, auth_headers, monkeypatch):
    team = make_team()
    member = make_team_member(team_id=team.id, status="invited", email="x@example.com", user_id=None)
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[member])])
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("app.routes.teams.email_service.send", fake_send)

    resp = await client.post(f"/teams/{team.id}/members/{member.id}/resend", headers=auth_headers)

    assert resp.status_code == 200
    assert member.invite_token
    assert len(sent) == 1
    assert sent[0]["to"] == "x@example.com"


async def test_resend_invite_rejects_active_member(client, fake_db, auth_headers):
    team = make_team()
    member = make_team_member(team_id=team.id, status="active", email="x@example.com")
    fake_db.stub(execute=[FakeResult(rows=[team]), FakeResult(rows=[member])])

    resp = await client.post(f"/teams/{team.id}/members/{member.id}/resend", headers=auth_headers)

    assert resp.status_code == 400


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
