# Supabase Auth cutover

This is a full replacement with no existing users to migrate. Firebase authentication and Firestore are removed. The application PostgreSQL database remains in place. Its `user_id` values are the verified Supabase Auth UUID shared by web and Android.

## Configure the backend before deploying

Set these on the Railway API/MCP service:

```text
SUPABASE_URL=https://liqnbsdzzkrddxbhihzc.supabase.co
SUPABASE_PUBLISHABLE_KEY=<the project's publishable key>
SUPABASE_SECRET_KEY=<server-only secret key or supported service-role key>
SUPABASE_JWT_AUDIENCE=authenticated
SUPABASE_JWKS_CACHE_SECONDS=300
SUPABASE_AUTH_TIMEOUT_SECONDS=10
```

Keep `DATABASE_URL`, AI settings, worker settings, CORS and all Immensity MCP issuer/resource/client configuration. The secret key is required for account deletion and MCP `/userinfo`, not for local JWT signature verification. Remove old authentication service-account variables after successful cutover. No Auth user or old datastore is deleted by the SQL migration.

Railway predeploy runs `scripts/migrate.py`. Migration `0055_supabase_accounts.sql` adds `account_profiles` and `launch_waitlist`; `/ready` requires it. It does not rewrite or delete existing tables. Deploy the backend before the migrated web service. Returning to the old backend after web activation rejects Supabase tokens; rollback both services together and preserve the new tables.

## Shared project configuration

Keep email/password enabled and confirmation required. Public settings were read during development: email was enabled, auto-confirm was false, and project signups were enabled. URL allowlist, SMTP and email templates require a project administrator to inspect/configure.

Add these exact web destinations, preserving Android destinations:

```text
https://console.useimmensity.com/auth/callback
https://console.useimmensity.com/auth/callback?next=/reset-password
com.codingwithjoseph.immensity://auth
com.codingwithjoseph.immensity://auth/reset-password
```

Use a separate test project for localhost/staging callback URLs. Keep the existing Site URL and the email template's `ConfirmationURL` behavior initially. Web explicitly supplies its callback; Android signup currently omits redirectUrl and can rely on the Site URL. Do not replace its default or hardcode all email links to the web. If the Site URL needs to become the console origin, first change Android signup to explicitly supply `com.codingwithjoseph.immensity://auth` and test that change on a device. Recovery is already explicit in Android.

Web SSR uses PKCE. Email links must be completed in the initiating browser; otherwise the error screen offers a safe restart. Never transfer a verifier or session token between devices. No production emails are needed to apply schema/configuration; use a dedicated controlled test account for the final real-project smoke test.

## Deletion

Deletion requires a recent actual authentication event and live Supabase user validation. A refreshed JWT's `iat` is insufficient. It commits a durable account fence and MCP revocation before calling Auth administration. Existing JWTs and MCP operations cannot bypass the fence. Product requests and MCP transactions hold account locks so deletion cannot race new writes. Product rows/children are removed and a minimal UUID/timestamps tombstone remains. A background worker retries pending cleanup every 30 seconds; failure is logged without credentials. Provider/storage deletion errors remain pending, not falsely successful.

The application does not use Supabase Storage. If storage is introduced later, user-owned objects must also be included in deletion before Auth removal.

## Verification

Run `python -m pytest -q` for offline auth/domain tests. Set `TEST_DATABASE_URL` to an isolated local `auth_migration` database with pgvector to also run real schema/profile/onboarding/waitlist/deletion tests. Tests refuse a non-local or differently named integration database; schemas are created with unique names and dropped afterward. CI runs those tests in a PostgreSQL service.

After cutover, check `/ready`, unauthenticated REST 401 responses, wrong-project-token rejection, web/Android UUID agreement, profile/onboarding persistence, and MCP consent/refresh/UserInfo. Verify deletion only with an explicitly disposable test account.
