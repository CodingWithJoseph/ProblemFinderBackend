"""Tests for derived revenue metrics (PR2).

Pure arithmetic on the movement + customer-MRR rows. The scenario is hand-
computable so every ratio can be checked against its components.
"""

from datetime import date

from app.routes.portfolio import _revenue_metrics


# A 30-day window. Starting MRR (at window_start) = cus_a 1000 + cus_b 2000 = 3000.
# During the window: cus_b expands +500, cus_c is new +1500, cus_a churns -1000.
# Current MRR (now) = cus_b 2500 + cus_c 1500 = 4000.
WINDOW_START = date(2026, 5, 1)
NOW = date(2026, 5, 31)


def _scenario_rows():
    customer_rows = [
        # cus_a: live at 1000 at start, churns to 0 mid-window
        ("cus_a", date(2026, 4, 1), 1000, "usd"),
        ("cus_a", date(2026, 5, 20), 0, "usd"),
        # cus_b: live at 2000 at start, expands to 2500
        ("cus_b", date(2026, 4, 1), 2000, "usd"),
        ("cus_b", date(2026, 5, 10), 2500, "usd"),
        # cus_c: new in-window at 1500
        ("cus_c", date(2026, 5, 15), 1500, "usd"),
    ]
    movements = [
        ("cus_b", date(2026, 5, 10), "expansion", 500, "usd"),
        ("cus_c", date(2026, 5, 15), "new", 1500, "usd"),
        ("cus_a", date(2026, 5, 20), "churn", -1000, "usd"),
    ]
    return customer_rows, movements


def test_components_match_scenario():
    rows, movements = _scenario_rows()
    m = _revenue_metrics(rows, movements, WINDOW_START, NOW, "usd", {},
                         gross_margin=0.8, cac_cents=None, profit_margin=None,
                         default_margin=0.8, window_days=30)
    c = m["components"]
    assert c["startingMrrCents"] == 3000
    assert c["currentMrrCents"] == 4000
    assert c["newMrrCents"] == 1500
    assert c["expansionMrrCents"] == 500
    assert c["churnMrrCents"] == 1000  # magnitude, not signed
    assert c["contractionMrrCents"] == 0
    assert c["reactivationMrrCents"] == 0
    assert c["customersAtStart"] == 2   # cus_a + cus_b
    assert c["activeAccounts"] == 2     # cus_b + cus_c
    assert c["churnedCustomers"] == 1
    assert c["newCustomers"] == 1


def test_ratios_derive_from_components():
    rows, movements = _scenario_rows()
    r = _revenue_metrics(rows, movements, WINDOW_START, NOW, "usd", {},
                         gross_margin=0.8, cac_cents=None, profit_margin=None,
                         default_margin=0.8, window_days=30)["ratios"]
    # starting 3000; churn 1000, contraction 0, expansion 500, reactivation 0
    assert r["grossMrrChurn"] == round(1000 / 3000, 4)
    assert r["netMrrChurn"] == round((1000 - 500) / 3000, 4)
    assert r["nrr"] == round((3000 + 500 - 0 - 1000) / 3000, 4)
    assert r["grr"] == round((3000 - 0 - 1000) / 3000, 4)
    assert r["logoChurn"] == round(1 / 2, 4)
    assert r["quickRatio"] == round((1500 + 500) / (0 + 1000), 4)
    assert r["arpaCents"] == round(4000 / 2)  # 2000
    assert r["mrrGrowthRate"] == round((4000 - 3000) / 3000, 4)


def test_unit_economics_estimated_when_margin_defaulted():
    rows, movements = _scenario_rows()
    ue = _revenue_metrics(rows, movements, WINDOW_START, NOW, "usd", {},
                          gross_margin=None, cac_cents=None, profit_margin=None,
                          default_margin=0.8, window_days=30)["unitEconomics"]
    assert ue["grossMarginEstimated"] is True
    assert ue["grossMarginPct"] == 0.8
    # ARPA 2000c, margin 0.8 → monthly margin 1600c; gross churn = 1000/3000.
    # LTV = 1600 / (1000/3000) = 4800c, under the 60mo cap (1600*60=96000).
    assert ue["ltvCents"] == round(1600 / (1000 / 3000))
    assert ue["cacCents"] is None
    assert ue["cacPaybackMonths"] is None
    assert ue["ruleOf40"] is None
    assert ue["ruleOf40Estimated"] is True


def test_unit_economics_with_inputs():
    rows, movements = _scenario_rows()
    ue = _revenue_metrics(rows, movements, WINDOW_START, NOW, "usd", {},
                          gross_margin=0.75, cac_cents=10000, profit_margin=0.1,
                          default_margin=0.8, window_days=30)["unitEconomics"]
    assert ue["grossMarginEstimated"] is False
    assert ue["grossMarginPct"] == 0.75
    monthly_margin = round(2000 * 0.75)  # 1500c
    assert ue["cacPaybackMonths"] == round(10000 / monthly_margin, 2)
    assert ue["ltvCac"] == round(ue["ltvCents"] / 10000, 2)
    # Rule of 40 = (growth + profit) * 100; growth = 1000/3000.
    assert ue["ruleOf40"] == round((round(1000 / 3000, 4) + 0.1) * 100, 1)
    assert ue["ruleOf40Estimated"] is False


def test_ltv_caps_at_five_years_under_zero_churn():
    # No churn/contraction in window → gross churn 0 → LTV would diverge → cap.
    customer_rows = [
        ("cus_b", date(2026, 4, 1), 2000, "usd"),
        ("cus_b", date(2026, 5, 10), 2500, "usd"),
    ]
    movements = [("cus_b", date(2026, 5, 10), "expansion", 500, "usd")]
    ue = _revenue_metrics(customer_rows, movements, WINDOW_START, NOW, "usd", {},
                          gross_margin=0.8, cac_cents=None, profit_margin=None,
                          default_margin=0.8, window_days=30)["unitEconomics"]
    arpa = 2500  # only cus_b active now
    assert ue["ltvCents"] == round(arpa * 0.8) * 60  # capped at 60 months
