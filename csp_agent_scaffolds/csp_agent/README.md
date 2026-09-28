# CSP Agreement Automation Agent

The smallest reliable version, built to the priority order we settled on:
stabilize → document storage → structured AI extraction → wire into the
state machine → deterministic dedup → email channel → dashboard/review
queue. Six autonomous agents, pgvector, RoomKit, WhatsApp/SMS, and
Celery/Redis are intentionally **not** in here yet — see the project plan
doc for why, and add them only once this core loop has run reliably for a
while.

## What this proves

> Can the system reliably ingest one agreement, validate it, store it,
> calculate expiry, send one correct reminder, and show the result on
> the dashboard?

Everything in this scaffold exists to answer that question. Nothing else.

## Project layout

```
app/
├── main.py              FastAPI app, /health, /ready
├── config.py             all settings, read from .env
├── logging_config.py      structured JSON logging
├── db.py                  SQLAlchemy engine/session
├── models.py               csp, agreements, documents, reminders,
│                             escalations, manual_review_queue, etc.
├── policy.py               the reminder ladder as DATA, not code
├── idempotency.py           idempotency-key builder + duplicate check
├── validation.py             deterministic checks AFTER the AI extraction
├── langgraph_flow.py          the actual state machine (see below)
├── scheduler.py                APScheduler: daily expiry check + email poll
├── email_ingest.py              IMAP polling (async-safe wrapper included)
├── csv_ingest.py                  bulk CSV onboarding
├── ai/
│   ├── schemas.py                  Pydantic extraction schemas
│   ├── service.py                   public interface — business logic
│   │                                  never imports providers directly
│   └── providers/gemini.py            current (and only) provider
├── comms/
│   ├── base.py                       NotificationProvider interface
│   └── email_adapter.py               first and only channel right now
└── api/
    ├── csp.py                          includes the fast code-lookup route
    ├── agreements.py                    includes manual run-flow trigger
    ├── documents.py                      upload → dedup → extract → route
    ├── ingest.py                          CSV + manual email-poll trigger
    └── review.py                          human review queue

tests/
├── conftest.py            isolated test DB + graph SessionLocal patch
└── test_escalation.py     one test per escalation stage (freezegun)
```

## The state machine

```
load_agreement
      ↓
calculate_expiry
      ↓
evaluate_policy
      ├── no_action
      ├── send_reminder   (channels come from policy.py, idempotency-checked)
      └── escalate        (RM/DC/LHO + optional EXPIRED_LOCKED at T-0)
```

One LangGraph thread per agreement (`thread_id` would be `agreement_id`
once you add a checkpointer — not wired in yet, since APScheduler already
re-invokes the graph daily and idempotency keys make re-invocation safe
without needing pause/resume checkpointing for this version).

An LLM is called from exactly one place: `ai/service.validate_document()`,
triggered from the document upload endpoint. It is never on the path of
`evaluate_policy` or the daily scheduler run — those are pure DB queries,
so they stay fast regardless of Gemini's free-tier rate limits.

## The human-approval gate (critical, do not remove)

An AI `AUTO_ACCEPT_CANDIDATE` decision — even at 0.97 confidence with zero
flagged issues — **never** directly renews an agreement or marks a document
`VALID`. It routes to `Document.status = NEEDS_APPROVAL` and a
`ManualReviewQueue` item tagged `AWAITING_RENEWAL_APPROVAL`. The only code
path allowed to set `Agreement.renewal_status = RENEWED` or
`Document.status = VALID` is `POST /api/review/{id}/approve-renewal`, which:

- requires an `InternalUser` with role `DC`, `LHO`, or `ADMIN` (placeholder
  check — replace with real session auth before this goes near production),
- re-runs `apply_deterministic_checks()` against the *current* CSP master
  list rather than trusting the decision made at extraction time,
- archives the old CSP code into `csp_code_history`, updates the agreement,
  links the document, and logs an `AGREEMENT_RENEWED` audit event — all in
  one transaction.

