# Pointing local dev at the prod data (read-only)

Goal: run the app locally but read **real Immensity data** instead of mock/demo or a
dev project — without any risk of mutating production.

The monitoring metrics (usage, errors, revenue, sessions) live in **Postgres**, not
Firestore. Firestore is only used to **verify the Firebase ID token**. So pointing at
prod means two things must line up:

1. **Postgres** — `DATABASE_URL` points at the prod database, via a **read-only role**.
2. **Firebase** — the client SDK, the web app's admin SDK, and the backend's service
   account all use the **same** Immensity project (or every `/api/portfolio` call 401s).

## Why read-only matters

A local backend does not only write when you click things. On boot, `main.py`'s
`lifespan` starts a scheduler that, by default, runs against `DATABASE_URL`:

- revenue sync every 6h → calls Stripe and **writes** revenue snapshots
- alert checks + digests every 1h → can **email** real product owners

A read-only Postgres role neutralizes all of it: schedulers and UI write actions
(sync revenue, resolve issue, create investigation/report) fail with a permission
error instead of touching prod. You can browse every screen safely.

## 1. Create a read-only role in the prod database

Run once, as an admin, against the prod DB:

```sql
CREATE ROLE immensity_readonly LOGIN PASSWORD '<choose-a-strong-password>';
GRANT CONNECT ON DATABASE <dbname> TO immensity_readonly;
GRANT USAGE ON SCHEMA public TO immensity_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO immensity_readonly;
-- future tables stay readable too:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO immensity_readonly;
```

Then build the async URL (Supabase: use the same host, swap in this role):

```
postgresql+asyncpg://immensity_readonly:<password>@<host>:5432/<dbname>
```

## 2. Backend — `ProblemFinderBackend/.env`

```bash
ENVIRONMENT=development

# Prod data, read-only role — writes fail harmlessly.
DATABASE_URL=postgresql+asyncpg://immensity_readonly:<password>@<host>:5432/<dbname>

# Immensity Firebase (token verification). The service account's own project_id
# is authoritative (app/auth.py), so just drop the Immensity SA JSON here:
FIREBASE_PROJECT_ID=<immensity-project-id>
FIREBASE_CREDENTIALS_PATH=firebase-service-account-prod.json
# (or inline:)  FIREBASE_CREDENTIALS_JSON='{...immensity service account...}'

# Optional but tidy: silence the schedulers so they don't log a failed write
# every cycle (the read-only role already blocks the write).
REVENUE_SYNC_INTERVAL_HOURS=0
ALERT_CHECK_INTERVAL_HOURS=0
EMAIL_TRANSPORT=console
```

Do **not** run Alembic migrations against the prod URL from dev.

## 3. Frontend — `ProblemFinderWeb/.env.local`

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000   # wherever the backend runs

# Immensity web app config (Project settings → General → Your apps → Web)
NEXT_PUBLIC_FIREBASE_API_KEY=
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=<immensity>.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=<immensity-project-id>
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=<immensity>.appspot.com
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=
NEXT_PUBLIC_FIREBASE_APP_ID=

# Immensity admin SDK — the Next.js /api routes verify the same token.
FIREBASE_ADMIN_PROJECT_ID=<immensity-project-id>
FIREBASE_ADMIN_CLIENT_EMAIL=firebase-adminsdk-xxxxx@<immensity>.iam.gserviceaccount.com
FIREBASE_ADMIN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

All three Firebase credential sets — frontend client, frontend admin, backend SA —
must be the same Immensity project.

## 4. Run

```bash
# backend
uvicorn main:app --reload --port 8000
# frontend
npm run dev
```

Log in with a real Immensity account and the dashboard renders that account's real
data. (Demo mode — `?demo=1` — remains available for backend-free clicking.)

## Expected behavior

- Read paths: real data on every screen.
- Write actions (sync revenue, resolve issue, create investigation/report): fail with
  a permission/500 error. That's the read-only guard doing its job, not a bug.
