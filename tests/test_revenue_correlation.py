"""Tests for the usage↔revenue correlation engine (D1).

Pure 2×2 / odds-ratio math and the guardrails. Builds a labeled cohort big
enough to clear the sample floor so candidates are kept, and a degenerate one to
exercise the bail paths.
"""

from app.routes.portfolio import (
    _fisher_exact_two_sided,
    _laplace_odds_ratio,
    _revenue_correlation,
    _two_by_two,
    CORR_MIN_OUTCOME_PER_CLASS,
)


def test_two_by_two_and_odds_ratio():
    positives = {"p1", "p2", "p3"}
    negatives = {"n1", "n2"}
    behavior = {"p1", "p2", "n1"}
    a, b, c, d = _two_by_two(behavior, positives, negatives)
    assert (a, b, c, d) == (2, 1, 1, 1)
    # OR = ((2+1)(1+1)) / ((1+1)(1+1)) = 6/4 = 1.5
    assert _laplace_odds_ratio(a, b, c, d) == 1.5


def test_fisher_exact_runs_and_bounds():
    p = _fisher_exact_two_sided(8, 2, 2, 8)
    assert p is not None and 0 <= p <= 1


def _cohort(positive_with, positive_without, negative_with, negative_without, behavior="activation"):
    """Build labeled + behaviors maps with the given cell counts for one behavior."""
    labeled: dict[str, bool] = {}
    behaviors: dict[str, set] = {}
    i = 0
    def add(n, outcome, has_behavior):
        nonlocal i
        for _ in range(n):
            cid = f"cus_{i}"; i += 1
            labeled[cid] = outcome
            behaviors[cid] = {behavior} if has_behavior else {"pageview"}
    add(positive_with, True, True)
    add(positive_without, True, False)
    add(negative_with, False, True)
    add(negative_without, False, False)
    return labeled, behaviors


def test_strong_expansion_signal_is_kept_with_raw_2x2():
    # Activation strongly associates with positive outcome.
    labeled, behaviors = _cohort(positive_with=25, positive_without=5, negative_with=3, negative_without=25)
    result = _revenue_correlation(labeled, behaviors)
    activation = next((c for c in result["candidates"] if c["behavior"] == "activation"), None)
    assert activation is not None
    # raw 2x2 ships alongside the OR (the Emit requirement)
    assert {"a", "b", "c", "d", "oddsRatio"} <= set(activation)
    assert activation["a"] == 25 and activation["b"] == 3
    assert activation["direction"] == "expansion"
    assert activation["expandLikelihood"] > 1
    assert activation["pValue"] is not None


def test_neutral_signal_dropped_with_reason():
    # Behavior present/absent split evenly across outcomes → OR ~1.0 → dropped.
    labeled, behaviors = _cohort(positive_with=15, positive_without=15, negative_with=15, negative_without=15)
    result = _revenue_correlation(labeled, behaviors)
    assert all(c["behavior"] != "activation" for c in result["candidates"])
    dropped = next(d for d in result["dropped"] if d["behavior"] == "activation")
    assert "no signal" in dropped["reason"]
    assert {"a", "b", "c", "d"} <= set(dropped)


def test_too_few_people_dropped():
    # Only 2 people did the behavior → below CORR_MIN_BEHAVIOR_PERSONS.
    labeled, behaviors = _cohort(positive_with=2, positive_without=30, negative_with=0, negative_without=30)
    result = _revenue_correlation(labeled, behaviors)
    dropped = next(d for d in result["dropped"] if d["behavior"] == "activation")
    assert "too few people" in dropped["reason"]


def test_skewed_outcomes_bail():
    # Almost no negatives → can't compare; bail with a caveat, no candidates.
    labeled, behaviors = _cohort(positive_with=40, positive_without=10, negative_with=1, negative_without=0)
    result = _revenue_correlation(labeled, behaviors)
    assert result["candidates"] == []
    assert any(str(CORR_MIN_OUTCOME_PER_CLASS) in c for c in result["caveats"])


def test_small_cohort_gets_sample_caveat():
    labeled, behaviors = _cohort(positive_with=6, positive_without=4, negative_with=2, negative_without=6)
    result = _revenue_correlation(labeled, behaviors)
    assert any("practitioner floor" in c for c in result["caveats"])