`tests/test_approval_gate.py` exists specifically to prove this boundary:
a 0.97-confidence extraction with no issues still lands on
`NEEDS_APPROVAL`, never `VALID`, until an authorized reviewer approves it.

A vision model can report that a signature/stamp-*shaped* mark is present
on a page (`VisualCheck.detected`) — it can never assert authenticity,
authority, or that the document wasn't altered. `validation.py` always
flags `visual_review_required: True` for exactly this reason.

## Feature flags for the extraction pipeline

```
DOCUMENT_AI_MODE=text        # default — extract text, send text to Gemini
DOCUMENT_AI_MODE=multimodal  # not implemented yet — send page images directly to a vision model
DOCUMENT_PARSER=pymupdf      # default — fast, fine for digital PDFs with selectable text
DOCUMENT_PARSER=docling      # not implemented yet — layout-aware, for tables/multi-column forms
```
Both `multimodal` and `docling` raise `NotImplementedError` on purpose —
adopt them only after comparing output quality against the current
pipeline on real (sanitized) sample documents, not because they sound more
advanced.

## Bulk CSP onboarding (Phase 1 + 2 of the campaign model)

Before any reminder can be sent, the system needs to actually know your
500+ CSPs. Two new endpoints, deliberately stopping short of sending
anything:

