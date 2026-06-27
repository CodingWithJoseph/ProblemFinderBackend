from types import SimpleNamespace

from conftest import FakeResult
from app.services import alerts as alerts_mod
from app.services.alerts import (
    AlertPrefs,
    already_alerted,
    detect_error_spike,
    detect_revenue_drop,
    detect_signups_drop,
    evaluate_alerts,
    problem_from_alert,
    render_alert,
    resolve_owner_email,
)
from datetime import datetime, timezone


def test_problem_from_alert_error_spike():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    alert = {"type": "error_spike", "dedupe_key": "2026-06-01",
             "context": {"product": "App", "current": 60, "baseline": 10.0}}
    p = problem_from_alert(alert, now)
    assert p["kind"] == "error_spike"
    assert p["severity"] == "critical"
    assert p["metric"] == "errors"
    assert p["observed"] == 60.0
    assert p["baseline"] == 10.0
    assert p["detected_at"] == now


def test_problem_from_alert_signups_drop():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    alert = {"type": "signups_drop", "dedupe_key": "2026-W22",
             "context": {"current": 2, "previous": 10}}
    p = problem_from_alert(alert, now)
    assert p["metric"] == "signups"
    assert p["observed"] == 2.0 and p["baseline"] == 10.0


class FakeSettingsRow:
    """Stand-in for a MonitorAlertSettings row."""

    def __init__(self, **kw):
        defaults = dict(
            new_issue_enabled=True, error_spike_enabled=True,
            signups_drop_enabled=True, revenue_drop_enabled=True,
            error_spike_multiplier=None, signups_drop_pct=None, revenue_drop_pct=None,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


# ── detectors ────────────────────────────────────────────────────────────────

def test_detect_error_spike():
    assert detect_error_spike(30, 2.0, min_errors=5, multiplier=3.0) is True   # 30 >= 6
    assert detect_error_spike(3, 2.0, min_errors=5, multiplier=3.0) is False    # below floor
    assert detect_error_spike(6, 0.0, min_errors=5, multiplier=3.0) is True     # no baseline
    assert detect_error_spike(5, 10.0, min_errors=5, multiplier=3.0) is False   # 5 < 30


def test_detect_signups_drop():
    assert detect_signups_drop(2, 20, min_previous=5, drop_pct=0.5) is True     # 90% drop
    assert detect_signups_drop(9, 10, min_previous=5, drop_pct=0.5) is False    # 10% drop
    assert detect_signups_drop(0, 3, min_previous=5, drop_pct=0.5) is False     # baseline too small


def test_detect_revenue_drop():
    assert detect_revenue_drop(8000, 12000, drop_pct=0.2) is True               # 33% drop
    assert detect_revenue_drop(11000, 12000, drop_pct=0.2) is False             # 8% drop
    assert detect_revenue_drop(None, 12000, drop_pct=0.2) is False
    assert detect_revenue_drop(8000, None, drop_pct=0.2) is False


# ── evaluate (pure orchestration) ────────────────────────────────────────────

def test_evaluate_alerts_fires_all_four():
    snapshot = {
        "product_name": "Acme",
        "today": "2026-06-01",
        "week": "2026-W23",
        "new_issues": [{"id": "g1", "title": "TypeError: boom", "level": "error"}],
        "error": {"current": 30, "baseline": 2.0},
        "signups": {"current": 2, "previous": 20},
        "revenue": {"current_mrr": 8000, "previous_mrr": 12000},
    }
    types = {a["type"] for a in evaluate_alerts(snapshot)}
    assert types == {"new_issue", "error_spike", "signups_drop", "revenue_drop"}


def _all_fire_snapshot():
    return {
        "product_name": "Acme",
        "today": "2026-06-01",
        "week": "2026-W23",
        "new_issues": [{"id": "g1", "title": "TypeError: boom", "level": "error"}],
        "error": {"current": 30, "baseline": 2.0},
        "signups": {"current": 2, "previous": 20},
        "revenue": {"current_mrr": 8000, "previous_mrr": 12000},
    }


def test_evaluate_alerts_caps_new_issues():
    snapshot = {
        "product_name": "Acme", "today": "2026-06-01", "week": "2026-W23",
        "new_issues": [{"id": f"g{i}", "title": f"err {i}", "level": "error"} for i in range(10)],
        "error": {"current": 0, "baseline": 0.0},
        "signups": {"current": 10, "previous": 10},
        "revenue": {"current_mrr": None, "previous_mrr": None},
    }
    prefs = AlertPrefs(new_issue_cap=3)
    fired = [a for a in evaluate_alerts(snapshot, prefs) if a["type"] == "new_issue"]
    assert len(fired) == 3


def test_evaluate_alerts_respects_disabled_triggers():
    prefs = AlertPrefs(new_issue_enabled=False, error_spike_enabled=False)
    types = {a["type"] for a in evaluate_alerts(_all_fire_snapshot(), prefs)}
    assert types == {"signups_drop", "revenue_drop"}


def test_evaluate_alerts_respects_custom_thresholds():
    # A stricter 50% revenue-drop threshold should not fire on the 33% drop.
    prefs = AlertPrefs(revenue_drop_pct=0.5)
    types = {a["type"] for a in evaluate_alerts(_all_fire_snapshot(), prefs)}
    assert "revenue_drop" not in types


def test_alert_prefs_resolve_merges_overrides():
    row = FakeSettingsRow(error_spike_enabled=False, signups_drop_pct=0.8)
    prefs = AlertPrefs.resolve(row)
    assert prefs.error_spike_enabled is False
    assert prefs.signups_drop_pct == 0.8
    # Unset override falls back to the global default; min floors stay global.
    assert prefs.revenue_drop_pct == AlertPrefs.from_settings().revenue_drop_pct
    assert prefs.error_spike_min == AlertPrefs.from_settings().error_spike_min


def test_alert_prefs_resolve_none_is_all_defaults():
    prefs = AlertPrefs.resolve(None)
    assert prefs.new_issue_enabled and prefs.error_spike_enabled
    assert prefs.signups_drop_enabled and prefs.revenue_drop_enabled


def test_evaluate_alerts_quiet_when_healthy():
    snapshot = {
        "product_name": "Acme",
        "today": "2026-06-01",
        "week": "2026-W23",
        "new_issues": [],
        "error": {"current": 1, "baseline": 1.0},
        "signups": {"current": 22, "previous": 20},
        "revenue": {"current_mrr": 12500, "previous_mrr": 12000},
    }
    assert evaluate_alerts(snapshot) == []


# ── rendering ────────────────────────────────────────────────────────────────

def test_render_alert_subjects():
    s, _, _ = render_alert("new_issue", {"product": "Acme", "title": "TypeError", "level": "error"})
    assert "Acme" in s and "TypeError" in s
    s, _, _ = render_alert("revenue_drop", {"product": "Acme", "current_mrr": 8000, "previous_mrr": 12000})
    assert "Acme" in s and "MRR" in s


# ── recipient ────────────────────────────────────────────────────────────────

def test_resolve_owner_email(monkeypatch):
    import firebase_admin
    monkeypatch.setattr(firebase_admin.auth, "get_user", lambda uid: SimpleNamespace(email="owner@example.com"))
    assert resolve_owner_email("uid-1") == "owner@example.com"


def test_resolve_owner_email_missing(monkeypatch):
    import firebase_admin
    monkeypatch.setattr(firebase_admin.auth, "get_user", lambda uid: SimpleNamespace(email=None))
    assert resolve_owner_email("uid-1") is None


# ── dedupe ───────────────────────────────────────────────────────────────────

async def test_already_alerted(fake_db):
    fake_db.stub(execute=[FakeResult(rows=["existing-id"]), FakeResult(rows=[])])
    assert await already_alerted(fake_db, "p1", "new_issue", "g1") is True
    assert await already_alerted(fake_db, "p1", "new_issue", "g2") is False
