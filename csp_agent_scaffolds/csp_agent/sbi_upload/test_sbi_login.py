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
    """Take screenshot of captcha and run Tesseract OCR with image preprocessing."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    captcha_path = SCREENSHOT_DIR / f"test_captcha_{int(time.time())}.png"
    img_locator.screenshot(path=str(captcha_path))
    log.info("Captcha image saved to: %s", captcha_path)

    ocr_text = ""
    try:
        import pytesseract
        from PIL import Image, ImageEnhance
        img_obj = Image.open(captcha_path)

        # Preprocessing: upscale 2x and enhance contrast for crisp characters
        w, h = img_obj.size
        img_scaled = img_obj.resize((w * 2, h * 2), Image.Resampling.LANCZOS).convert("L")
        enhanced = ImageEnhance.Contrast(img_scaled).enhance(2.0)

        raw = pytesseract.image_to_string(
            enhanced,
            config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        ocr_text = re.sub(r"[^A-Za-z0-9]", "", raw).strip()

        # Fallback to direct raw if enhanced produced unexpected length
        if len(ocr_text) != 6:
            raw_orig = pytesseract.image_to_string(
                img_obj,
                config="--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )
            clean_orig = re.sub(r"[^A-Za-z0-9]", "", raw_orig).strip()
            if len(clean_orig) == 6 or not ocr_text:
                ocr_text = clean_orig

        log.info("Tesseract OCR result: '%s'", ocr_text)
    except Exception as e:
        log.warning("Tesseract OCR error: %s", e)

    if auto_accept and ocr_text:
        log.info("Auto-accepting Tesseract result: %s", ocr_text)
        return ocr_text

    prompt = f"Enter captcha text (Tesseract read [{ocr_text or 'none'}], press Enter to accept): "
    typed = input(prompt).strip()
    return typed if typed else ocr_text


CONSUMED_OTPS: set[str] = set()


def wait_for_relayed_otp(start_time: float, relay_url: str, timeout_seconds: int = 60) -> str:
    """Poll for the OTP received on the phone in real time (local files or relay URL)."""
    deadline = start_time + timeout_seconds
    min_ts = start_time - 1.5  # Accept OTP generated strictly for this login attempt
    log.info("Waiting up to %ds for real-time OTP relayed from phone (must be after %s)...",
             timeout_seconds, time.strftime("%H:%M:%S", time.localtime(start_time)))

    otp_spool = Path(os.getenv("OTP_SPOOL_DIR", "/home/adarsh/eko/.tmp/sbi_stmt_bot/sms_inbox"))
    otp_json = Path(os.getenv("OTP_JSON_FILE", "/tmp/sbi_latest_otp.json"))
    otp_txt = Path(os.getenv("OTP_TEXT_FILE", "/tmp/sbi_latest_otp.txt"))

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
                            log.info("Real-time OTP received from spool file %s! OTP=%s, sender=%s, age=%.1fs",
                                     f.name, otp, data.get("from"), time.time() - ts)
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

        # 2. Check local files (/tmp)
        if otp_json.exists():
            try:
                data = json.loads(otp_json.read_text(encoding="utf-8"))
                ts = data.get("timestamp", 0)
                otp = str(data.get("otp", "")).strip()
                if otp and len(otp) == 6 and (ts >= min_ts) and otp not in CONSUMED_OTPS:
                    log.info("Real-time OTP received from local file! OTP=%s, sender=%s, age=%.1fs",
                             otp, data.get("from"), time.time() - ts)
                    CONSUMED_OTPS.add(otp)
                    return otp
            except Exception:
                pass

        if otp_txt.exists():
            try:
                mtime = otp_txt.stat().st_mtime
                if mtime >= min_ts:
                    otp = otp_txt.read_text(encoding="utf-8").strip()
                    if otp and len(otp) == 6 and otp.isdigit() and otp not in CONSUMED_OTPS:
                        log.info("Real-time OTP received from %s: %s", otp_txt, otp)
                        CONSUMED_OTPS.add(otp)
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
                    if otp and len(otp) == 6 and (ts >= min_ts) and otp not in CONSUMED_OTPS:
                        log.info("Real-time OTP received from relay URL! OTP=%s, sender=%s, age=%.1fs",
                                 otp, data.get("from"), time.time() - ts)
                        CONSUMED_OTPS.add(otp)
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

        # Register dialog handler to auto-dismiss any SBI alert or confirmation popups
        page1.on("dialog", lambda d: (log.info("SBI Dialog: '%s' (auto-dismissed)", d.message), d.dismiss()))
        page.on("dialog", lambda d: (log.info("Parent Dialog: '%s' (auto-dismissed)", d.message), d.dismiss()))

        try:
            # Step 2-4: Login attempts (with automatic captcha refresh on error)
            max_captcha_attempts = 3
            otp_box = None
            start_wait_otp = time.time()
            login_success = False

            for attempt in range(1, max_captcha_attempts + 1):
                log.info("Login attempt %d of %d...", attempt, max_captcha_attempts)

                # Ensure username and password are filled
                user_field = page1.locator("#specialChar, input[name='userName']").first
                user_field.wait_for(state="visible", timeout=15000)
                user_field.fill("")
                user_field.fill(username)

                pass_field = page1.locator("#password, input[type='password']").first
                pass_field.fill("")
                pass_field.fill(password)

                # Step 3: Solve Captcha
                captcha_img = page1.locator("img#captchaImage, img[src*='captcha' i], img[id*='captcha' i]").first
                if not captcha_img.is_visible():
                    captcha_img = page1.locator("img").first

                captcha_val = solve_captcha_tesseract(page1, captcha_img, auto_accept=auto_captcha)
                captcha_input = page1.locator("#txtInput, input[name='captcha']").first
                captcha_input.fill("")
                captcha_input.fill(captcha_val)
                log.info("Captcha filled with: %s", captcha_val)

                # Step 4: Click Login
                start_wait_otp = time.time()
                login_btn = page1.locator("button:has-text('Login'), input[value*='Login' i], button:has-text('Sign In')").first
                login_btn.click()
                log.info("Login clicked. Checking response...")

                page1.wait_for_timeout(2000)

                # Check if captcha error was shown
                err_box = page1.locator(".errorMessage, .error, #errorMsg, [class*='error' i], font[color='red']").first
                if err_box.is_visible(timeout=2000):
                    err_text = err_box.inner_text().strip()
                    if "captcha" in err_text.lower():
                        log.warning("Captcha rejected ('%s'). Refreshing captcha for attempt %d...", err_text, attempt + 1)
                        if attempt < max_captcha_attempts:
                            refresh_btn = page1.locator("img[alt*='Refresh' i], a:has-text('Refresh'), img[src*='refresh' i], [title*='Refresh' i]").first
                            if refresh_btn.is_visible(timeout=3000):
                                refresh_btn.click()
                                page1.wait_for_timeout(1500)
                            continue
                        else:
                            log.error("Max login attempts reached on captcha. Aborting.")
                            return False
                    elif "user" in err_text.lower() or "password" in err_text.lower() or "blocked" in err_text.lower():
                        log.error("Portal login credentials error: %s", err_text)
                        return False

                # Check if OTP prompt appeared or dashboard loaded
                otp_box = page1.locator("input[name*='otp' i], [aria-label*='OTP' i], input[placeholder*='OTP' i]").first
                if not otp_box.is_visible(timeout=4000):
                    try:
                        otp_box = page1.get_by_role("textbox", name=re.compile("Please enter the OTP|OTP", re.I)).first
                    except Exception:
                        pass

                if (otp_box and otp_box.is_visible()) or page1.locator("iframe[name='mainframe'], text='User Services'").first.is_visible():
                    login_success = True
                    break

            if not login_success and (not otp_box or not otp_box.is_visible()):
                log.error("Could not reach OTP screen or dashboard after %d login attempts.", max_captcha_attempts)
                return False

            if otp_box and otp_box.is_visible():
                log.info("OTP prompt detected on screen!")
                otp_val = wait_for_relayed_otp(start_wait_otp, relay_url)
                log.info("Filling OTP: %s", otp_val)
                otp_box.fill(otp_val)

                submit_btn = page1.get_by_role("button", name="Submit").first
                submit_btn.click()
                log.info("OTP submitted. Checking if accepted...")

                page1.wait_for_timeout(2000)

                # Check if OTP was rejected
                err_box = page1.locator(".errorMessage, .error, #errorMsg, [class*='error' i], font[color='red']").first
                if err_box.is_visible(timeout=3000):
                    err_text = err_box.inner_text().strip()
                    if "OTP" in err_text or "match" in err_text or "invalid" in err_text.lower():
                        log.warning("Portal rejected OTP: '%s'. Waiting for next fresh OTP from phone...", err_text)
                        retry_start = time.time()
                        new_otp = wait_for_relayed_otp(retry_start, relay_url, timeout_seconds=60)
                        if new_otp and new_otp != otp_val:
                            log.info("New fresh OTP received: %s. Re-submitting...", new_otp)
                            otp_box.fill("")
                            otp_box.fill(new_otp)
                            submit_btn.click()
                            page1.wait_for_timeout(2500)
                        else:
                            log.error("Could not obtain a newer OTP or relay timed out. Aborting.")
                            return False

            # Wait for portal navigation and dashboard elements
            try:
                page1.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            # Check if there is still an error banner visible
            err_box = page1.locator(".errorMessage, .error, #errorMsg, [class*='error' i], font[color='red']").first
            if err_box.is_visible(timeout=2000):
                err_text = err_box.inner_text().strip()
                if err_text:
                    log.error("Portal error displayed: %s. Aborting.", err_text)
                    return False

            # Wait for dashboard marker
            log.info("Waiting for dashboard to be ready...")
            try:
                page1.locator("iframe[name='mainframe'], a:has-text('User Services'), a:has-text('Sign Out')").first.wait_for(timeout=20000)
                log.info("Dashboard loaded.")
            except Exception as e:
                log.error("Timeout waiting for dashboard marker: %s. Aborting.", e)
                return False

            page1.wait_for_timeout(1000)

            # Step 6: Navigate to User Services
            log.info("Navigating to User Services...")
            clicked_user_services = False
            for candidate in [
                page1.locator("a:has-text('User Services'), a:has-text('USER SERVICES')").first,
                page1.get_by_role("link", name=re.compile("User Services", re.I)).first,
                page1.locator("text='User Services'").first,
                page1.locator("a").nth(1),
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
                    page1.locator("a").nth(1).click(timeout=5000)
                except Exception as e:
                    log.error("Failed to click a.nth(1): %s", e)

            # Click submenu item: Capture PVR DRA IIBF AMFI and Agreement Details
            log.info("Clicking submenu: Capture PVR DRA IIBF AMFI and Agreement Details...")
            capture_link = page1.locator("a").filter(has_text=re.compile("Capture PVR DRA IIBF AMFI and", re.I)).first
            capture_link.wait_for(state="visible", timeout=12000)
            capture_link.click()
            log.info("Opened Capture PVR DRA IIBF AMFI and Agreement Details form.")

            page1.wait_for_timeout(2000)

            # Step 7: Mainframe iframe interaction using dynamic frame_locator
            log.info("Selecting request type in mainframe...")
            mframe = page1.frame_locator("iframe[name='mainframe']")
            mframe.get_by_role("combobox").select_option("01")
            mframe.locator("button:has-text('Submit'), input[value*='Submit' i]").first.click()

            page1.wait_for_timeout(2000)

            # Check Query CSP Code radio
            log.info("Selecting Query CSP Code radio...")
            mframe.locator("input[type='radio'][value*='CSP' i], input[type='radio']").first.check()
            try:
                mframe.get_by_role("radio", name=re.compile("Query CSP Code", re.I)).check(timeout=3000)
            except Exception:
                pass

            # Fill CSP Code
            csp_box = mframe.locator("#stReferenceNo, input[name='stReferenceNo'], input[id*='Ref' i]").first
            if not csp_box.is_visible(timeout=3000):
                csp_box = mframe.get_by_role("textbox", name=re.compile("Enter CSP code|CSP", re.I)).first
            csp_box.fill(csp_code)

            # Click Query button (handles "  Query", "Query", input or button)
            log.info("Querying CSP Code: %s...", csp_code)
            query_btn = mframe.locator("input[value*='Query' i], button:has-text('Query')").first
            if not query_btn.is_visible(timeout=3000):
                query_btn = mframe.get_by_role("button", name=re.compile("Query", re.I)).first
            query_btn.click()

            page1.wait_for_timeout(2000)

            # Step 8: Select result row and open details popup
            log.info("Waiting for query results for CSP %s...", csp_code)
            result_row = mframe.locator("tr").filter(has_text=re.compile(csp_code, re.I)).first
            try:
                result_row.wait_for(state="visible", timeout=20000)
                log.info("CSP row for %s found in search results!", csp_code)

                # Check the radio button in that row if present
                row_radio = result_row.locator("input[type='radio']").first
                if row_radio.is_visible(timeout=3000):
                    row_radio.check()
                    log.info("Checked result row radio button.")

                # Click the CSP link in that row to trigger the popup
                with page1.expect_popup(timeout=15000) as p2_info:
                    csp_link = result_row.locator("a").filter(has_text=re.compile(csp_code, re.I)).first
                    if not csp_link.is_visible(timeout=3000):
                        csp_link = result_row.locator("a").first
                    csp_link.click()

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
            except Exception as e_row:
                log.warning("Could not find CSP result row or popup failed: %s", e_row)
                # Fallback to direct link anywhere in mframe
                try:
                    with page1.expect_popup(timeout=10000) as p2_info:
                        mframe.locator(f"a:has-text('{csp_code}')").first.click(timeout=5000)
                    page2 = p2_info.value
                    page2.wait_for_load_state("domcontentloaded")
                    log.info("SUCCESS via fallback: CSP %s details popup opened!", csp_code)
                    page2.close()
                except Exception:
                    pass

            # Sign out
            try:
                page1.once("dialog", lambda dialog: dialog.dismiss())
                page1.get_by_role("link", name="Sign Out").click()
                log.info("Signed out.")
            except Exception:
                pass

            return True

        except Exception as exc:
            log.error("Flow execution failed: %s", exc, exc_info=True)
            try:
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                fail_shot = SCREENSHOT_DIR / f"failure_{int(time.time())}.png"
                page1.screenshot(path=str(fail_shot), full_page=True)
                log.info("Saved failure screenshot to: %s", fail_shot)
                fail_html = SCREENSHOT_DIR / f"failure_{int(time.time())}.html"
                fail_html.write_text(page1.content(), encoding="utf-8")
                log.info("Saved failure HTML to: %s", fail_html)
            except Exception as e_shot:
                log.warning("Could not save failure artifacts: %s", e_shot)
            return False

        finally:
            try:
                video_obj = page.video or page1.video
                context.close()
                if video_obj:
                    log.info("Full video recording saved: %s", video_obj.path())
            except Exception:
                pass


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