- `POST /api/campaigns/import-csps` — upload a CSV (see required columns
  below), validates every row, resolves RM/DC/LHO by email (creating stub
  `InternalUser` rows if they don't exist yet), and creates/updates `CSP`
  + `Agreement` records. Returns an import report
  (`received`/`imported`/`rejected`/`errors`) instead of silently
  succeeding or failing — review that report before doing anything else.
  Required CSV columns: `csp_code, csp_name, agreement_start_date,
  agreement_expiry_date`. Optional: `email, phone, whatsapp_number,
  rm_email, dc_email, lho_email, police_verification_expiry, status,
  region, branch, kiosk_location, preferred_language`.

- `POST /api/campaigns/` — creates a `Campaign` + one `CampaignTarget` per
  eligible CSP (`status=READY_FOR_REQUEST`) + `RequiredDocument` rows.
  Still does not send anything.

**Intentionally not built yet** (each is a real next step, not a stub):
campaign preview (`GET /api/campaigns/{id}/preview`), the actual send
step, inbound-response classification (no-response vs. text-only vs.
document vs. wrong-sender), and the state-based follow-up scheduler that
would query `campaign_targets` on a separate job from the existing
`daily_expiry_check`. Building the send step before the state model has
been exercised against real CSP replies would mean guessing at edge cases
instead of discovering them safely.

**Why bulk import can write directly to `CSP`/`Agreement`, but a document
upload can't reach `VALID` without approval:** these are different risk
categories. An authorized operator running a CSV import from the company's
own master data is the equivalent of a human typing that data into a form
— not an unverified AI proposal. The human-approval gate in
`app/api/review.py` exists specifically for *AI-extracted* document data,
which is a fundamentally less trustworthy source.

## Deployment: Render + Neon + Supabase Storage + billed Gemini (chosen stack)

Verified against current (Sept 2026) pricing/behavior before choosing this
split:
- **Render** free web service: no time limit, sleeps after 15 min idle
  (~30-60s cold start on wake — fine for an internal RM/DC tool).
- **Neon** for the database: scales to zero after 5 min idle but
  **auto-resumes on the next query in under a second, with no manual
  intervention** — this matters because our scheduler runs unattended; a
  provider that requires a human to click "Restore" after a pause (which
  Supabase's free tier does, after 7 days) is the wrong fit for a
  cron-driven backend. Neon's branching feature also gives a free,
  separate `TEST_DATABASE_URL` with zero extra setup.
- **Supabase Storage only** (not Supabase's database) for uploaded
  documents — kept because it's free and already wired in
  `app/storage.py`, and file storage isn't subject to the same
  auto-pause risk pattern as an idle database is (though see the
  keepalive note below).
- **Gemini with Cloud Billing enabled, $0 spend**: attaching a payment
  method to the Google Cloud project (no charge expected while under the
  free quota) moves usage from "free tier, used for training, human
  reviewers may read it" to "Paid Services terms, no training, DPA
  applies" — verified directly against Google's billing documentation.
  This is a Google Cloud Console setting, not a code change.

**1. Create the Neon project**: neon.tech → New Project (no card
needed). Copy the connection string from **Connection Details** — keep
the `?sslmode=require` suffix. Create a **branch** named `test` for a
free, separate `TEST_DATABASE_URL` (Branches → Create branch), rather
than a second manually-managed database.

**2. Create the Supabase project for Storage only**: supabase.com → New
Project → Storage → New bucket → `csp-documents` (private). Get the
**service_role key** (Settings → API) for `SUPABASE_SERVICE_KEY` — not
the anon key, since `app/storage.py`'s upload/read calls need write
access. You will not use this project's own Postgres database.

**3. Enable Cloud Billing on the Gemini project**: console.cloud.google.com
→ select the project your `GEMINI_API_KEY` belongs to → Billing → Link a
billing account → set a small budget alert (e.g. ₹10-50) immediately
under Budgets & alerts, so an unexpected spend emails you rather than
surprises you at month-end. Confirm the project shows **"Paid"** under
Plan on AI Studio's API key page.

**4. Push this repo to GitHub**, then in Render: New → Blueprint → point
at the repo. Render reads `render.yaml`. Fill in the `sync: false` env
vars in the Render dashboard: `DATABASE_URL` (Neon), `EMAIL_USER`,
`EMAIL_APP_PASSWORD`, `GEMINI_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY` (Supabase, Storage only).

**5. Confirm `STORAGE_BACKEND=supabase` is set** in the deployed
environment — `STORAGE_BACKEND=local` on Render means every uploaded
document is lost on the next redeploy or idle-restart, since Render's
free disk is ephemeral.

**6. Table creation is automatic** — `Base.metadata.create_all()` runs on
first startup (see `main.py`) against whichever `DATABASE_URL` is set, no
separate migration step needed at this stage.

**7. Prevent the Supabase Storage project from pausing** — even though
the database moved to Neon, the Supabase project (now used for Storage
only) can still fully pause after 7 days with no activity, taking file
access down with it. A tiny scheduled GitHub Actions workflow pinging
Supabase's REST endpoint every 2-3 days prevents this; ask for the
workflow file if you haven't added one yet.

**8. Keep Render warm during business hours (optional, still free):** an
UptimeRobot free-tier ping or scheduled GitHub Actions workflow hitting
`/health` every 10 minutes avoids the cold-start delay on the CSP-facing
code-lookup flow. Not needed for correctness — Neon and the scheduler's
job store both persist state independently of whether Render is awake —
only affects response latency.

**Local development uses `STORAGE_BACKEND=local`** (the default in
`.env.example`) and can point `DATABASE_URL` at either local Postgres or
directly at Neon — only the deployed environment must use `supabase` for
storage.

## Setup and running the system

See **`RUNBOOK.md`** for the full, copy-paste command sequence (Ubuntu/bash) —
virtual environment setup, starting the app, and a complete walkthrough
of every endpoint with expected output at each step.

## Trying the core loop manually

1. Seed one CSP and one agreement expiring in 7 days (via `/docs` Swagger
   UI or direct SQL insert).
2. `POST /api/agreements/{id}/run-flow` — this invokes the exact same graph
   the 9am scheduler would, so you see the T-7 reminder + RM escalation
   fire immediately.
3. Check `GET /api/csp/lookup/CSP12345` — confirms the fast lookup path
   works independently of the background flow.
4. Upload a PDF via `POST /api/documents/upload?csp_id=1` — confirms
   dedup (upload the same file twice, second call returns `DUPLICATE`)
   and AI routing (`VALID` / `NEEDS_REVIEW` / `REJECTED` depending on
   Gemini's confidence and the deterministic checks in `validation.py`).
5. `GET /api/review/` — anything routed to human review shows up here;
   `POST /api/review/{id}/resolve` to approve/correct/reject it.

## What's deliberately not here yet

- WhatsApp/SMS adapters — the `NotificationProvider` interface in
  `comms/base.py` is ready for them; email is first because it's free and
  easiest to audit.
- Migrations via Alembic — `alembic init migrations` and point
  `sqlalchemy.url` at `app.config.DATABASE_URL` when you're ready to stop
  relying on `create_all()`.
- LangGraph checkpointing/pause-resume — not needed while the scheduler
  re-invokes daily and idempotency keys make that safe.
- Risk-tiered escalation, batch RM digests, response-channel optimization,
  auto-format correction, the feedback-loop table — all real ideas, all
  correctly deferred until the core loop has run for a while and you know
  what actually breaks.

## Fixes applied after external review

A second review caught real bugs in the first version of this scaffold.
Fixed, in the code as it stands now:

- `GraphState` widened (`total=False` + `days_left`/`stage_policy`) to match
  what the graph actually carries.
- `SQLAlchemyJobStore(url=...)` instead of `engine=...` for broader
  APScheduler version compatibility.
- Escalations are now idempotent: a partial unique index on
  `(agreement_id, level) WHERE resolved_at IS NULL` plus a check-before-insert,
  so a daily re-scan doesn't open a second RM/DC/LHO escalation every day
  the agreement sits at the same stage. (A plain `UniqueConstraint` would
  **not** have worked here — Postgres treats every `NULL` as distinct.)
- Added `CSP.lho_id` and an explicit `RECIPIENT_FIELDS` map (`RM→rm_id`,
  `DC→dc_id`, `LHO→lho_id`) instead of `getattr(csp, f"{role.lower()}_id")`.
- T-0 now sends the `critical_escalation` template, never the routine
  `renewal_reminder`, once the agreement is `EXPIRED_LOCKED` — covered by
  a test.
- T-90 is `internal_only` and no longer flips `renewal_status` to
  `PENDING_RENEWAL` — only real reminder stages do that.
- `Document.extracted_fields` is `JSONB`, not `Text` — queryable later.
- Uploads are actually written to disk now (`storage/documents/<sha256>`,
  hash-named to avoid collisions and path traversal), not just hashed in
  memory.
- Email ingestion has a durable `InboundMessage` dedup table keyed on
  `Message-ID`, independent of the mailbox's `\Seen` flag (which isn't
  crash-safe on its own).
- Email parsing uses `email.policy.default` + `decode_header` for subjects,
  with an HTML-body fallback when there's no plain-text part.
- `AgreementEvent` now carries `csp_id`, `source`, and a `JSONB payload`
  column, so email-ingestion events are actually traceable to a CSP.
- `EmailAdapter` and the reminder's `delivery_status` are labeled
  `SMTP_ACCEPTED`, not `SENT`/`DELIVERED` — SMTP acceptance is not delivery
  confirmation, and claiming otherwise would be a false audit trail entry.
- Document upload is now **async**: `POST /upload` returns `202 PROCESSING`
  immediately (after the fast SHA-256 dedup check) and the Gemini call runs
  in a `BackgroundTask` after the response is sent. This is *not* a durable
  task queue — no retry, no cross-process persistence — so a process crash
  mid-extraction leaves a document stuck at `PROCESSING` until manually
  reprocessed. Move to Celery+Redis (Upstash's free tier works) once that
  gap actually matters.
- Tests: the async adapter is now monkeypatched with a real `async def`
  fake, not a plain lambda (`_run_async()` would otherwise receive a
  non-awaitable and fail); every test uses a UUID-based CSP code instead of
  a fixed literal to avoid unique-constraint collisions between tests; two
  new tests cover the T-0 template switch and repeated-scan escalation
  idempotency.

**Explicitly still labeled MVP, not production**: no authentication yet on
the internal API routes, PDF/CSV branching from email attachments is not
wired to the same extraction pipeline as direct upload (attachments land as
`UPLOADED` documents only), no Alembic migrations yet, and background-task
processing has no retry/durability guarantee. All of these are called out
explicitly rather than silently assumed to work.
