# RUNBOOK — How to Run, Test, and Understand This System (Ubuntu / bash)

Run everything from the `csp_agent/` folder unless noted.

---

## 1. One-time setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (e.g. `nano .env`) — fill in `EMAIL_USER`, `EMAIL_APP_PASSWORD`,
`GEMINI_API_KEY`. Leave `EMAIL_SENDER_ALLOWLIST=` and
`STORAGE_BACKEND=local` for now.

### Database: Neon (recommended -- no local Postgres install needed)

1. neon.tech → New Project (no card required)
2. Copy the connection string from **Connection Details**, keep the
   `?sslmode=require` suffix
3. Branches → Create branch → name it `test`, copy *that* branch's own
   connection string separately

```
DATABASE_URL=postgresql://neondb_owner:...@ep-main-xxxxx.region.aws.neon.tech/neondb?sslmode=require
TEST_DATABASE_URL=postgresql://neondb_owner:...@ep-test-xxxxx.region.aws.neon.tech/neondb?sslmode=require
```

Neon auto-suspends after 5 min idle and auto-resumes on the next query in
under a second -- no manual restore step, unlike Supabase's free-tier
database pause.

### Database: local Postgres (fallback, if you'd rather not depend on Neon for local dev)

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql
```
```sql
CREATE DATABASE csp_db;
CREATE DATABASE csp_db_test;
CREATE USER csp_user WITH PASSWORD 'strong_password_here';
GRANT ALL PRIVILEGES ON DATABASE csp_db TO csp_user;
GRANT ALL PRIVILEGES ON DATABASE csp_db_test TO csp_user;
\q
```
```
DATABASE_URL=postgresql://csp_user:strong_password_here@localhost:5432/csp_db
TEST_DATABASE_URL=postgresql://csp_user:strong_password_here@localhost:5432/csp_db_test
```

If `psql` complains about peer authentication later:
```bash
sudo nano /etc/postgresql/*/main/pg_hba.conf
# change the "local" and "127.0.0.1" lines' method to "md5" or "scram-sha-256"
sudo systemctl restart postgresql
```

**Note for the rest of this runbook:** every `psql -U csp_user -d csp_db
-h localhost -c "..."` command below assumes local Postgres. If you're on
Neon, replace it with `psql "$DATABASE_URL" -c "..."` instead (or export
it first: `export $(grep DATABASE_URL .env)`).

---

## 2. Start the app

```bash
uvicorn app.main:app --reload
```

Leave this running in its own terminal. Open a **second** terminal (or a
new tab: `Ctrl+Shift+T`) for every command below.

---

## 3. Confirm it's actually alive

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

`/ready` must show `"database": "ok"` and `"scheduler": "running"`. Fix
`DATABASE_URL` first if `database` shows an error. Pipe through
`python3 -m json.tool` for readable output:
`curl -s http://127.0.0.1:8000/ready | python3 -m json.tool`

---

## 4. Open the interactive UI — this is how you'll "see" it

```
http://127.0.0.1:8000/docs
```

Swagger UI. Click **"Try it out"** on any endpoint, fill in fields, hit
**Execute** — no curl needed for exploration. Use the commands below when
you want something scriptable/repeatable.

---

## 5. Feed in the sample CSPs (Phase 1)

```bash
curl -X POST "http://127.0.0.1:8000/api/campaigns/import-csps" \
  -F "file=@samples/csp_master_sample.csv"
```

**Expect:** `{"received": 3, "imported": 3, "rejected": 0, "errors": []}`

Check it landed in the DB:
```bash
psql -U csp_user -d csp_db -h localhost -c "SELECT id, name, lookup_code, email FROM csp;"
```
(`-h localhost` forces password auth instead of Ubuntu's peer auth, which
otherwise expects your Linux username to match a Postgres role.)

---

## 6. Create a campaign (Phase 2)

```bash
curl -X POST "http://127.0.0.1:8000/api/campaigns/" \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Renewal Campaign", "document_types": ["AGREEMENT", "POLICE_VERIFICATION"], "template_version": "v1", "deadline": "2026-12-31", "created_by": 1, "csp_status_filter": "ACTIVE"}'
```

**Expect:** `{"campaign_id": 1, "status": "DRAFT", "eligible_csps": 3, "targets_created": 3}`

Verify:
```bash
curl -s http://127.0.0.1:8000/api/campaigns/1/targets | python3 -m json.tool
```
All 3 should show `"status": "READY_FOR_REQUEST"`.

---

## 7. Send the initial request (Phase 4 — the actual "ask all CSPs" step)

```bash
curl -X POST "http://127.0.0.1:8000/api/campaigns/1/send"
```

**Expect:** `{"campaign_id": 1, "sent": 3, "skipped_no_email": 0}`

This sends real emails via your configured `EMAIL_USER` SMTP — make sure
the sample CSV's email addresses are ones you control, or edit
`samples/csp_master_sample.csv` first. Re-check `targets` — all 3 should
now show `REQUEST_SENT`.

---

## 8. Simulate a CSP responding (manual for now)

Send a real email **to** `EMAIL_USER` (your test mailbox), subject
containing a code shaped per `CSP_CODE_REGEX` in `.env` (default: like
`1A852474`). No attachment to test "replied but no document"; attach a
synthetic PDF to test document detection.

Then pull it in:
```bash
curl -X POST "http://127.0.0.1:8000/api/ingest/email/run-now"
```

**Expect:** `{"processed": 1, "errors": 0}`

Check what happened:
```bash
curl -s http://127.0.0.1:8000/api/campaigns/1/targets | python3 -m json.tool
```
Look for `RESPONSE_RECEIVED_NO_DOCUMENT` (no attachment) or
`AWAITING_ADDITIONAL_DOCUMENTS` / `DOCUMENT_NEEDS_APPROVAL` (attachment sent).

---

## 9. See exactly *why* — the audit trail

```bash
psql -U csp_user -d csp_db -h localhost -c "SELECT event_type, source, notes, sent_at FROM agreement_events ORDER BY sent_at DESC LIMIT 10;"
```

Every state change logs a `CAMPAIGN_TARGET_STATUS_CHANGED` row here with
the reason — your primary debugging tool.

Check the document itself:
```bash
psql -U csp_user -d csp_db -h localhost -c "SELECT id, status, document_type, overall_confidence, storage_path FROM documents ORDER BY id DESC LIMIT 5;"
```
`status` should be `NEEDS_APPROVAL` or `NEEDS_REVIEW` — **never** `VALID`
at this stage, by design.

---

## 10. Approve a renewal (the human-approval gate)

Seed one authorized reviewer (one-time):
```bash
psql -U csp_user -d csp_db -h localhost -c "INSERT INTO internal_users (name, email, role) VALUES ('Test DC', 'testdc@example.com', 'DC');"
psql -U csp_user -d csp_db -h localhost -c "SELECT id FROM internal_users WHERE role='DC';"
```

Find the pending review:
```bash
curl -s http://127.0.0.1:8000/api/review/ | python3 -m json.tool
```

Approve it (replace `1` with the real `review_id` and `reviewer_id`):
```bash
curl -X POST "http://127.0.0.1:8000/api/review/1/approve-renewal" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": 1}'
```

**Expect:** `{"status": "RENEWED", "agreement_id": ..., "document_id": ..., "new_expiry_date": "..."}`

Confirm the agreement actually changed:
```bash
psql -U csp_user -d csp_db -h localhost -c "SELECT csp_id, expiry_date, renewal_status FROM agreements ORDER BY id DESC LIMIT 5;"
```

---

## 11. Run the automated tests

```bash
pytest tests/ -v
```

Proves — without manually clicking through every time — that duplicate
reminders never send twice, T-0 locks the agreement correctly, and a
high-confidence AI extraction can never set `VALID` without approval.

---

## 12. Quick reference — what each table tells you

| Table | Tells you |
|---|---|
| `csp` | Who's in the system |
| `campaign_targets` | Where each CSP is in the request/response/approval cycle — **your main status view** |
| `documents` | What's been uploaded, its AI-read confidence, and current status |
| `submission_items` | Which specific required document (agreement vs. PVR) is satisfied per CSP |
| `agreement_events` | The full audit trail — every automated decision, with a reason |
| `manual_review_queue` | What's currently waiting on a human |
| `agreements` | The official, approved renewal dates — only changes via approve-renewal |

---

## Ubuntu-specific gotchas

- **`pip install` fails with "externally managed environment"** — you're
  probably not inside the venv. Confirm with `which python3` — it should
  point into `.venv/bin/`. If you really need a system-wide install for
  something, add `--break-system-packages`, but prefer the venv.
- **`psql: FATAL: Peer authentication failed`** — happens when you omit
  `-h localhost`; without it, `psql` uses Unix socket "peer" auth, which
  checks your Linux username against a matching Postgres role instead of
  a password. Always include `-h localhost` for `csp_user`.
- **Port 8000 already in use** — `sudo lsof -i :8000` to find the process,
  `kill <pid>` to stop it, or run uvicorn on another port:
  `uvicorn app.main:app --reload --port 8001` (and adjust every URL above).
- **IMAP/Gmail connection refused** — check that outbound port 993 isn't
  blocked by a corporate/VPN firewall; test with
  `openssl s_client -connect imap.gmail.com:993 -quiet` (Ctrl+C to exit
  once you see a connection, not an error).

---

## Autonomous agent (2026-09 rebuild) — how to run it

### Processes
| What | Command | Notes |
|---|---|---|
| Web (dashboard, API, upload portal) | `.venv/bin/python -m uvicorn app.main:app --port 8000` | Starts no background jobs. |
| Worker (the agent) | `.venv/bin/python -m app.worker` | Gmail every 15 min, calling sheet hourly, follow-ups daily 09:00 IST, outbox every 2 min. Run one. |
| First Gmail backfill | `.venv/bin/python -m scripts.run_backfill` | Reads the last 2 years once, resumable. The worker also continues it. |

Always call tools as `.venv/bin/python -m ...`: the scripts in `.venv/bin/` (pip, uvicorn) point at another
project's Python. Recreating the venv fixes that: `python3 -m venv --clear .venv && .venv/bin/python -m pip install -r requirements.txt`.

On the rack server use `deploy/csp-web.service` and `deploy/csp-worker.service` (systemd, restart on reboot),
and `deploy/ollama-model.md` for the free local vision model.

### Testing vs deployment
- Testing (now): `OUTBOUND_COMMUNICATION_MODE=review`, `WHATSAPP_MODE=stub`, `CALLING_SHEET_SOURCE=local_xlsx`.
  Every message is a draft in the dashboard's Communication Hub; nothing leaves until you approve it, and
  approved WhatsApp messages are marked "ready, not sent" until the WhatsApp agent is connected.
- Deployment: `OUTBOUND_COMMUNICATION_MODE=auto`, `WHATSAPP_MODE=push` (or `pull`), `CALLING_SHEET_SOURCE=google`.
  Every sent message stays on record in `outbound_messages`.

### Database
- Schema is managed by Alembic: `.venv/bin/python -m alembic upgrade head`.
- Tests use `<main db>_test` (or `TEST_DATABASE_URL` if it's a different database whose name contains "test")
  and refuse to run against the main database.

### Where things are
- Documents live in the vault, `STORAGE_ROOT` (default `storage/sbi_kiosk/documents/`), one folder per CSP:
  ```
  INDEX.xlsx                                   every CSP x document, state, dates (rebuilt nightly)
  1A850004_KAUSHAR_JAHAN/
      1A850004_AGREEMENT_ACTIVE_2024-05-25_to_2027-05-24.pdf
      1A850004_PVR_EXPIRED_2024-03-10_to_2025-03-09.pdf   current copy, expired: renew it
      1A850004_IIBF_ACTIVE_2023-11-02_lifetime.pdf
      expired/     older copies (EXPIRED, or REPLACED before they expired)
      unreadable/  blurred/unreadable copies, kept for audit
      rejected/    copies a reviewer rejected
  ```
  States in the name: ACTIVE, EXPIRED, REVIEW (date read by the vision model, check it), REPLACED, UNREADABLE.
  The worker's `vault` job (daily 00:10) renames ACTIVE -> EXPIRED the night a document expires; a renewed
  copy takes the top slot and pushes the old one into `expired/`. Never rename files by hand: the database
  decides the names, and the next run would rename them back.
  Moving the old layout into the vault: `python -m scripts.reorganize_vault` (dry run, CSV in logs/), then `--apply`.
  Dashboard → CSP → "Download all 3 (zip)".
- Follow-up schedule: `app/policy.py` (data). Categories: `app/compliance.py`. Engine: `app/renewal_engine.py`.
- Date rules: `app/ai/extraction/rules/`. Accuracy check: `scripts/eval_extraction.py` with `samples/gold.csv`.
- Re-read unreadable files after installing the vision model: `scripts/retry_unreadable.py`.
