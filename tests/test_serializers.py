"""Unit tests for route serializers + helpers (no DB).

Build transient ORM instances and assert the new Section 2/5/7 fields are
exposed in the camelCase API payloads.
"""
import uuid
from datetime import date

from app.models import (
    Opportunity, Cluster, Pipeline, Problem,
    RevenueSignal, Experiment, ExperimentResult, DomainSnapshot,
)


def test_opportunity_serialize_keeps_non_scoring_fields():
    from app.routes.opportunities import _serialize
    o = Opportunity(
        title="t", revenue_potential_score=0.7, primary_problem="invoices",
        needs_review=True,
    )
    d = _serialize(o)
    assert d["revenuePotentialScore"] == 0.7
    assert d["primaryProblem"] == "invoices"
    assert d["needsReview"] is True
    # Stale scoring fields are no longer exposed (JOB 3).
    for removed in (
        "painScore", "urgencyScore", "commercialIntentScore", "solutionSeekingScore",
        "signalScore", "signalStrength", "signalReason", "industryCategory",
        "confidenceScore", "wtpSignal", "intensity", "sentiment", "domain",
    ):
        assert removed not in d


def test_cluster_serialize_drops_scoring_fields():
    from app.routes.clusters import _serialize_cluster
    c = Cluster(id=1, name="c", status="growing", primary_domain="Software",
                growth_acceleration=0.1, revenue_potential_score=0.7,
                competition_score=0.2, saturation_score=0.3)
    d = _serialize_cluster(c, post_count=5, signals={})
    assert d["status"] == "growing"
    assert d["primaryDomain"] == "Software"
    assert d["growthAcceleration"] == 0.1
    assert d["revenuePotentialScore"] == 0.7
    for removed in ("signalScore", "avgPainScore", "avgUrgencyScore", "confidenceScore"):
        assert removed not in d


def test_pipeline_serialize_outcome_mrr_and_signal_post():
    from app.routes.pipeline import _serialize, _serialize_signal_post
    p = Pipeline(user_id="u", name="n", outcome="success", mrr=1200.0)
    d = _serialize(p)
    assert d["outcome"] == "success"
    assert d["mrr"] == 1200.0
    assert "outcomeNotedAt" in d

    o = Opportunity(title="t", source_score=42.0, num_comments=5)
    sd = _serialize_signal_post(o)
    assert sd["sourceScore"] == 42.0
    assert sd["numComments"] == 5
    for removed in ("painScore", "urgencyScore", "signalStrength", "industryCategory"):
        assert removed not in sd


def test_problem_serialize_and_helpers():
    from app.routes.problems import _serialize, _is_uuid
    p = Problem(pipeline_id="p", user_id="u", title="t", pain_level="high",
                urgency_level="low", buyer_type="prosumer")
    d = _serialize(p)
    assert d["painLevel"] == "high"
    assert d["urgencyLevel"] == "low"
    assert d["buyerType"] == "prosumer"
    # Removed scoring fields no longer serialized (JOB 3).
    assert "willingnessToPaySignal" not in d
    assert "solutionSeekingScore" not in d

    assert _is_uuid(str(uuid.uuid4())) is True
    assert _is_uuid("not-a-uuid") is False


def test_revenue_signal_serialize():
    from app.routes.revenue_signals import _serialize
    r = RevenueSignal(user_id="u", domain="Finance", price_point=29,
                      revenue_amount=2900, customer_count=100, conversion_count=3)
    d = _serialize(r)
    assert d["domain"] == "Finance"
    assert d["pricePoint"] == 29.0
    assert d["revenueAmount"] == 2900.0
    assert d["customerCount"] == 100


def test_experiment_serialize_with_results():
    from app.routes.experiments import _serialize, _serialize_result
    e = Experiment(user_id="u", experiment_name="landing page", status="running")
    res = ExperimentResult(experiment_id="e", impressions=1000, clicks=50,
                           purchases=3, revenue_generated=297)
    d = _serialize(e, [res])
    assert d["experimentName"] == "landing page"
    assert d["status"] == "running"
    assert len(d["results"]) == 1
    assert d["results"][0]["impressions"] == 1000
    assert d["results"][0]["revenueGenerated"] == 297.0


def test_domain_snapshot_serialize():
    from app.routes.domain_snapshots import _serialize
    s = DomainSnapshot(domain="Software", snapshot_date=date(2026, 5, 1),
                       opportunity_count=42, avg_signal_score=55.5,
                       total_reported_revenue=10000)
    d = _serialize(s)
    assert d["domain"] == "Software"
    assert d["snapshotDate"] == "2026-05-01"
    assert d["opportunityCount"] == 42
    assert d["avgSignalScore"] == 55.5
    assert d["totalReportedRevenue"] == 10000.0
