# Connect Status — what's wired vs. stubbed

_Last updated: 2026-06-19. This is the living map of the "Connect" surface across the
three repos (`ProblemFinderBackend`, `ProblemFinderWeb`, `ProblemFinderAI`). It records
what is actually wired end-to-end, what is stubbed, and what does not exist yet, with
file references so the next person doesn't have to re-discover it._

## TL;DR

Most of "Connect" is **already live**. The framing that it was mostly stubbed was wrong.
The real gaps are three:

1. **Team invite emails** — invites are recorded in the DB but no email is ever sent and
   there is no accept flow. There is **no email infrastructure in any repo**.
2. **Errors monitor source** — the in-house traffic/usage monitor is live, but the Errors
   source is a "Planned" placeholder.
3. **Revenue auto-refresh** — Stripe Connect revenue sync works, but only on a manual
   trigger; nothing refreshes it on a schedule.

Plan/tier **gating is intentionally OFF and staying off** for now.

## Status legend

- 🟢 **Live** — wired end-to-end and exercised.
- 🟡 **Partial** — works but with a notable limitation.
- 🔴 **Stubbed** — placeholder UI/record only, no real behavior.
- ⚪ **N/A** — intentionally not built.

## Inventory

| Capability | State | Evidence |
|---|---|---|
| Stripe subscriptions (checkout, portal, upgrade, webhooks) | 🟢 Live | backend `app/routes/subscriptions.py`; web `app/api/stripe/*` |
| Stripe revenue: Connect OAuth + real MRR/churn sync | 🟡 Live, manual-trigger only | backend `app/routes/portfolio.py` (`stripe` import L22, `last_synced_at` L113, `_revenue_source` L119, Connect redirect L175+); migrations `0019_portfolio_revenue.sql`, `0020_portfolio_stripe_connect.sql` |
| In-house traffic/usage monitor | 🟢 Live end-to-end | backend `portfolio_usage` + `/public/portfolio/events` + `/portfolio/{id}/usage`; migrations `0017_portfolio_usage.sql`, `0018_usage_source_domain.sql`; web `public/pf-usage.js` |
| Errors monitor source | 🔴 Stubbed ("Planned") | web `app/(core)/dashboard/(manage)/portfolio/PortfolioSetupDashboard.tsx` (`ErrorsSetupPanel` L125, `StatusBadge label="Planned"` L133); `public/pf-usage.js` has no error capture |
| Team invite emails | 🔴 DB record only, no send, no accept | backend `app/routes/teams.py` `add_member` L264-292 (creates `TeamMember` with `status="invited"`, sends nothing); web `app/(core)/dashboard/(manage)/manage/teams/page.tsx` ("Invite recorded. No email was sent." L242, "Email sending is not active yet." L587) |
| Email infrastructure (any transport) | 🔴 Does not exist | no `smtplib`/`aiosmtplib`/`EmailService`/`send_email` anywhere in `app/` or `requirements.txt` |
| Plan / tier gating | ⚪ Intentionally off | — |

## Notes & corrections to the original framing

- **The "in-house monitor Codex started" is not in `ProblemFinderAI`.** The AI repo is the
  scraping/ML pipeline. The monitor is the backend `portfolio_usage` system
  (`/public/portfolio/events` ingest + `/portfolio/{id}/usage` readout) with the
  first-party `public/pf-usage.js` beacon on the web side. Traffic/usage is essentially
  done; Errors is the only missing source.
- **`TeamMember` has no invite-token fields yet.** Current columns: `id, team_id, user_id,
  email, display_name, role, status, created_at, updated_at`
  (`app/models.py` L257-268). An accept flow needs `invite_token`,
  `invite_token_expires_at`, `invited_at`.
- **Latest migration is `0020`.** New migrations start at `0021`.

## Gaps → planned work

| # | Gap | Workstream |
|---|---|---|
| 1 | No email / no invite accept flow | In-house `EmailService` (SMTP relay, `EMAIL_TRANSPORT=console\|smtp`) + invite tokens + `GET /invites/{token}`, `POST /invites/{token}/accept`, resend; web accept page + copy |
| 2 | Errors source stubbed | Error event ingest reusing the usage `public_key`/domain whitelist; `pf-usage.js` `onerror`/`unhandledrejection` capture; real Errors panel |
| 3 | Revenue sync manual-only | In-process scheduler re-running the existing Connect sync; surface `lastSyncedAt` staleness |

## Email deliverability (operator setup, out of band)

Live SMTP send depends on the environment network policy allowing outbound SMTP and on
relay credentials being provided. With `EMAIL_TRANSPORT=console` (the default for dev/CI),
mail is logged/captured rather than sent, so development and tests stay green without
credentials. For real delivery from `useimmensity.com`, the domain needs **SPF**, **DKIM**,
and a **DMARC** record aligned with the chosen relay — a DNS task on the operator side.
