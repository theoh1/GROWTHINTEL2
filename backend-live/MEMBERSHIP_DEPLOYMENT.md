# Membership production deployment

The downloaded backend already uses SQLAlchemy and supports PostgreSQL through
`DATABASE_URL`. Membership tables are created on that same engine. The archived
application contains no existing end-user identity/session model, so membership
accounts are the Growth Intel server identity for this flow; they are not linked
to a second repository authentication system.

## Required production environment

- `DATABASE_URL`: a reachable, persistent PostgreSQL URL. Membership startup
  deliberately fails if the backend falls back to SQLite.
- `BANK_TRANSFER_ACCOUNT_NAME`
- `BANK_TRANSFER_SORT_CODE`
- `BANK_TRANSFER_ACCOUNT_NUMBER`
- `MEMBERSHIP_ADMIN_EMAILS`: comma-separated, normalized account emails.
- `MEMBERSHIP_ALLOWED_ORIGINS`: comma-separated exact HTTPS frontend origins,
  without paths. Include every production/preview origin that may submit forms.

`MEMBERSHIP_ALLOW_SQLITE=true` is an explicit local/test-only switch. Never set
it on a production backend or an ephemeral host. No real bank details, database
credentials, or administrator passwords belong in source control.

## Which service owns each setting

The repository deploys the static site on Vercel and forwards `/api/v1/*` to a
separate FastAPI service. Set all membership variables on that **FastAPI backend
service**, not on Vercel:

| Variable | Service | Notes |
| --- | --- | --- |
| `DATABASE_URL` | FastAPI backend | Existing application database; use one persistent PostgreSQL URL. |
| `BANK_TRANSFER_ACCOUNT_NAME` | FastAPI backend | Secret deployment configuration; never source code. |
| `BANK_TRANSFER_SORT_CODE` | FastAPI backend | Secret deployment configuration; never source code. |
| `BANK_TRANSFER_ACCOUNT_NUMBER` | FastAPI backend | Secret deployment configuration; never source code. |
| `MEMBERSHIP_ADMIN_EMAILS` | FastAPI backend | Comma-separated normalized membership-account emails. |
| `MEMBERSHIP_ALLOWED_ORIGINS` | FastAPI backend | Include `https://growthintel.vercel.app`; add only intentional preview/custom domains. |

No membership environment variables are required in the Vercel static frontend.
Do not expose bank or database values with a `NEXT_PUBLIC_` prefix.

The repository contains no Render/Railway/Fly deployment manifest and no evidence
of a provisioned PostgreSQL provider. Configure `DATABASE_URL` in the environment
settings of whichever service runs `backend-live/app/main.py`. The checked-in
`vercel.json` currently forwards API traffic to the separate HTTPS backend shown
there; verify that destination is a stable production service before launch.

## Initial schema and staging verification

On startup, SQLAlchemy `MetaData.create_all()` creates only missing membership
tables and indexes on the existing engine. It does not drop tables or alter
existing application tables. The PostgreSQL role therefore needs `CONNECT`,
`USAGE` and initial `CREATE` privileges. After the first successful deployment,
verify that these objects exist:

- `membership_users`
- `membership_sessions`
- `membership_payment_requests`
- `membership_audit_logs`
- `uq_membership_pending`
- `ix_membership_payment_user_created`

`create_all()` is safe for this initial additive schema, but it is not a general
migration system. Any future column or constraint change must use the backend's
chosen migration tooling rather than relying on `create_all()`.

The frontend continues to send `/api/v1/*` through the existing Vercel rewrite.
Because authentication uses a `Secure`, `HttpOnly`, `SameSite=Strict` cookie,
the public frontend and API must be served in a same-site deployment. If the
backend host is cross-site rather than reached through the frontend rewrite,
cookie delivery must be re-designed before launch rather than weakening these
settings.

For the current Vercel rewrite, the browser requests
`https://growthintel.vercel.app/api/v1/...`; the host-only `Secure` cookie is
therefore stored for the Vercel site and returned through that same rewrite.
Keep API calls relative as implemented. The backend must allow the exact origin
`https://growthintel.vercel.app` for state-changing membership requests.
