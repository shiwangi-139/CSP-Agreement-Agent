#!/usr/bin/env python3
"""
test_sbi_login.py
Real-time test script using the EXACT recorded SBI FI portal flow:
1. Launches headed Chromium on your laptop display
2. Opens https://fi.sbi.bank.in/admin/Admin.jsp (or start.do)
3. Fills credentials (#specialChar, #password)
4. Solves captcha with Tesseract OCR -> fills #txtInput
5. Clicks Login
6. Fetches real-time OTP from phone relay app (https://indev.eko.in/csp-agreement-agent/sbi-otp/latest)
7. Fills OTP -> submits
8. Navigates to User Services -> Capture PVR DRA IIBF AMFI and Agreement Details of CSP
9. Queries CSP in mainframe iframe and opens CSP details popup!

Usage:
    python3 sbi_upload/test_sbi_login.py
    python3 sbi_upload/test_sbi_login.py --csp 1A850077
    python3 sbi_upload/test_sbi_login.py --auto-captcha
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("sbi_login_test")

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv(HERE.parent / ".env")

DEFAULT_URL = os.getenv("SBI_URL", "https://fi.sbi.bank.in/admin/Admin.jsp")
DEFAULT_USER = os.getenv("EP_USERNAME", "")
DEFAULT_PASS = os.getenv("EP_PASSWORD", "")
DEFAULT_RELAY_URL = os.getenv("OTP_RELAY_URL", "https://indev.eko.in/csp-agreement-agent/sbi-otp/latest")
DEFAULT_CSP = os.getenv("TEST_CSP", "1A850077")
HAS_DISPLAY = bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
DEFAULT_HEADLESS = not HAS_DISPLAY or os.getenv("HEADLESS", "false").lower() == "true"
SCREENSHOT_DIR = HERE / "screenshots"


def solve_captcha_tesseract(page, img_locator, auto_accept: bool = False) -> str:
    """Take screenshot of captcha and run Tesseract OCR."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    captcha_path = SCREENSHOT_DIR / f"test_captcha_{int(time.time())}.png"
    img_locator.screenshot(path=str(captcha_path))
    log.info("Captcha image saved to: %s", captcha_path)

    ocr_text = ""
    try:
        import pytesseract
        from PIL import Image
        img_obj = Image.open(captcha_path)
        raw = pytesseract.image_to_string(
            img_obj,
            config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        ocr_text = re.sub(r"[^A-Za-z0-9]", "", raw).strip()
        log.info("Tesseract OCR result: '%s'", ocr_text)
    except Exception as e:
        log.warning("Tesseract OCR error: %s", e)

    if auto_accept and ocr_text:
        log.info("Auto-accepting Tesseract result: %s", ocr_text)
        return ocr_text

    prompt = f"Enter captcha text (Tesseract read [{ocr_text or 'none'}], press Enter to accept): "
    typed = input(prompt).strip()
    return typed if typed else ocr_text


def wait_for_relayed_otp(start_time: float, relay_url: str, timeout_seconds: int = 60) -> str:
    """Poll for the OTP received on the phone in real time (local files or relay URL)."""
    deadline = start_time + timeout_seconds
    log.info("Waiting up to %ds for real-time OTP relayed from phone...", timeout_seconds)

    otp_spool = Path(os.getenv("OTP_SPOOL_DIR", "/home/adarsh/eko/.tmp/sbi_stmt_bot/sms_inbox"))
    otp_json = Path(os.getenv("OTP_JSON_FILE", "/tmp/sbi_latest_otp.json"))
    otp_txt = Path(os.getenv("OTP_TEXT_FILE", "/tmp/sbi_latest_otp.txt"))

    while time.time() < deadline:
        # 1. Check spool directory directly (when running on server)
        if otp_spool.exists() and otp_spool.is_dir():
            try:
                files = sorted(
                    [f for f in otp_spool.glob("*.json") if not f.name.endswith(".yonosbi-consumed")],
                    key=lambda p: p.name,
                    reverse=True
                )
                for f in files[:10]:
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

                        if ts >= start_time - 30.0:
                            log.info("Real-time OTP received from spool file %s! OTP=%s, sender=%s, age=%.1fs",
                                     f.name, otp, data.get("from"), time.time() - ts)
                            return otp
                    except Exception:
                        continue
            except Exception:
                pass

        # 2. Check local files (/tmp)
        if otp_json.exists():
            try:
                data = json.loads(otp_json.read_text(encoding="utf-8"))
                ts = data.get("timestamp", 0)
                otp = str(data.get("otp", "")).strip()
                if otp and len(otp) == 6 and (ts >= start_time - 30.0):
                    log.info("Real-time OTP received from local file! OTP=%s, sender=%s, age=%.1fs",
                             otp, data.get("from"), time.time() - ts)
                    return otp
            except Exception:
                pass

        if otp_txt.exists():
            try:
                mtime = otp_txt.stat().st_mtime
                if mtime >= start_time - 30.0:
                    otp = otp_txt.read_text(encoding="utf-8").strip()
                    if otp and len(otp) == 6 and otp.isdigit():
                        log.info("Real-time OTP received from %s: %s", otp_txt, otp)
                        return otp
            except Exception:
                pass

        # Check relay URL
        try:
            req = urllib.request.Request(relay_url, headers={"User-Agent": "sbi-tester/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    ts = data.get("timestamp", 0)
                    otp = str(data.get("otp", "")).strip()
                    if otp and len(otp) == 6 and (ts >= start_time - 30.0):
                        log.info("Real-time OTP received from relay URL! OTP=%s, sender=%s, age=%.1fs",
                                 otp, data.get("from"), time.time() - ts)
                        return otp
        except Exception:
            pass

        time.sleep(1.5)

    log.warning("Timeout waiting for relayed OTP")
    return input("Relay timeout: Please type the OTP manually from your phone: ").strip()


def run_login_flow(url: str, username: str, password: str, relay_url: str, csp_code: str, auto_captcha: bool, headless: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed! Run: pip install playwright && python3 -m playwright install chromium")
        return False

    if not username or not password:
        log.error("Username or Password not set! Please check sbi_upload/.env or pass --username and --password")
        return False

    log.info("Starting headed Chromium on your laptop display...")
    log.info("URL: %s", url)
    log.info("Username: %s", username)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=200)
        videos_dir = SCREENSHOT_DIR / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 850},
            record_video_dir=str(videos_dir)
        )
        page = context.new_page()

        # Step 1: Open URL (handling popup if Admin.jsp triggers one)
        log.info("Navigating to SBI FI Portal...")
        page1 = page
        try:
            with page.expect_popup(timeout=6000) as p_info:
                page.goto(url, wait_until="domcontentloaded")
            page1 = p_info.value
            page1.wait_for_load_state("domcontentloaded")
            log.info("Popup login window opened.")
        except Exception:
            if not page.url.endswith("start.do") and "Admin.jsp" in page.url:
                try:
                    page.goto("https://fi.sbi.bank.in/admin/start.do", wait_until="domcontentloaded")
                except Exception:
                    pass

        # Step 2: Fill Username & Password
        log.info("Filling credentials...")
        user_field = page1.locator("#specialChar, input[name='userName']").first
        user_field.wait_for(state="visible", timeout=15000)
        user_field.fill(username)

        pass_field = page1.locator("#password, input[type='password']").first
        pass_field.fill(password)

        # Step 3: Solve Captcha
        captcha_img = page1.locator("img#captchaImage, img[src*='captcha' i], img[id*='captcha' i]").first
        if not captcha_img.is_visible():
            captcha_img = page1.locator("img").first

        captcha_val = solve_captcha_tesseract(page1, captcha_img, auto_accept=auto_captcha)
        captcha_input = page1.locator("#txtInput, input[name='captcha']").first
        captcha_input.fill(captcha_val)
        log.info("Captcha filled.")

        # Step 4: Click Login
        start_wait_otp = time.time()
        login_btn = page1.locator("button:has-text('Login'), input[value*='Login' i], button:has-text('Sign In')").first
        login_btn.click()
        log.info("Login clicked. Checking for OTP prompt...")

        page1.wait_for_timeout(2000)

        # Step 5: OTP Check
        otp_box = page1.locator("input[name*='otp' i], [aria-label*='OTP' i], input[placeholder*='OTP' i]").first
        if not otp_box.is_visible(timeout=5000):
            try:
                otp_box = page1.get_by_role("textbox", name=re.compile("Please enter the OTP|OTP", re.I)).first
            except Exception:
                pass

        if otp_box.is_visible():
            log.info("OTP prompt detected on screen!")
            otp_val = wait_for_relayed_otp(start_wait_otp, relay_url)
            log.info("Filling OTP: %s", otp_val)
            otp_box.fill(otp_val)

            submit_btn = page1.get_by_role("button", name="Submit").first
            submit_btn.click()
            log.info("OTP submitted.")

        page1.wait_for_timeout(3000)

        # Step 6: Navigate to User Services
        log.info("Navigating to User Services...")
        try:
            page1.locator("a").nth(1).click()
        except Exception:
            page1.get_by_role("button", name="User Services").click()

        page1.locator("a").filter(has_text=re.compile("Capture PVR DRA IIBF AMFI and", re.I)).first.click()
        log.info("Opened Capture PVR DRA IIBF AMFI and Agreement Details form.")

        page1.wait_for_timeout(2000)

        # Step 7: Mainframe iframe interaction
        frame = page1.locator("iframe[name='mainframe']").content_frame
        frame.get_by_role("combobox").select_option("01")
        frame.get_by_role("button", name="Submit").click()

        page1.wait_for_timeout(1000)

        frame.get_by_role("radio", name="Query CSP Code").check()
        csp_box = frame.locator("#stReferenceNo")
        if not csp_box.is_visible():
            csp_box = frame.get_by_role("textbox", name=re.compile("Enter CSP code", re.I)).first
        csp_box.fill(csp_code)

        log.info("Querying CSP Code: %s...", csp_code)
        frame.get_by_role("button", name=re.compile(r"^\s*Query", re.I)).click()

        page1.wait_for_timeout(2000)

        # Step 8: Select result row and open details popup
        radio = frame.get_by_role("radio").first
        if radio.is_visible(timeout=8000):
            radio.check()
            log.info("CSP row found! Opening details popup...")
            with page1.expect_popup(timeout=10000) as p2_info:
                frame.get_by_role("link", name=csp_code).click()
            page2 = p2_info.value
            page2.wait_for_load_state("domcontentloaded")
            log.info("SUCCESS: CSP %s details popup opened successfully!", csp_code)

            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            shot_path = SCREENSHOT_DIR / f"csp_details_{csp_code}_{int(time.time())}.png"
            page2.screenshot(path=str(shot_path))
            log.info("Screenshot saved: %s", shot_path)

            log.info("Keeping browser open for 15 seconds so you can see the active forms...")
            page1.wait_for_timeout(15000)

            page2.close()
        else:
            log.warning("No search results returned for CSP %s", csp_code)
            page1.wait_for_timeout(10000)

        # Sign out
        try:
            page1.once("dialog", lambda dialog: dialog.dismiss())
            page1.get_by_role("link", name="Sign Out").click()
            log.info("Signed out.")
        except Exception:
            pass

        try:
            video_obj = page.video or page1.video
            context.close()
            if video_obj:
                log.info("Full video recording saved: %s", video_obj.path())
        except Exception:
            pass

        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real-time SBI FI Portal Login & Flow Tester")
    parser.add_argument("--url", default=DEFAULT_URL, help="SBI Login URL")
    parser.add_argument("--username", default=DEFAULT_USER, help="SBI Username")
    parser.add_argument("--password", default=DEFAULT_PASS, help="SBI Password")
    parser.add_argument("--relay-url", default=DEFAULT_RELAY_URL, help="OTP Relay URL")
    parser.add_argument("--csp", default=DEFAULT_CSP, help="CSP Code to query (default: 1A850077)")
    parser.add_argument("--auto-captcha", action="store_true", help="Auto-fill Tesseract captcha without prompt")
    parser.add_argument("--headless", action="store_true", default=DEFAULT_HEADLESS, help="Run in headless mode")
    parser.add_argument("--headed", action="store_true", help="Force visible browser window (requires X11 or Wayland)")

    args = parser.parse_args()

    use_headless = False if args.headed else args.headless

    ok = run_login_flow(
        url=args.url,
        username=args.username,
        password=args.password,
        relay_url=args.relay_url,
        csp_code=args.csp,
        auto_captcha=args.auto_captcha,
        headless=use_headless,
    )
    sys.exit(0 if ok else 1)
