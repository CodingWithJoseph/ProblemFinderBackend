# Monitor Feature Split Plan

## Product Boundary

Portfolio is the launched-product workspace: it lists products, opens a product shell, and shows account/project goals.

Monitor is the observability product inside a launched product. It owns ingestion, setup, health, sessions, traces, feature flows, errors, logs, investigations, reports, alerts, and the future war-room experience.

Revenue remains adjacent to Monitor because it is a business signal used for impact and correlation, but it should not define Monitor's core ownership.

## Phase 1: PR 379, Route Ownership

Goal: make Monitor a first-class backend feature without breaking the current frontend.

- Add `app.routes.monitor` with its own private and public routers.
- Move Monitor-owned endpoints out of `app.routes.portfolio`.
- Preserve existing paths for compatibility:
  - `/portfolio/{pipeline_id}/...` for authenticated monitor reads/actions.
  - `/public/portfolio/...` for current beacon ingestion.
- Keep Portfolio focused on product list, product shell, goals, admin settings, and revenue setup/sync.
- Update tests to import Monitor helpers from `app.routes.monitor`.

This phase intentionally keeps URL compatibility. API path migration should happen only after the frontend and SDK/setup copy have stable aliases.

## Phase 2: Shared Services

Goal: shrink route files and remove remaining helper coupling.

- Move launched-product authorization into a product access service.
- Move usage-source lookup, origin checks, rate limiting, serializers, and ingestion builders into Monitor services.
- Move issue grouping, trace assembly, flow graphing, vitals scoring, and correlation into smaller service modules.
- Keep route files thin: request model, authorization, service call, response shape.

PR 379 starts this split with `app.services.monitoring`: common utilities, source/access helpers, serializers, analytics helpers, and ingest row builders now live outside the route module. The remaining route-local helpers should become smaller workflow services next.

## Phase 3: API Boundary

Goal: make Monitor explicit to clients while keeping old installs alive.

- Add canonical `/monitor/{pipeline_id}/...` and `/public/monitor/...` route aliases.
- Keep `/portfolio/...` aliases during a migration window.
- Update frontend API clients to call Monitor paths.
- Update setup snippets and backend tracing helpers to post to Monitor paths.
- Add compatibility tests for both route families.

PR 379 starts this by mounting the same Monitor routers under both the legacy Portfolio prefixes and the canonical Monitor prefixes. Frontend and SDK migration can now happen without breaking existing installs.

## Phase 4: Data Model Naming

Goal: align storage language with product ownership.

- Leave physical table renames until route/service split is stable.
- Introduce model aliases or new table names only with migrations and read/write compatibility.
- Prefer `monitor_*` naming for new data structures.
- Keep historical `portfolio_*` tables readable until migrations and dashboards are verified.

PR 379 starts this phase with a Monitor schema map: each Monitor-owned model now references a logical table definition with today's physical table name and the future canonical `monitor_*` name. This keeps the deployed database stable while giving migrations, docs, and new features a single source of truth for Monitor storage ownership.

## Phase 5: War-Room UX

Goal: shift from pages that show data to pages that drive investigations.

- Command Center opens a focused investigation context, not a generic list.
- Issue, session, trace, feature, release, and problem surfaces can add evidence to an investigation with one action.
- Session timelines include events, errors, logs, spans, vitals, releases, and feature steps.
- Feature health includes runs, error rate, p75 duration, affected users, and drop-off.
- Investigation reports render resolved evidence, not manual IDs.

PR 379 starts this phase with a canonical evidence-to-investigation endpoint. Monitor clients can open a war-room investigation from an issue, problem, session, trace, feature, release, log, or external link in one request, and the first timeline evidence item is created with the investigation.

## Phase 6: SDK Coverage

Goal: make Monitor seamless beyond websites.

- Stabilize the browser setup path first.
- Add backend helpers for common server frameworks.
- Add mobile contracts for iOS and Android using the same trace/session/release/platform model.
- Expand ingestion auth so server and native clients can report safely without depending on browser origin headers.

PR 379 starts this phase by moving the backend tracing helper to the canonical `/api/public/monitor/batch` ingest path while preserving the legacy public Portfolio alias for existing installs.
