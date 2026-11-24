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
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path):
        p = Path(path)
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
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
# SBI settings may live in sbi_upload/.env OR in the agent's own .env; both are
# read (sbi_upload/.env wins where both set the same value). Nothing is printed.
AGENT_DIR = Path(os.getenv("AGENT_DIR", "/home/shiwangi/csp_agent_scaffolds/csp_agent"))
load_dotenv(AGENT_DIR / ".env")                                # does not override values from sbi_upload/.env
SBI_URL = os.getenv("SBI_URL", "https://fi.sbi.bank.in/admin/start.do")
EP_USERNAME = os.getenv("EP_USERNAME", "")
EP_PASSWORD = os.getenv("EP_PASSWORD", "")
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
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
    """Always log to the screen; add a log file too when the folder is
    writable (set LOG_DIR to a folder you own if you run it as another user)."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log.setLevel(logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_DIR / f"sbi_upload_{date.today():%Y-%m-%d}.log", encoding="utf-8"))
    except OSError as e:
        print(f"[warn] not writing a log file ({LOG_DIR}: {e}); set LOG_DIR to a writable folder to keep one.")
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


def screenshot(page: Optional[Page], label: str) -> Optional[Path]:
    """A timestamped screenshot, for a failure or for a dry-run form."""
    if page is None or page.is_closed():
        return None
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Cannot write screenshots to %s (%s); set SCREENSHOT_DIR.", SCREENSHOT_DIR, e)
        return None
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
def solve_image(page: Page, attempt: int, max_image_retries: int = 3) -> str:
    """The person types the captcha. The image is also saved, so it can be
    read if the browser window is hidden. Tesseract OCR is used if available.
    If OCR does not find exactly 6 characters, automatically refreshes image before returning."""
    for r in range(max_image_retries):
        img = page.locator("img#captchaImage, img[alt*='captcha' i], img[src*='captcha' i]").first
        img.wait_for(state="visible", timeout=TIMEOUT_MS)
        captcha_path = None
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            captcha_path = SCREENSHOT_DIR / f"captcha_attempt{attempt}_sub{r}.png"
            img.screenshot(path=str(captcha_path))
            log.info("Captcha image saved to %s", captcha_path)
        except Exception as e:
            log.warning("Could not save captcha screenshot: %s", e)

        # Attempt automatic Tesseract OCR
        ocr_text = ""
        if captcha_path and captcha_path.exists():
            try:
                import pytesseract
                from PIL import Image, ImageEnhance
                img_obj = Image.open(captcha_path)
                w, h = img_obj.size

                # Strategy 1: 2x Lanczos + Contrast enhance (PSM 7)
                img_scaled = img_obj.resize((w * 2, h * 2), Image.Resampling.LANCZOS).convert("L")
                enhanced = ImageEnhance.Contrast(img_scaled).enhance(2.0)
                raw = pytesseract.image_to_string(
                    enhanced,
                    config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                )
                cand = re.sub(r"[^A-Za-z0-9]", "", raw).strip()
                if len(cand) == 6:
                    ocr_text = cand

                # Strategy 2: PSM 8 (single word)
                if len(ocr_text) != 6:
                    raw = pytesseract.image_to_string(
                        enhanced,
                        config="--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    )
                    cand = re.sub(r"[^A-Za-z0-9]", "", raw).strip()
                    if len(cand) == 6:
                        ocr_text = cand

                # Strategy 3: 3x scale + contrast
                if len(ocr_text) != 6:
                    img_scaled3 = img_obj.resize((w * 3, h * 3), Image.Resampling.LANCZOS).convert("L")
                    enh3 = ImageEnhance.Contrast(img_scaled3).enhance(2.2)
                    raw = pytesseract.image_to_string(
                        enh3,
                        config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    )
                    cand = re.sub(r"[^A-Za-z0-9]", "", raw).strip()
                    if len(cand) == 6:
                        ocr_text = cand

                # Strategy 4: Raw original image
                if len(ocr_text) != 6:
                    raw_orig = pytesseract.image_to_string(
                        img_obj,
                        config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    )
                    cand = re.sub(r"[^A-Za-z0-9]", "", raw_orig).strip()
                    if len(cand) == 6 or not ocr_text:
                        ocr_text = cand

                if ocr_text:
                    log.info("Tesseract detected captcha: '%s'", ocr_text)
            except Exception as e:
                log.warning("Tesseract OCR failed: %s", e)

        auto_captcha = os.getenv("AUTO_CAPTCHA", "false").lower() in ("1", "true", "yes")
        if len(ocr_text) == 6:
            if auto_captcha:
                log.info("Auto-filling captcha: %s", ocr_text)
                return ocr_text
            prompt = f"Type captcha text (Tesseract detected [{ocr_text}], press Enter to accept): "
            typed = input(prompt).strip()
            return typed if typed else ocr_text

        # If not 6 chars, refresh image before submitting
        if r < max_image_retries - 1:
            log.info("Captcha OCR result '%s' is not 6 chars. Refreshing captcha image...", ocr_text)
            try:
                refresh_btn = page.locator("img[alt*='Refresh' i], a:has-text('Refresh'), img[src*='refresh' i], [title*='Refresh' i]").first
                if refresh_btn.is_visible(timeout=3000):
                    refresh_btn.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass

    if auto_captcha and ocr_text:
        log.info("Auto-filling best-effort captcha: %s", ocr_text)
        return ocr_text

    prompt = f"Type captcha text (Tesseract detected [{ocr_text or 'none'}], press Enter to accept): "
    typed = input(prompt).strip()
    return typed if typed else ocr_text


def _fill_credentials(page: Page) -> None:
    user = page.locator("#specialChar, input[name='userName'], input[id*='user' i]").first
    pwd = page.locator("#password, input[type='password']").first
    user.wait_for(state="visible", timeout=15000)
    user.fill(EP_USERNAME)
    pwd.fill(EP_PASSWORD)


CONSUMED_OTPS: set[str] = set()


def _fetch_relayed_otp(timeout_seconds: float = 45.0) -> Optional[str]:
    """Poll for freshly relayed OTP from spool directory, /tmp/sbi_latest_otp.json, /tmp/sbi_latest_otp.txt, or remote relay server."""
    start_time = time.time()
    min_ts = start_time - 1.5
    otp_spool = Path(os.getenv("OTP_SPOOL_DIR", "/home/adarsh/eko/.tmp/sbi_stmt_bot/sms_inbox"))
    otp_json = Path(os.getenv("OTP_JSON_FILE", "/tmp/sbi_latest_otp.json"))
    otp_txt = Path(os.getenv("OTP_TEXT_FILE", "/tmp/sbi_latest_otp.txt"))
    relay_url = os.getenv("OTP_RELAY_URL", "https://indev.eko.in/csp-agreement-agent/sbi-otp/latest")
    deadline = start_time + timeout_seconds

    log.info("Checking for relayed SBI OTP (waiting up to %ds from mobile relay, after %s)...",
             int(timeout_seconds), time.strftime("%H:%M:%S", time.localtime(start_time)))
    while time.time() < deadline:
        # 1. Check spool directory directly (when running on server)
        if otp_spool.exists() and otp_spool.is_dir():
            try:
                files = sorted(
                    [f for f in otp_spool.glob("*.json")
                     if not f.name.endswith(".yonosbi-consumed") and not f.name.endswith(".csp-consumed")],
                    key=lambda p: p.name,
                    reverse=True
                )
                for f in files[:10]:
                    if f.name in CONSUMED_OTPS:
                        continue
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        otp = str(data.get("otp", "")).strip()
                        if not otp or len(otp) != 6 or not otp.isdigit():
                            continue
                        ts = data.get("received_ts") or data.get("timestamp")
                        if not ts:
                            m = re.match(r"^(\d+)", f.stem)
                            ts = float(m.group(1)) / 1000.0 if m else f.stat().st_mtime
                        else:
                            ts = float(ts)
                            if ts > 1e11:
                                ts = ts / 1000.0

                        if ts >= min_ts:
                            log.info("Found relayed OTP %s from spool %s (sender: %s, age: %.1fs)",
                                     otp, f.name, data.get("from"), time.time() - ts)
                            CONSUMED_OTPS.add(f.name)
                            try:
                                f.with_name(f.name + ".csp-consumed").touch(exist_ok=True)
                            except Exception:
                                pass
                            return otp
                    except Exception:
                        continue
            except Exception:
                pass

        # 2. Check JSON record (local on server)
        if otp_json.exists():
            try:
                data = json.loads(otp_json.read_text(encoding="utf-8"))
                ts = data.get("timestamp", 0)
                otp = str(data.get("otp", "")).strip()
                if otp and len(otp) == 6 and (ts >= min_ts) and otp not in CONSUMED_OTPS:
                    log.info("Found relayed OTP %s (sender: %s, age: %.1fs)", otp, data.get("from"), time.time() - ts)
                    CONSUMED_OTPS.add(otp)
                    return otp
            except Exception:
                pass

        # Check text file (local on server)
        if otp_txt.exists():
            try:
                mtime = otp_txt.stat().st_mtime
                if mtime >= min_ts:
                    otp = otp_txt.read_text(encoding="utf-8").strip()
                    if otp and len(otp) == 6 and otp.isdigit() and otp not in CONSUMED_OTPS:
                        log.info("Found relayed OTP %s from %s", otp, otp_txt)
                        CONSUMED_OTPS.add(otp)
                        return otp
            except Exception:
                pass

        # Check remote relay server (when running on laptop / workstation)
        if relay_url:
            try:
                import urllib.request
                req = urllib.request.Request(relay_url, headers={"User-Agent": "sbi-uploader/1.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        ts = data.get("timestamp", 0)
                        otp = str(data.get("otp", "")).strip()
                        if otp and len(otp) == 6 and (ts >= min_ts) and otp not in CONSUMED_OTPS:
                            log.info("Found relayed OTP %s from %s (sender: %s, age: %.1fs)", otp, relay_url, data.get("from"), time.time() - ts)
                            CONSUMED_OTPS.add(otp)
                            return otp
            except Exception:
                pass

        time.sleep(1.5)

    return None


def open_login_page(page: Page) -> Page:
    """Handle popup window if Admin.jsp triggers one, else return page."""
    try:
        with page.expect_popup(timeout=6000) as p_info:
            page.goto(SBI_URL, wait_until="domcontentloaded")
        p = p_info.value
        p.wait_for_load_state("domcontentloaded")
        return p
    except Exception:
        if not page.url.endswith("start.do") and "Admin.jsp" in page.url:
            try:
                page.goto("https://fi.sbi.bank.in/admin/start.do", wait_until="domcontentloaded")
            except Exception:
                pass
        return page


def _otp_if_asked(page: Page) -> None:
    """SBI may ask for an OTP after the captcha."""
    otp_box = page.locator("input[name*='otp' i], [aria-label*='OTP' i], input[placeholder*='OTP' i]").first
    if not otp_box.is_visible(timeout=5000):
        try:
            otp_box = page.get_by_role("textbox", name=re.compile("Please enter the OTP|OTP", re.I)).first
        except Exception:
            pass
    if not otp_box.is_visible():
        return
    log.info("OTP prompt shown on screen")
    otp = _fetch_relayed_otp(timeout_seconds=60.0)
    if not otp:
        otp = input("Type the OTP sent by SBI and press Enter: ").strip()
    otp_box.fill(otp)
    submit_btn = page.locator("button:has-text('Submit'), input[value*='Submit' i], button:has-text('Verify')").first
    submit_btn.click()

    page.wait_for_timeout(2000)
    err_box = page.locator(".errorMessage, .error, #errorMsg, [class*='error' i], font[color='red']").first
    if err_box.is_visible(timeout=3000):
        err_text = err_box.inner_text().strip()
        if "OTP" in err_text or "match" in err_text or "invalid" in err_text.lower():
            log.warning("Portal rejected OTP: '%s'. Waiting for next fresh OTP...", err_text)
            new_otp = _fetch_relayed_otp(timeout_seconds=60.0)
            if new_otp and new_otp != otp:
                log.info("New fresh OTP received: %s. Re-submitting...", new_otp)
                otp_box.fill("")
                otp_box.fill(new_otp)
                submit_btn.click()


def _logged_in(page: Page) -> bool:
    try:
        marker = page.locator("a:has-text('Capture PVR'), a:has-text('User Services'), a:has-text('Sign Out'), iframe[name='mainframe']").first
        marker.wait_for(timeout=10000)
        return True
    except PWTimeout:
        return False


def login(page: Page) -> tuple[bool, Page]:
    """Up to MAX_LOGIN_ATTEMPTS tries; returns (success, active_page)."""
    if not EP_USERNAME or not EP_PASSWORD:
        log.error("EP_USERNAME / EP_PASSWORD are not set in .env")
        return False, page

    active_page = open_login_page(page)

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        log.info("Login attempt %d of %d", attempt, MAX_LOGIN_ATTEMPTS)
        try:
            _fill_credentials(active_page)
            captcha_val = solve_image(active_page, attempt)
            captcha_input = active_page.locator("#txtInput, input[name='captcha']").first
            captcha_input.fill(captcha_val)

            sign_in_btn = active_page.locator("button:has-text('Login'), input[value*='Login' i], button:has-text('Sign In')").first
            sign_in_btn.click()

            _otp_if_asked(active_page)
            if _logged_in(active_page):
                log.info("Logged in successfully!")
                return True, active_page

            err = active_page.locator(".errorMessage, .error, #errorMsg, [class*='error' i]")
            msg = err.first.inner_text().strip() if err.count() else "dashboard not shown"
            log.warning("Login attempt %d failed: %s", attempt, msg)
        except PWTimeout as e:
            log.warning("Login attempt %d timed out: %s", attempt, e)

        if attempt < MAX_LOGIN_ATTEMPTS:
            log.info("Refreshing the captcha image")
            active_page.locator("img[alt*='Refresh' i], a:has-text('Refresh')").first.click()

    log.error("Login failed after %d attempts", MAX_LOGIN_ATTEMPTS)
    return False, active_page


def sign_out(page: Page) -> None:
    try:
        page.get_by_role("link", name=re.compile("Sign Out|Logout", re.I)).click(timeout=5000)
        log.info("Signed out")
    except Exception as e:
        log.warning("Sign out failed: %s", e)


def open_csp(page: Page, code: str) -> Page:
    """Steps 8-16: Navigate to Capture PVR DRA IIBF AMFI and open CSP popup."""
    def _safe_dialog(d):
        try:
            log.info("SBI Dialog: '%s' (auto-dismissed)", d.message)
            d.dismiss()
        except Exception:
            pass

    page.on("dialog", _safe_dialog)

    # Wait for dashboard navigation
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    try:
        page.locator("iframe[name='mainframe'], a:has-text('User Services'), a:has-text('Sign Out')").first.wait_for(timeout=20000)
    except Exception as e:
        log.warning("Notice: Dashboard marker wait returned: %s", e)

    page.wait_for_timeout(1000)

    # 1. Click User Services
    log.info("Navigating to User Services...")
    clicked_user_services = False
    for candidate in [
        page.locator("a:has-text('User Services'), a:has-text('USER SERVICES')").first,
        page.get_by_role("link", name=re.compile("User Services", re.I)).first,
        page.locator(":has-text('User Services')").first,
        page.locator("a").nth(1),
    ]:
        try:
            if candidate.is_visible(timeout=3000):
                try:
                    candidate.hover(timeout=1000)
                except Exception:
                    pass
                candidate.click(timeout=3000)
                clicked_user_services = True
                log.info("Clicked User Services menu.")
                break
        except Exception:
            continue

    if not clicked_user_services:
        log.warning("Could not find visible candidate for User Services, attempting direct click on a.nth(1)...")
        try:
            page.locator("a").nth(1).click(timeout=5000)
        except Exception as e:
            log.error("Failed to click a.nth(1): %s", e)

    # 2. Submenu item
    log.info("Clicking submenu: Capture PVR DRA IIBF AMFI and Agreement Details...")
    submenu = page.locator("a").filter(has_text=re.compile("Capture PVR DRA IIBF AMFI and", re.I)).first
    submenu.wait_for(state="visible", timeout=12000)
    submenu.click()
    log.info("Opened Capture PVR DRA IIBF AMFI form.")

    # 3. Main content inside iframe 'mainframe' using dynamic frame_locator
    mframe = page.frame_locator("iframe[name='mainframe']")
    mframe.get_by_role("combobox").select_option("01")
    mframe.locator("button:has-text('Submit'), input[value*='Submit' i]").first.click()

    page.wait_for_timeout(2000)

    # 4. Check Query CSP Code
    mframe.locator("input[type='radio'][value*='CSP' i], input[type='radio']").first.check()
    try:
        mframe.get_by_role("radio", name=re.compile("Query CSP Code", re.I)).check(timeout=3000)
    except Exception:
        pass

    # 5. Fill CSP Code
    csp_box = mframe.locator("#stReferenceNo, input[name='stReferenceNo'], input[id*='Ref' i]").first
    if not csp_box.is_visible(timeout=3000):
        csp_box = mframe.get_by_role("textbox", name=re.compile("Enter CSP code|CSP", re.I)).first
    csp_box.fill(code)

    # 6. Click Query
    log.info("Querying CSP Code: %s...", code)
    query_btn = mframe.locator("input[value*='Query' i], button:has-text('Query')").first
    if not query_btn.is_visible(timeout=3000):
        query_btn = mframe.get_by_role("button", name=re.compile(r"^\s*Query", re.I)).first
    query_btn.click()

    page.wait_for_timeout(3000)

    # Save state screenshot immediately after query
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        query_shot = SCREENSHOT_DIR / f"query_state_{code}_{int(time.time())}.png"
        page.screenshot(path=str(query_shot), full_page=True)
        log.info("Query state screenshot saved: %s", query_shot)
    except Exception:
        pass

    # Check for any portal messages in mainframe
    try:
        err_el = mframe.locator(".errorMessage, .error, #errorMsg, [class*='error' i], font[color='red'], td:has-text('pending for Authorization'), td:has-text('No record')").first
        if err_el.is_visible(timeout=1500):
            err_msg = err_el.inner_text().strip()
            log.warning("Portal message after Query: '%s'", err_msg)
            if "pending for authorization" in err_msg.lower():
                raise RuntimeError(f"CSP {code} details are already submitted and pending authorization by SBI branch.")
            if "no record" in err_msg.lower():
                raise RuntimeError(f"No records found for CSP {code} on SBI portal.")
    except RuntimeError:
        raise
    except Exception:
        pass

    # 7. Select radio in the search results
    log.info("Waiting for query results for CSP %s...", code)
    csp_direct_link = mframe.locator(f"a:has-text('{code}')").first
    result_row = mframe.locator("tr").filter(has_text=re.compile(code, re.I)).first
    result_radio = mframe.locator("table tr input[type='radio']").first

    try:
        for _ in range(20):
            if csp_direct_link.is_visible() or result_row.is_visible() or result_radio.is_visible():
                break
            page.wait_for_timeout(500)

        if result_row.is_visible():
            log.info("CSP %s row found in search results!", code)
            row_radio = result_row.locator("input[type='radio']").first
            if row_radio.is_visible(timeout=2000):
                row_radio.check()
        elif result_radio.is_visible():
            result_radio.check()

        # 8. Open CSP details popup
        with page.expect_popup(timeout=TIMEOUT_MS) as pop:
            if csp_direct_link.is_visible(timeout=3000):
                csp_direct_link.click()
            elif result_row.is_visible() and result_row.locator("a").first.is_visible(timeout=2000):
                result_row.locator("a").first.click()
            else:
                mframe.locator("table a").first.click()

        popup = pop.value
        popup.wait_for_load_state("domcontentloaded")
        log.info("CSP %s: details popup opened", code)
        return popup
    except Exception as e:
        log.warning("Could not open CSP popup: %s", e)
        # Fallback direct click
        with page.expect_popup(timeout=TIMEOUT_MS) as pop:
            mframe.locator(f"a:has-text('{code}')").first.click()
        popup = pop.value
        popup.wait_for_load_state("domcontentloaded")
        log.info("CSP %s: details popup opened via fallback", code)
        return popup


def _set_date_field(p: Page, field_type: str, dt: date) -> None:
    """Set date field via DOM evaluation and interactive datepicker."""
    date_str = dt.strftime("%d/%m/%Y")
    month_name = dt.strftime("%B")
    day_str = str(dt.day)
    year_str = str(dt.year)

    btn = None
    if field_type == "pvr":
        btn = p.get_by_role("button", name="Pick a Date").first
    elif field_type == "agreement_start":
        btn = p.get_by_role("row", name=re.compile("Agreement start date", re.I)).get_by_role("button").first
    elif field_type == "agreement_end":
        btn = p.get_by_role("row", name=re.compile("Agreement end date", re.I)).get_by_role("button").first
    elif field_type == "iibf":
        btn = p.get_by_role("row", name=re.compile("Date of Issuance", re.I)).get_by_role("button").first

    if not btn or not btn.is_visible():
        btn = p.locator("button:has-text('Pick a Date'), img[alt*='calendar' i]").first

    if btn and btn.is_visible():
        try:
            row_input = btn.locator("xpath=./preceding::input[1]")
            if row_input.is_visible():
                row_input.evaluate(f"el => {{ el.value = '{date_str}'; el.removeAttribute('readonly'); el.dispatchEvent(new Event('change')); }}")
        except Exception:
            pass

        try:
            btn.click()
            p.wait_for_timeout(300)
            span_year = p.locator("#spanYear")
            if span_year.is_visible():
                span_year.click()
                year_cell = p.get_by_role("cell", name=year_str, exact=True)
                if not year_cell.is_visible():
                    for _ in range(5):
                        prev_cell = p.get_by_role("cell", name="-")
                        if prev_cell.is_visible():
                            prev_cell.click()
                        if p.get_by_role("cell", name=year_str, exact=True).is_visible():
                            break
                if p.get_by_role("cell", name=year_str, exact=True).is_visible():
                    p.get_by_role("cell", name=year_str, exact=True).click()

            span_month = p.locator("#spanMonth")
            if span_month.is_visible():
                span_month.click()
            elif p.locator("#changeMonth").is_visible():
                p.locator("#changeMonth").click()

            month_cell = p.get_by_role("cell", name=month_name)
            if month_cell.is_visible():
                month_cell.click()

            day_link = p.get_by_role("link", name=day_str, exact=True)
            if day_link.is_visible():
                day_link.click()
        except Exception as e:
            log.warning("Datepicker interaction for %s: %s", field_type, e)


def fill_pvr(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("link", name="Police Verification Details").click()
    p.locator("select[name='stPVRFlag']").select_option("Y")
    auth_input = p.get_by_role("textbox", name=re.compile("Enter the Name of PVR Issuing", re.I)).first
    authority = csp.state or "Police Department"
    if "police" not in authority.lower():
        authority = f"{authority} Police"
    auth_input.fill(authority)
    _set_date_field(p, "pvr", d.issue_date)
    p.locator("input[type='file']").first.set_input_files(str(d.file))


def fill_agreement(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("link", name="CSP Agreement Details").click()
    p.locator("select[name='stCSPAgreementFlag']").select_option("Y")
    sig_input = p.get_by_role("textbox", name=re.compile("Enter Name of Person who", re.I)).first
    sig_input.fill(SIGNATORY_NAME)
    _set_date_field(p, "agreement_start", d.issue_date)
    if d.expiry_date:
        _set_date_field(p, "agreement_end", d.expiry_date)
    p.locator("input[type='file']").first.set_input_files(str(d.file))


def fill_iibf(p: Page, csp: CspUpload, d: DocToUpload) -> None:
    p.get_by_role("link", name="IIBF Certificate Details").click()
    p.locator("select[name='stIIBFFlag']").select_option("Y")
    reg_input = p.get_by_role("textbox", name=re.compile("Enter the IIBF Membership", re.I)).first
    reg_input.fill(d.iibf_reg_number or "")
    _set_date_field(p, "iibf", d.issue_date)
    p.locator("input[type='file']").first.set_input_files(str(d.file))


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

            # Check confirmation checkbox
            chk = popup.locator("#check")
            if chk.is_visible():
                chk.check()

            popup.get_by_role("button", name="Submit").click()
            ok = popup.locator("text=/success|submitted|saved/i")
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
            ok, active_page = login(page)
            if not ok:
                screenshot(active_page, "login_failed")
                return 1
            try:
                for csp in todo:
                    try:
                        n = upload_csp(active_page, csp, args.submit, SessionLocal, sbi, EP_USERNAME or "sbi_upload")
                        log.info("CSP %s: done (%d submitted)", csp.code, n)
                    except Exception:
                        failed += 1
                        log.exception("CSP %s: failed; moving to the next CSP", csp.code)
                        screenshot(active_page, f"failed_{csp.code}")
                        active_page.goto(SBI_URL.rsplit("/", 1)[0] + "/", wait_until="domcontentloaded")
            finally:
                sign_out(active_page)
                browser.close()
    except Exception:
        log.exception("Unexpected failure")
        screenshot(page, "unhandled")
        return 1
    log.info("Finished: %d CSP(s), %d failed", len(todo), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
