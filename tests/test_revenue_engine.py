"""Unit tests for the invoice/event-derived MRR engine (PR1).

These exercise the pure computation (Stripe-shaped dicts in, plain rows out) so
no DB or live Stripe is needed. The final test prints the new-engine total next
to the legacy subscription snapshot — the validation the PR asks for — and
asserts they differ for the reasons the PR documents (prorations/discounts/
history), without asserting exact equality.
"""

from datetime import datetime, timezone

from app.routes.portfolio import (
    _billing_events_from_invoices,
    _combine_currency_totals,
    _customer_mrr_series,
    _invoice_line_mrr_cents,
    _is_subscription_line,
    _line_subscription_id,
    _line_subscription_item_id,
    _movements_from_series,
    _normalize_interval_cents,
    _subscription_mrr_cents,
    _total_mrr_at,
    _active_subs_by_customer,
)


def _ts(year, month, day=1) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _line(line_id, sub, unit_amount, *, interval="month", interval_count=1, qty=1,
          period_start=None, proration=False, discounts=None, currency="usd",
          recurring=True, type_="subscription"):
    price = {"unit_amount": unit_amount}
    price["recurring"] = {"interval": interval, "interval_count": interval_count} if recurring else None
    return {
        "id": line_id,
        "subscription": sub,
        "type": type_,
        "quantity": qty,
        "proration": proration,
        "currency": currency,
        "period": {"start": period_start if period_start is not None else _ts(2026, 1)},
        "discount_amounts": discounts or [],
        "amount": unit_amount * qty,
        "price": price,
    }


def _invoice(invoice_id, customer, lines, currency="usd"):
    return {"id": invoice_id, "customer": customer, "currency": currency, "lines": {"data": lines}}


def _active_sub(customer, unit_amount, *, interval="month", currency="usd"):
    return {
        "customer": customer,
        "currency": currency,
        "items": {"data": [{
            "quantity": 1,
            "price": {"unit_amount": unit_amount, "recurring": {"interval": interval, "interval_count": 1}},
        }]},
    }


# ── normalization ─────────────────────────────────────────────────────────────

def test_normalize_interval_matches_subscription_helper():
    assert _normalize_interval_cents(1200, "month") == 1200
    assert _normalize_interval_cents(12000, "year") == 1000
    assert _normalize_interval_cents(1000, "week") == round(1000 * 52 / 12)
    assert _normalize_interval_cents(100, "day") == round(100 * 365 / 12)


def test_invoice_line_mrr_normalizes_annual():
    line = _line("li_1", "sub_1", 12000, interval="year")
    assert _invoice_line_mrr_cents(line) == 1000


def test_invoice_line_mrr_subtracts_discounts():
    # $20/mo with a $5 coupon → $15 MRR.
    line = _line("li_2", "sub_1", 2000, discounts=[{"amount": 500}])
    assert _invoice_line_mrr_cents(line) == 1500


def test_trial_line_contributes_zero():
    line = _line("li_trial", "sub_1", 0)
    assert _invoice_line_mrr_cents(line) == 0


# ── ledger ingestion ──────────────────────────────────────────────────────────

def test_billing_events_exclude_non_subscription_lines():
    setup_fee = _line("li_setup", None, 5000, recurring=False, type_="invoiceitem")
    sub_line = _line("li_sub", "sub_1", 1000)
    invoice = _invoice("in_1", "cus_1", [setup_fee, sub_line])

    events = _billing_events_from_invoices([invoice], "src_1")

    assert [e["stripe_line_item_id"] for e in events] == ["li_sub"]
    assert events[0]["mrr_cents"] == 1000
    assert events[0]["stripe_customer_id"] == "cus_1"


def test_billing_events_flag_prorations():
    proration = _line("li_pror", "sub_1", 700, proration=True)
    full = _line("li_full", "sub_1", 2000)
    events = _billing_events_from_invoices([_invoice("in_1", "cus_1", [proration, full])], "src_1")
    by_id = {e["stripe_line_item_id"]: e for e in events}
    assert by_id["li_pror"]["is_proration"] is True
    assert by_id["li_full"]["is_proration"] is False


# ── per-customer series + prorations ──────────────────────────────────────────

