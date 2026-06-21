"""Live invoice-line inspector — confirms the parser recognizes the current
Stripe API line shape on a real account, without running a full sync.

Reads the platform's own account (first-party mode) via STRIPE_SECRET_KEY and
prints, per invoice line: the detected subscription id (top-level vs nested
parent), whether it's treated as a subscription line, its normalized MRR, and
the proration flag — plus the total billing-event count the engine would
persist. Acceptance check: that count must be > 0.

    export STRIPE_SECRET_KEY=sk_test_...
    python -m harness.inspect_live_invoice_lines
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("Set STRIPE_SECRET_KEY (sk_test_... for the test account) first.")
        return 1
    import stripe
    stripe.api_key = key

    # Import the real engine helpers (no offline stubs — we want the real SDK).
    from app.routes.portfolio import (
        _stripe_items, _stripe_value, _is_subscription_line, _line_subscription_id,
        _invoice_line_mrr_cents, _billing_events_from_invoices,
    )

    invoices = _stripe_items(stripe.Invoice.list(limit=100, expand=["data.lines"]))
    print(f"Fetched {len(invoices)} invoice(s) from the account.\n")

    total_lines = 0
    recognized = 0
    for invoice in invoices:
        lines = _stripe_value(_stripe_value(invoice, "lines", {}) or {}, "data", []) or []
        for line in lines:
            total_lines += 1
            top = _stripe_value(line, "subscription")
            nested = _line_subscription_id(line)
            is_sub = _is_subscription_line(line)
            recognized += 1 if is_sub else 0
            print(
                f"  line={_stripe_value(line, 'id')} "
                f"top_sub={top!r} resolved_sub={nested!r} "
                f"is_subscription={is_sub} "
                f"mrr_cents={_invoice_line_mrr_cents(line) if is_sub else '-'} "
                f"proration={bool(_stripe_value(line, 'proration', False))}"
            )

    events = _billing_events_from_invoices(invoices, "inspect")
    print(f"\nlines total={total_lines}  recognized as subscription={recognized}")
    print(f"billing events that would persist = {len(events)}")
    ok = len(events) > 0
    print("\nACCEPTANCE:", "PASS (billingEvents > 0)" if ok else "FAIL (still 0 — investigate the live line shape above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
