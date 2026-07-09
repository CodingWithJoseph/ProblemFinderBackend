from datetime import datetime, timezone

from conftest import FakeResult, make_pipeline
from app.models import (
    MonitorInvestigation,
    MonitorInvestigationEntry,
    MonitorReport,
)


def _inv(pipeline_id, **kw):
    defaults = dict(
        id="inv-1", pipeline_id=pipeline_id, title="Checkout broke", summary=None,
        status="open",
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return MonitorInvestigation(**defaults)


async def test_create_investigation(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])

    resp = await client.post(
        f"/portfolio/{product.id}/investigations",
        json={"title": "Checkout broke", "summary": "5xx spike after deploy"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Checkout broke"
    assert resp.json()["data"]["status"] == "open"
    assert any(isinstance(o, MonitorInvestigation) for o in fake_db.added)


async def test_create_monitor_investigation_from_evidence(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])

    resp = await client.post(
        f"/monitor/{product.id}/investigations/from-evidence",
        json={
            "kind": "trace",
            "ref_id": "trace-abc",
            "metadata": {"spanId": "span-1", "service": "api"},
        },
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["investigation"]["title"] == "Investigate trace: trace-abc"
    assert body["investigation"]["status"] == "open"
    assert body["timeline"][0]["kind"] == "trace"
    assert body["timeline"][0]["refId"] == "trace-abc"
    assert body["timeline"][0]["metadata"] == {"spanId": "span-1", "service": "api"}
    assert any(isinstance(o, MonitorInvestigation) for o in fake_db.added)
    assert any(isinstance(o, MonitorInvestigationEntry) for o in fake_db.added)


async def test_investigation_timeline(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    inv = _inv(product.id)
    note = MonitorInvestigationEntry(
        id="e1", investigation_id=inv.id, kind="note", ref_id=None, body="Looks deploy-related",
        event_metadata={}, created_at=datetime(2026, 5, 20, 10, tzinfo=timezone.utc),
    )
    evidence = MonitorInvestigationEntry(
        id="e2", investigation_id=inv.id, kind="issue", ref_id="group-9", body=None,
        event_metadata={}, created_at=datetime(2026, 5, 20, 11, tzinfo=timezone.utc),
    )
    fake_db.stub(execute=[
        FakeResult(rows=[product]),   # launched product lookup
        FakeResult(rows=[inv]),       # investigation lookup
        FakeResult(rows=[note, evidence]),  # timeline entries
    ])

    resp = await client.get(f"/portfolio/{product.id}/investigations/{inv.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["investigation"]["title"] == "Checkout broke"
    assert [e["kind"] for e in body["timeline"]] == ["note", "issue"]
    assert body["timeline"][1]["refId"] == "group-9"


async def test_add_note_requires_body(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    inv = _inv(product.id)
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[inv])])

    resp = await client.post(
        f"/portfolio/{product.id}/investigations/{inv.id}/entries",
        json={"kind": "note"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_add_evidence_entry(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    inv = _inv(product.id)
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[inv])])

    resp = await client.post(
        f"/portfolio/{product.id}/investigations/{inv.id}/entries",
        json={"kind": "problem", "ref_id": "prob-7", "body": "spike record"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["refId"] == "prob-7"
    assert any(isinstance(o, MonitorInvestigationEntry) for o in fake_db.added)


async def test_update_investigation_status(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    inv = _inv(product.id, status="open")
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[inv])])

    resp = await client.patch(
        f"/portfolio/{product.id}/investigations/{inv.id}",
        json={"status": "resolved"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"
    assert inv.status == "resolved"


async def test_investigation_404(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[])])
    resp = await client.get(f"/portfolio/{product.id}/investigations/nope", headers=auth_headers)
    assert resp.status_code == 404


async def test_create_and_export_report(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    fake_db.stub(execute=[FakeResult(rows=[product])])

    resp = await client.post(
        f"/portfolio/{product.id}/reports",
        json={"title": "Checkout incident", "body": "## Summary\nRolled back."},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Checkout incident"
    assert any(isinstance(o, MonitorReport) for o in fake_db.added)


async def test_export_report_markdown(client, fake_db, auth_headers):
    product = make_pipeline(launched_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
    report = MonitorReport(
        id="rep-1", pipeline_id=product.id, investigation_id=None,
        title="Checkout incident", body="Rolled back the deploy.",
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    fake_db.stub(execute=[FakeResult(rows=[product]), FakeResult(rows=[report])])

    resp = await client.get(f"/portfolio/{product.id}/reports/rep-1/export", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Checkout incident" in resp.text
    assert "Rolled back the deploy." in resp.text
