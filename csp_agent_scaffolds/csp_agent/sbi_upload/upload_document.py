"""
upload_document.py
Uploads CSPs' current documents (Agreement, Police Verification, IIBF
certificate) from the CSP Document Agent ("agent 1") to the SBI FI portal:
User Services -> Capture PVR DRA IIBF AMFI and Agreement Details of CSP.

The person running it types the login captcha and the OTP (if SBI asks for
one); everything else is automatic. It never solves the captcha itself.

Run it on the machine that has the agent's database and document vault,
from the agent's folder, with the agent's Python (it reads documents through
the agent's own code, so "current document" means the same thing):

    cd /home/shiwangi/csp_agent_scaffolds/csp_agent
    .venv/bin/python /path/to/upload_document.py --plan --csp 1A852417        # what would be uploaded
    .venv/bin/python /path/to/upload_document.py --csp 1A852417               # fill the forms, don't submit
    .venv/bin/python /path/to/upload_document.py --csp 1A852417 --submit      # fill and submit
    .venv/bin/python /path/to/upload_document.py --pending --submit           # every CSP with a document
                                                                              # not uploaded yet

Without --submit nothing is sent to SBI: each form is filled, a screenshot
is saved, and the script moves on. Every submitted document is written to
the ledger file (LEDGER_FILE), so it is never uploaded twice.

Selectors marked "ADJUST SELECTOR" are placeholders: record the real ones
with `playwright codegen https://fi.sbi.bank.in/admin/start.do` and replace
them before the first --submit run.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
Page = PWTimeout = sync_playwright = None       # loaded lazily (browser only; --plan / dashboard need none)

load_dotenv(Path(__file__).with_name(".env"))


def _load_playwright() -> bool:
    """Import Playwright only for an actual browser run; --plan needs none."""
    global Page, PWTimeout, sync_playwright
    try:
        from playwright.sync_api import Page as _P, TimeoutError as _T, sync_playwright as _S
    except ImportError:
        return False
    Page, PWTimeout, sync_playwright = _P, _T, _S
    return True

# ------------------------------------------------------------------ settings
SBI_URL = os.getenv("SBI_URL", "https://fi.sbi.bank.in/admin/start.do")
EP_USERNAME = os.getenv("EP_USERNAME", "")
EP_PASSWORD = os.getenv("EP_PASSWORD", "")
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "3"))
AGENT_DIR = Path(os.getenv("AGENT_DIR", "/home/shiwangi/csp_agent_scaffolds/csp_agent"))
SIGNATORY_NAME = os.getenv("AGREEMENT_SIGNATORY", "Biswajit Chacko")
DATE_FORMAT = os.getenv("SBI_DATE_FORMAT", "%d/%m/%Y")          # ADJUST to what the SBI form accepts
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "2"))         # ADJUST to SBI's file size limit
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"     # a person types the captcha: keep it visible
TIMEOUT_MS = int(os.getenv("STEP_TIMEOUT_MS", "20000"))
HERE = Path(__file__).resolve().parent
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", HERE / "screenshots"))
LOG_DIR = Path(os.getenv("LOG_DIR", HERE / "logs"))

DOC_KINDS = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}

log = logging.getLogger("sbi_upload")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_DIR / f"sbi_upload_{date.today():%Y-%m-%d}.log", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


def screenshot(page: Optional[Page], label: str) -> Optional[Path]:
    """A timestamped screenshot, for a failure or for a dry-run form."""
    if page is None or page.is_closed():
        return None
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
        return path
    except Exception as e:                                      # never hide the real error
        log.warning("Screenshot failed (%s): %s", label, e)
        return None


# ------------------------------------------------------- data from agent 1
def _agent_modules():
    """Put the agent on the path and return (SessionLocal, the sbi_upload
    module). The agent's own code (app.sbi_upload / app.compliance) decides
    what a 'current document' is and which documents are ready to upload, so
    this script and the dashboard always agree."""
    sys.path.insert(0, str(AGENT_DIR))
    os.chdir(AGENT_DIR)                                         # the agent reads its own .env from here
    from app import sbi_upload                                  # noqa: E402
    from app.db import SessionLocal                             # noqa: E402
    return SessionLocal, sbi_upload


# ------------------------------------------------------------------- login
def solve_image(page: Page, attempt: int) -> str:
    """The person types the captcha. The image is also saved, so it can be
    read if the browser window is hidden."""
    img = page.locator("img#captchaImage")                      # ADJUST SELECTOR: the captcha image
    img.wait_for(state="visible", timeout=TIMEOUT_MS)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"captcha_attempt{attempt}.png"
    img.screenshot(path=str(path))
    log.info("Captcha shown in the browser (also saved to %s)", path)
    return input("Type the captcha text and press Enter: ").strip()


def _fill_credentials(page: Page) -> None:
    user = page.locator("input[name='userName']")               # ADJUST SELECTOR: username field
    pwd = page.locator("input[type='password']")                # ADJUST SELECTOR: password field
    if user.input_value() != EP_USERNAME:
        user.fill(EP_USERNAME)
    pwd.fill(EP_PASSWORD)                                       # always: forms often clear it after a failure


def _otp_if_asked(page: Page) -> None:
    """SBI may ask for an OTP after the captcha (OTPVerificationExecute.do)."""
    otp_box = page.locator("input[name='otp']")                 # ADJUST SELECTOR: OTP field
    try:
        otp_box.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        return
    log.info("OTP page shown")
    otp_box.fill(input("Type the OTP sent by SBI and press Enter: ").strip())
    page.get_by_role("button", name=re.compile("submit|verify", re.I)).click()   # ADJUST SELECTOR


def _logged_in(page: Page) -> bool:
    try:
        page.get_by_role("button", name="User Services").wait_for(timeout=10000)   # ADJUST SELECTOR: dashboard marker
        return True
    except PWTimeout:
        return False


def login(page: Page) -> bool:
    """Up to MAX_LOGIN_ATTEMPTS tries; a new captcha image each time."""
    if not EP_USERNAME or not EP_PASSWORD:
        log.error("EP_USERNAME / EP_PASSWORD are not set in .env")
        return False
    page.goto(SBI_URL, wait_until="domcontentloaded")
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        log.info("Login attempt %d of %d", attempt, MAX_LOGIN_ATTEMPTS)
        try:
            _fill_credentials(page)
            page.locator("input[name='captcha']").fill(solve_image(page, attempt))   # ADJUST SELECTOR: captcha input
            page.get_by_role("button", name=re.compile("sign in", re.I)).click()     # ADJUST SELECTOR
            _otp_if_asked(page)
            if _logged_in(page):
                log.info("Logged in")
                return True
            err = page.locator(".errorMessage, .error")                              # ADJUST SELECTOR: login error text
            msg = err.first.inner_text().strip() if err.count() else "dashboard not shown"
            log.warning("Login attempt %d failed: %s", attempt, msg)
        except PWTimeout as e:
            log.warning("Login attempt %d timed out: %s", attempt, e)
        if attempt < MAX_LOGIN_ATTEMPTS:
            log.info("Refreshing the captcha image")
            page.locator("img[alt*='Refresh' i], a:has-text('Refresh')").first.click()  # ADJUST SELECTOR
    log.error("Login failed after %d attempts; stopping (the account may lock after more)", MAX_LOGIN_ATTEMPTS)
    return False


def sign_out(page: Page) -> None:
    try:
        page.get_by_role("link", name=re.compile("sign out", re.I)).click(timeout=TIMEOUT_MS)   # ADJUST SELECTOR
        log.info("Signed out")
    except Exception as e:
        log.warning("Sign out failed: %s", e)


# ------------------------------------------------------- CSP document form
def open_csp(page: Page, code: str) -> Page:
    """Steps 8-16: up to the CSP's popup. Returns the popup page."""
    page.get_by_role("button", name="User Services").click()                                     # ADJUST SELECTOR
    page.get_by_text("Capture PVR DRA IIBF AMFI and Agreement Details of CSP", exact=True).first.click()  # ADJUST SELECTOR
    page.locator("select[name='requestType']").select_option(                                    # ADJUST SELECTOR
        label="Capture PVR DRA IIBF AMFI and Agreement Details of CSP")
    page.get_by_role("button", name="Submit").click()                                            # ADJUST SELECTOR
    page.get_by_label("Query CSP Code").check()                                                  # ADJUST SELECTOR
    page.locator("input[name='cspCode']").fill(code)                                             # ADJUST SELECTOR
    page.get_by_role("button", name="Query").click()                                             # ADJUST SELECTOR
    row = page.locator("table tr", has_text=code)                                                # ADJUST SELECTOR: result row
    row.first.wait_for(timeout=TIMEOUT_MS)
    row.first.locator("input[type='radio'], input[type='checkbox']").first.check()               # ADJUST SELECTOR: Select column
    with page.expect_popup(timeout=TIMEOUT_MS) as pop:
        row.first.get_by_role("link", name=code).click()                                         # ADJUST SELECTOR: CSP Code link
    popup = pop.value
    popup.wait_for_load_state("domcontentloaded")
    log.info("CSP %s: details popup opened", code)
    return popup


