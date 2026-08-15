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

The frontend continues to send `/api/v1/*` through the existing Vercel rewrite.
Because authentication uses a `Secure`, `HttpOnly`, `SameSite=Strict` cookie,
the public frontend and API must be served in a same-site deployment. If the
backend host is cross-site rather than reached through the frontend rewrite,
cookie delivery must be re-designed before launch rather than weakening these
settings.
