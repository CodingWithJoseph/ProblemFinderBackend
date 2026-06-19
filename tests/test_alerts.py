from types import SimpleNamespace

from conftest import FakeResult
from app.services import alerts as alerts_mod
from app.services.alerts import (
    already_alerted,
    detect_error_spike,
    detect_revenue_drop,
    detect_signups_drop,
    evaluate_alerts,
    render_alert,
    resolve_owner_email,
)


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