def test_series_uses_full_line_not_proration_delta():
    # Mid-cycle upgrade: a proration line plus the new plan's full-period line.
    # The recurring level must follow the full line ($30), not the proration.
    proration = _line("li_pror", "sub_1", 700, proration=True, period_start=_ts(2026, 2, 15))
    full = _line("li_full", "sub_1", 3000, period_start=_ts(2026, 2, 15))
    first = _line("li_first", "sub_1", 1000, period_start=_ts(2026, 1, 1))
    events = _billing_events_from_invoices([
        _invoice("in_1", "cus_1", [first]),
        _invoice("in_2", "cus_1", [proration, full]),
    ], "src_1")

    # No active sub passed → series is pure history.
    series = _customer_mrr_series(events, active_by_customer={}, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    levels = {when: mrr for (_c, when, mrr, _cur) in series}
    from datetime import date
    assert levels[date(2026, 1, 1)] == 1000
    assert levels[date(2026, 2, 15)] == 3000  # full line, not 1000+700


def test_movements_cover_all_five_types():
    cust = "cus_1"
    # new (0->1000), expansion (1000->2000), contraction (2000->1500),
    # churn (1500->0), reactivation (0->1200). The customer reactivated and is
    # still live, so we pass an active sub to carry the $12 level forward (no
    # trailing churn at the as_of point).
    history = [
        _invoice("in_1", cust, [_line("l1", "sub_1", 1000, period_start=_ts(2026, 1, 1))]),
        _invoice("in_2", cust, [_line("l2", "sub_1", 2000, period_start=_ts(2026, 2, 1))]),
        _invoice("in_3", cust, [_line("l3", "sub_1", 1500, period_start=_ts(2026, 3, 1))]),
        _invoice("in_4", cust, [_line("l4", "sub_1", 0, period_start=_ts(2026, 4, 1))]),
        _invoice("in_5", cust, [_line("l5", "sub_2", 1200, period_start=_ts(2026, 5, 1))]),
    ]
    events = _billing_events_from_invoices(history, "src_1")
    active = _active_subs_by_customer([_active_sub(cust, 1200)], "usd")
    series = _customer_mrr_series(events, active_by_customer=active, as_of=datetime(2026, 6, 1, tzinfo=timezone.utc))
    movements = _movements_from_series(series)
    types = [m["movement_type"] for m in sorted(movements, key=lambda m: m["effective_date"])]
    assert types == ["new", "expansion", "contraction", "churn", "reactivation"]


def test_no_active_sub_now_surfaces_churn():
    events = _billing_events_from_invoices([
        _invoice("in_1", "cus_1", [_line("l1", "sub_1", 1000, period_start=_ts(2026, 1, 1))]),
    ], "src_1")
    # cus_1 has history but is absent from active subs → churned now.
    series = _customer_mrr_series(events, active_by_customer={}, as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    movements = _movements_from_series(series)
    assert [m["movement_type"] for m in movements] == ["new", "churn"]


def test_active_sub_carries_invoiced_level_forward():
    from datetime import date
    events = _billing_events_from_invoices([
        _invoice("in_1", "cus_1", [_line("l1", "sub_1", 1000, period_start=_ts(2026, 1, 1))]),
    ], "src_1")
    # Active sub reports $20 list price, but the customer was invoiced $10 (e.g.
    # a coupon). Still-active → carry the invoiced $10 forward, no spurious
    # expansion, and the list price does not override it.
    active = _active_subs_by_customer([_active_sub("cus_1", 2000)], "usd")
    series = _customer_mrr_series(events, active_by_customer=active, as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    movements = _movements_from_series(series)
    assert [m["movement_type"] for m in movements] == ["new"]
    as_of_level = next(mrr for (_c, when, mrr, _cur) in series if when == date(2026, 2, 1))
    assert as_of_level == 1000


def test_active_sub_without_history_uses_sub_mrr():
    # Brand-new subscriber with no invoice in the window yet: fall back to the
    # subscription's own MRR so they still appear as a "new" customer.
    active = _active_subs_by_customer([_active_sub("cus_new", 2500)], "usd")
    series = _customer_mrr_series([], active_by_customer=active, as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    movements = _movements_from_series(series)
    assert [m["movement_type"] for m in movements] == ["new"]
    assert series[0][2] == 2500


# ── currency handling ─────────────────────────────────────────────────────────

def test_multi_currency_total_warns_on_missing_rate():
    by_currency = {"usd": 1000, "eur": 500}
    total, warnings = _combine_currency_totals(by_currency, "usd", {})
    assert total == 1000  # eur excluded, never silently mixed
    assert warnings and "eur" in warnings[0]


def test_multi_currency_total_applies_configured_rate():
    by_currency = {"usd": 1000, "eur": 500}
    total, warnings = _combine_currency_totals(by_currency, "usd", {"eur": 1.1})
    assert total == 1000 + round(500 * 1.1)
    assert warnings == []


def test_total_mrr_at_sums_latest_per_customer():
    events = _billing_events_from_invoices([
        _invoice("in_a", "cus_a", [_line("la", "sub_a", 1000, period_start=_ts(2026, 1, 1))]),
        _invoice("in_b", "cus_b", [_line("lb", "sub_b", 2000, period_start=_ts(2026, 1, 1))]),
    ], "src_1")
    series = _customer_mrr_series(events, active_by_customer={}, as_of=datetime(2026, 1, 15, tzinfo=timezone.utc))
    from datetime import date
    # As of the invoice date, both customers are live at their billed level. (As
    # of "now" they'd both churn since no active sub is passed.)
    total, by_currency, warnings = _total_mrr_at(series, date(2026, 1, 1), "usd", {})
    assert total == 3000
    assert by_currency == {"usd": 3000}
    assert warnings == []


# ── validation: new engine vs legacy snapshot ─────────────────────────────────

def test_engine_comparison_print(capsys):
    """Prints the new-engine MRR next to the legacy subscription snapshot for a
    small connected-account fixture, and asserts they differ for the documented
    reasons rather than asserting exact equality."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    # Two customers. cus_1 upgraded mid-cycle (proration noise the old path
    # ignores) and currently pays $30/mo. cus_2 used a coupon: $20 list, $5 off.
    invoices = [
        _invoice("in_1", "cus_1", [_line("l1", "sub_1", 1000, period_start=_ts(2026, 1, 1))]),
        _invoice("in_2", "cus_1", [
            _line("l2p", "sub_1", 700, proration=True, period_start=_ts(2026, 3, 15)),
            _line("l2", "sub_1", 3000, period_start=_ts(2026, 3, 15)),
        ]),
        _invoice("in_3", "cus_2", [_line("l3", "sub_2", 2000, discounts=[{"amount": 500}], period_start=_ts(2026, 5, 1))]),
    ]
    active_subs = [_active_sub("cus_1", 3000), _active_sub("cus_2", 2000)]  # Stripe list, no coupon applied

    events = _billing_events_from_invoices(invoices, "src_1")
    active_by_customer = _active_subs_by_customer(active_subs, "usd")
    series = _customer_mrr_series(events, active_by_customer, as_of=now)
    invoice_total, by_currency, warnings = _total_mrr_at(series, now.date(), "usd", {})

    # Legacy engine: naive sum of active subscriptions (no discount awareness).
    subscription_total = sum(_subscription_mrr_cents(s) for s in active_subs)

    print(f"\n[revenue-engine validation]")
    print(f"  legacy subscription snapshot MRR: {subscription_total} cents")
    print(f"  new invoice-engine MRR:           {invoice_total} cents (by currency: {by_currency})")
    print(f"  delta:                            {invoice_total - subscription_total} cents")
    print(f"  warnings:                         {warnings}")
    print("  expected: new < legacy here because the invoice engine honors cus_2's "
          "$5 coupon while the subscription sum bills list price.")

    # cus_1 = 3000, cus_2 = 1500 (after coupon) → 4500 new vs 5000 legacy.
    assert subscription_total == 5000
    assert invoice_total == 4500
    assert invoice_total != subscription_total
    captured = capsys.readouterr()
    assert "new invoice-engine MRR" in captured.out


# ── current Stripe API line shape (regression: live integration bug) ──────────
#
# The 2024+ Invoice line item moved the subscription association off the line
# itself onto line.parent.subscription_item_details.subscription, and dropped the
# top-level `subscription` / `type` fields. The old parser rejected every such
# line, so nothing was persisted and the engine fell back to the active-sub
# point (billingEvents=0). These fixtures match the live shape.


def _live_line(line_id, sub_id, unit_amount, *, sub_item="si_live", period_start=None,
               proration=False, interval="month", currency="usd"):
    """An invoice line as the current Stripe API returns it: no top-level
    `subscription`/`type`, association nested under `parent`."""
    return {
        "id": line_id,
        "object": "line_item",
        "parent": {
            "type": "subscription_item_details",
            "subscription_item_details": {"subscription": sub_id, "subscription_item": sub_item},
        },
        "quantity": 1,
        "proration": proration,
        "currency": currency,
        "period": {"start": period_start if period_start is not None else _ts(2026, 1)},
        "discount_amounts": [],
        "amount": unit_amount,
        "price": {"unit_amount": unit_amount, "recurring": {"interval": interval, "interval_count": 1}},
    }


def _live_invoice_item_line(line_id, unit_amount, period_start=None):
    """A one-off invoice item (setup fee) in the current shape: parent is
    invoice_item_details, no recurring price → must be excluded."""
    return {
        "id": line_id,
        "object": "line_item",
        "parent": {"type": "invoice_item_details", "invoice_item_details": {"invoice_item": "ii_1"}},
        "quantity": 1,
        "proration": False,
        "currency": "usd",
        "period": {"start": period_start if period_start is not None else _ts(2026, 1)},
        "discount_amounts": [],
        "amount": unit_amount,
        "price": {"unit_amount": unit_amount, "recurring": None},
    }


def test_line_subscription_id_reads_nested_parent():
    line = _live_line("il_live", "sub_live", 2900)
    assert _line_subscription_id(line) == "sub_live"
    assert _line_subscription_item_id(line) == "si_live"
    assert _is_subscription_line(line) is True


def test_live_invoice_lines_persist_as_billing_events():
    # Regression for the live bug: 1 active sub at $29, invoice line in the new
    # shape. Must yield a billing event (not 0) with the nested subscription id.
    invoices = [_invoice("in_live", "cus_live", [_live_line("il_live", "sub_live", 2900)])]
    events = _billing_events_from_invoices(invoices, "src_live")
    assert len(events) == 1
    event = events[0]
    assert event["stripe_subscription_id"] == "sub_live"
    assert event["stripe_customer_id"] == "cus_live"
    assert event["mrr_cents"] == 2900
    assert event["is_proration"] is False


def test_live_setup_fee_still_excluded():
    invoices = [_invoice("in_live", "cus_live", [
        _live_invoice_item_line("il_fee", 5000),
        _live_line("il_sub", "sub_live", 2900),
    ])]
    events = _billing_events_from_invoices(invoices, "src_live")
    assert [e["stripe_line_item_id"] for e in events] == ["il_sub"]


def test_live_proration_flagged_and_excluded_from_level():
    invoices = [_invoice("in_live", "cus_live", [
        _live_line("il_pror", "sub_live", 700, proration=True, period_start=_ts(2026, 3)),
        _live_line("il_full", "sub_live", 5000, period_start=_ts(2026, 3)),
    ])]
    events = _billing_events_from_invoices(invoices, "src_live")
    by_id = {e["stripe_line_item_id"]: e for e in events}
    assert by_id["il_pror"]["is_proration"] is True
    assert by_id["il_full"]["is_proration"] is False
    # Level follows the full line, not the proration.
    series = _customer_mrr_series(events, {}, as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
    from datetime import date
    assert {when: mrr for (_c, when, mrr, _cur) in series}[date(2026, 3, 1)] == 5000


def test_live_invoice_full_pipeline_derives_movement_from_events():
    # End to end: invoice line (new shape) + active sub → billing event persists,
    # series + movement derived from the EVENT, not just the active-sub fallback.
    invoices = [_invoice("in_live", "cus_live", [_live_line("il_live", "sub_live", 2900)])]
    events = _billing_events_from_invoices(invoices, "src_live")
    assert len(events) == 1  # billingEvents > 0
    active = _active_subs_by_customer([_active_sub("cus_live", 2900)], "usd")
    series = _customer_mrr_series(events, active, as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    movements = _movements_from_series(series)
    assert [m["movement_type"] for m in movements] == ["new"]
    # The 'new' movement is dated at the invoice period start (from the event),
    # not only at the as_of fallback point.
    from datetime import date
    assert movements[0]["effective_date"] == date(2026, 1, 1)
