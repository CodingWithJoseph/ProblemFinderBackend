from datetime import datetime, timedelta, timezone

from conftest import FakeResult, make_pipeline, make_problem, make_user_preference

from app.models import UserPreference
from app.services.preferences import NotificationPrefs, digest_interval
from app.services.alerts import render_digest


# ── service: NotificationPrefs / digest_interval ─────────────────────────────

def test_notification_prefs_defaults_when_no_row():
    np = NotificationPrefs.from_row(None)
    assert np.alerts_email_enabled is True
    assert np.digest_cadence == "instant"
    assert np.alert_email is None
    assert np.send_instantly is True


def test_notification_prefs_send_instantly_logic():
    assert NotificationPrefs(alerts_email_enabled=True, digest_cadence="instant").send_instantly is True
    assert NotificationPrefs(alerts_email_enabled=True, digest_cadence="daily").send_instantly is False
    assert NotificationPrefs(alerts_email_enabled=False, digest_cadence="instant").send_instantly is False


def test_digest_interval():
    assert digest_interval("daily") == timedelta(days=1)
    assert digest_interval("weekly") == timedelta(days=7)
    assert digest_interval("instant") is None


def test_render_digest_groups_by_product():
    items = [
        {"type": "new_issue", "product": "Acme"},
        {"type": "error_spike", "product": "Acme"},
        {"type": "revenue_drop", "product": "Beta"},
    ]
    subject, text, html = render_digest(items, "daily")
    assert "daily" in subject and "3 alerts" in subject
    assert "Acme" in text and "Beta" in text
    assert "New error issue" in text and "MRR dropped" in text
    assert "<li>Error spike</li>" in html


# ── endpoints ────────────────────────────────────────────────────────────────

async def test_get_preferences_defaults(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # no saved row
    resp = await client.get("/preferences", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["alertsEmailEnabled"] is True
    assert data["digestCadence"] == "instant"
    assert data["alertEmail"] is None


async def test_get_preferences_existing(client, fake_db, auth_headers):
    row = make_user_preference(digest_cadence="daily", alert_email="alerts@example.com", default_landing="dashboard")
    fake_db.stub(execute=[FakeResult(rows=[row])])
    resp = await client.get("/preferences", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["digestCadence"] == "daily"
    assert data["alertEmail"] == "alerts@example.com"
    assert data["defaultLanding"] == "dashboard"


async def test_update_preferences_creates_row(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # no existing row -> insert
    resp = await client.put(
        "/preferences",
        headers=auth_headers,
        json={"alerts_email_enabled": False, "digest_cadence": "weekly"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["alertsEmailEnabled"] is False
    assert data["digestCadence"] == "weekly"
    assert any(isinstance(o, UserPreference) for o in fake_db.added)
    assert fake_db.commit_count == 1


async def test_update_preferences_rejects_bad_cadence(client, fake_db, auth_headers):
    resp = await client.put("/preferences", headers=auth_headers, json={"digest_cadence": "hourly"})
    assert resp.status_code == 400
    assert not any(isinstance(o, UserPreference) for o in fake_db.added)


async def test_update_preferences_rejects_bad_email(client, fake_db, auth_headers):
    resp = await client.put("/preferences", headers=auth_headers, json={"alert_email": "not-an-email"})
    assert resp.status_code == 400


async def test_export_returns_user_data(client, fake_db, auth_headers):
    product = make_pipeline(name="LaunchKit")
    problem = make_problem(title="Invoices are late")
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # pipelines
        FakeResult(rows=[problem]),   # problems
        FakeResult(rows=[]),          # tasks
    ])
    resp = await client.get("/preferences/export", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "exportedAt" in data
    assert len(data["pipelines"]) == 1 and data["pipelines"][0]["name"] == "LaunchKit"
    assert len(data["problems"]) == 1 and data["problems"][0]["title"] == "Invoices are late"
    assert data["tasks"] == []
