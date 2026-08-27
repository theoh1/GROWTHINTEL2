# GrowthIntel Mailjet Setup Report

## What changed

- Added production email subscription endpoints to the active lightweight backend:
  - `POST /api/v1/email/subscribe`
  - `GET /api/v1/email/preferences`
  - `PUT /api/v1/email/preferences`
  - `POST /api/v1/email/unsubscribe`
  - `POST /api/v1/email/alert-event`
  - `POST /api/v1/cron/weekly-growth-brief`
  - `POST /api/v1/cron/re-engagement`
- Added persistent database tables for subscribers, referral tracking and delivery logs.
- Added Mailjet sending through server-side environment variables only.
- Kept development/testing safe with `EMAIL_SEND_ENABLED=false`.
- Added support for separate senders:
  - general emails: `EMAIL_FROM_ADDRESS`
  - support fallback emails: `SUPPORT_EMAIL_FROM_ADDRESS`
- Added tests covering subscribe, duplicate signup, preferences, referral tracking, unsubscribe, alert dedupe and cron protection.

## Recommended sender addresses

- General GrowthIntel emails: `hello@growthintel.app`
- Support replies/notifications: `support@growthintel.app`

These addresses must be verified in Mailjet before `EMAIL_SEND_ENABLED=true` is used.

## Required environment variables

Set these on the Render backend:

```text
EMAIL_PROVIDER=mailjet
EMAIL_SEND_ENABLED=true
MAILJET_API_KEY=...
MAILJET_SECRET_KEY=...
EMAIL_FROM_ADDRESS=GrowthIntel <hello@growthintel.app>
SUPPORT_EMAIL_FROM_ADDRESS=GrowthIntel Support <support@growthintel.app>
EMAIL_BASE_URL=https://www.growthintel.app
EMAIL_CRON_SECRET=...
CRON_SECRET=...
```

Set these on Vercel too for cron/support routes that run through Vercel:

```text
MAILJET_API_KEY=...
MAILJET_SECRET_KEY=...
EMAIL_FROM_ADDRESS=GrowthIntel <hello@growthintel.app>
SUPPORT_EMAIL_FROM_ADDRESS=GrowthIntel Support <support@growthintel.app>
EMAIL_BASE_URL=https://www.growthintel.app
CRON_SECRET=...
```

## Mailjet setup still required

1. In Mailjet, add and verify the sending domain `growthintel.app`.
2. Add the SPF/DKIM DNS records Mailjet gives you in the DNS provider for `growthintel.app`.
3. Add/verify sender addresses such as `hello@growthintel.app` and `support@growthintel.app`.
4. Make sure those addresses can receive verification/reply emails through a mailbox or email-routing service.
5. After Mailjet shows the sender/domain as verified, set `EMAIL_SEND_ENABLED=true` on Render.
6. Redeploy/restart the backend after changing environment variables.

## Database schema

The backend creates the email tables idempotently on startup when `DATABASE_URL` is configured. A manual SQL copy is also available at:

```text
backend-live/EMAIL_SYSTEM_SCHEMA.sql
```

## Tests completed

```text
py -3 -m py_compile backend-live/membership_bootstrap.py
node --check api/support-runtime.js
py -3 -m pytest backend-live/test_membership.py
```

Result: all 14 backend tests passed.