def _yes(p: Page, question: str) -> None:
    p.locator(f"xpath=//*[contains(normalize-space(.), '{question}')]/following::input[@type='radio'][@value='Y' or @value='Yes'][1]").check()  # ADJUST SELECTOR


def _field(p: Page, label: str):
    return p.locator(f"xpath=//*[contains(normalize-space(.), '{label}')]/following::input[not(@type='hidden')][1]")  # ADJUST SELECTOR


def fill_pvr(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("button", name="Police Verification Details").click()                          # ADJUST SELECTOR
    _yes(p, "Capture Police Verification Details")
    _field(p, "Name of PVR issuing Authority").fill(csp.state)
    _field(p, "Date of issuance of PVR").fill(d.issue_date.strftime(DATE_FORMAT))
    p.locator("input[type='file']").first.set_input_files(str(d.file))                          # ADJUST SELECTOR


def fill_agreement(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("button", name="CSP Agreement Details").click()                                # ADJUST SELECTOR
    _yes(p, "Capture CSP Agreement Details")
    _field(p, "Signatory name").fill(SIGNATORY_NAME)
    _field(p, "Agreement start date").fill(d.issue_date.strftime(DATE_FORMAT))
    _field(p, "Agreement end date").fill(d.expiry_date.strftime(DATE_FORMAT))
    p.locator("input[type='file']").first.set_input_files(str(d.file))                          # ADJUST SELECTOR


def fill_iibf(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("button", name="IIBF Certificate Details").click()                             # ADJUST SELECTOR
    _yes(p, "Capture IIBF Certificate Details")
    _field(p, "Membership/Registration Number").fill(d.iibf_reg_number)
    _field(p, "Date of Issuance").fill(d.issue_date.strftime(DATE_FORMAT))
    p.locator("input[type='file']").first.set_input_files(str(d.file))                          # ADJUST SELECTOR


FILLERS = {"pvr": fill_pvr, "agreement": fill_agreement, "iibf": fill_iibf}


def upload_csp(page: Page, csp, submit: bool, SessionLocal, sbi, by: str) -> int:
    """Steps 8-18 for one CSP. Returns how many documents were submitted."""
    popup = open_csp(page, csp.code)
    done = 0
    try:
        for d in csp.docs:
            log.info("CSP %s: filling %s (document %d, issued %s, file %s)",
                     csp.code, d.kind, d.document_id, d.issue_date, d.file.name)
            FILLERS[d.kind](popup, csp, d)
            if not submit:
                screenshot(popup, f"dryrun_{csp.code}_{d.kind}")
                log.info("CSP %s: %s filled, NOT submitted (dry run)", csp.code, d.kind)
                continue
            popup.get_by_role("button", name="Submit").click()                                   # ADJUST SELECTOR
            ok = popup.locator("text=/success|submitted|saved/i")                                # ADJUST SELECTOR: confirmation text
            ok.first.wait_for(timeout=TIMEOUT_MS)
            db = SessionLocal()
            try:
                sbi.record_upload(db, csp, d, by)
            finally:
                db.close()
            done += 1
            log.info("CSP %s: %s submitted and recorded in the ledger", csp.code, d.kind)
    finally:
        if not popup.is_closed():
            popup.close()
    return done


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Upload CSP documents from the agent to the SBI FI portal.")
    ap.add_argument("--csp", action="append", default=[], help="CSP code (repeat for several)")
    ap.add_argument("--pending", action="store_true", help="every active CSP with a valid document not uploaded yet")
    ap.add_argument("--docs", default="agreement,pvr,iibf", help="which documents: agreement,pvr,iibf")
    ap.add_argument("--plan", action="store_true", help="only list what would be uploaded; no browser, no Playwright needed")
    ap.add_argument("--submit", action="store_true", help="really submit (default: fill and screenshot only)")
    ap.add_argument("--limit", type=int, default=20, help="at most this many CSPs per run")
    args = ap.parse_args()
    setup_logging()

    kinds = [k.strip() for k in args.docs.split(",") if k.strip()]
    if bad := [k for k in kinds if k not in DOC_KINDS]:
        log.error("Unknown document kind(s): %s (use agreement, pvr, iibf)", ", ".join(bad))
        return 2
    if not args.csp and not args.pending:
        log.error("Give --csp CODE or --pending")
        return 2

    try:
        SessionLocal, sbi = _agent_modules()
    except Exception:
        log.exception("Could not load the agent (is AGENT_DIR right?)")
        return 1
    db = SessionLocal()
    try:
        plan = sbi.plan(db, args.csp or None, kinds, args.pending, max_mb=MAX_UPLOAD_MB)
    except Exception:
        log.exception("Could not read the agent's documents (is its database up?)")
        return 1
    finally:
        db.close()
    plan = [p for p in plan if p.docs][: args.limit] + [p for p in plan if not p.docs]
    for p in plan:
        log.info("CSP %s %s: upload %s%s", p.code, p.name, [d.kind for d in p.docs] or "nothing",
                 f" | skipped: {'; '.join(p.skipped)}" if p.skipped else "")
    todo = [p for p in plan if p.docs]
    if args.plan or not todo:
        log.info("%d CSP(s) to upload. %s", len(todo), "Plan only: nothing opened." if args.plan else "Nothing to do.")
        return 0
    log.info("%d CSP(s) to upload, %s", len(todo), "SUBMITTING" if args.submit else "dry run (forms filled, not submitted)")

    page = None
    failed = 0
    if not _load_playwright():
        log.error("Playwright is not installed, so the browser run can't start. Install it on the workstation: "
                  "pip install playwright==1.65.2 && python -m playwright install chromium. "
                  "(--plan lists the work and needs no browser.)")
        return 2
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=HEADLESS)
            context = browser.new_context(accept_downloads=True)
            context.set_default_timeout(TIMEOUT_MS)
            page = context.new_page()
            if not login(page):
                screenshot(page, "login_failed")
                return 1
            try:
                for csp in todo:
                    try:
                        n = upload_csp(page, csp, args.submit, SessionLocal, sbi, EP_USERNAME or "sbi_upload")
                        log.info("CSP %s: done (%d submitted)", csp.code, n)
                    except Exception:
                        failed += 1
                        log.exception("CSP %s: failed; moving to the next CSP", csp.code)
                        screenshot(page, f"failed_{csp.code}")
                        page.goto(SBI_URL.rsplit("/", 1)[0] + "/", wait_until="domcontentloaded")   # ADJUST: dashboard home
            finally:
                sign_out(page)
                browser.close()
    except Exception:
        log.exception("Unexpected failure")
        screenshot(page, "unhandled")
        return 1
    log.info("Finished: %d CSP(s), %d failed", len(todo), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
