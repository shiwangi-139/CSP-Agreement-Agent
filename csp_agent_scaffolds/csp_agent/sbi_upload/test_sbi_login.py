#!/usr/bin/env python3
"""
test_sbi_login.py
Standalone test script for verifying:
1. Browser launch on laptop (headed/visible window)
2. Auto-filling credentials
3. Captcha extraction using Tesseract OCR
4. Real-time OTP retrieval from the mobile relay (via https://indev.eko.in/csp-agreement-agent/sbi-otp/latest)
5. Successful login to SBI FI portal and dashboard visibility

Usage:
    python3 sbi_upload/test_sbi_login.py
    python3 sbi_upload/test_sbi_login.py --auto-captcha
    python3 sbi_upload/test_sbi_login.py --username <USER> --password <PASS>
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
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("sbi_login_test")

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv(HERE.parent / ".env")

DEFAULT_URL = os.getenv("SBI_URL", "https://fi.sbi.bank.in/unifyapp/login/")
DEFAULT_USER = os.getenv("EP_USERNAME", "")
DEFAULT_PASS = os.getenv("EP_PASSWORD", "")
DEFAULT_RELAY_URL = os.getenv("OTP_RELAY_URL", "https://indev.eko.in/csp-agreement-agent/sbi-otp/latest")
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
        log.warning("Tesseract OCR failed: %s", e)

    if auto_accept and ocr_text:
        log.info("Auto-accepting Tesseract result: %s", ocr_text)
        return ocr_text

    prompt = f"Enter captcha text (Tesseract read [{ocr_text or 'none'}], press Enter to accept): "
    typed = input(prompt).strip()
    return typed if typed else ocr_text


def wait_for_relayed_otp(start_time: float, relay_url: str, timeout_seconds: int = 60) -> str:
    """Poll the relay server for the OTP received on the phone in real time."""
    deadline = start_time + timeout_seconds
    log.info("Waiting up to %ds for real-time OTP relayed from phone via %s...", timeout_seconds, relay_url)

    while time.time() < deadline:
        try:
            req = urllib.request.Request(relay_url, headers={"User-Agent": "sbi-tester/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    ts = data.get("timestamp", 0)
                    otp = str(data.get("otp", "")).strip()
                    # Check if OTP is fresh (received after or up to 30s before waiting started)
                    if otp and len(otp) == 6 and (ts >= start_time - 30.0):
                        log.info("Real-time OTP received! OTP=%s, sender=%s, age=%.1fs",
                                 otp, data.get("from"), time.time() - ts)
                        return otp
        except Exception:
            pass
        time.sleep(1.5)

    log.warning("Timeout waiting for relayed OTP")
    return input("Relay timeout: Please type the OTP manually from your phone: ").strip()


def run_login_test(url: str, username: str, password: str, relay_url: str, auto_captcha: bool, headless: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed! Run: pip install playwright && python3 -m playwright install chromium")
        return False

    if not username or not password:
        log.error("Username or Password not provided! Set EP_USERNAME and EP_PASSWORD in sbi_upload/.env or pass --username and --password")
        return False

    log.info("Starting headed browser on laptop screen...")
    log.info("SBI URL: %s", url)
    log.info("Username: %s", username)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=300)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        log.info("Opening SBI FI Portal...")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        # 1. Fill credentials
        user_input = page.locator("input[name='userName'], input[id*='user' i], input[type='text']").first
        pass_input = page.locator("input[name='password'], input[type='password']").first

        user_input.fill(username)
        pass_input.fill(password)
        log.info("Credentials filled.")

        # 2. Captcha
        captcha_img = page.locator("img#captchaImage, img[alt*='captcha' i], img[src*='captcha' i]").first
        if captcha_img.is_visible(timeout=5000):
            captcha_text = solve_captcha_tesseract(page, captcha_img, auto_accept=auto_captcha)
            captcha_input = page.locator("input[name='captcha'], input[id*='captcha' i]").first
            captcha_input.fill(captcha_text)
            log.info("Captcha filled with: %s", captcha_text)

        # 3. Click Submit
        sign_in_btn = page.locator("button:has-text('Sign In'), button:has-text('Login'), input[type='submit']").first
        start_wait_otp = time.time()
        sign_in_btn.click()
        log.info("Sign In clicked. Waiting for next page...")

        page.wait_for_timeout(2000)

        # 4. Check for OTP prompt
        otp_input = page.locator("input[name='otp'], input[id*='otp' i]").first
        if otp_input.is_visible(timeout=6000):
            log.info("OTP field detected on screen!")
            otp_val = wait_for_relayed_otp(start_wait_otp, relay_url)
            log.info("Entering OTP: %s", otp_val)
            otp_input.fill(otp_val)

            verify_btn = page.locator("button:has-text('Verify'), button:has-text('Submit'), input[value*='Verify' i]").first
            verify_btn.click()
            log.info("OTP submitted.")

        page.wait_for_timeout(3000)

        # 5. Check if dashboard loaded
        user_services = page.locator("text='User Services'")
        if user_services.is_visible(timeout=10000):
            log.info("SUCCESS! Logged into SBI FI Portal! Dashboard is open and visible on your laptop screen.")
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            success_shot = SCREENSHOT_DIR / f"dashboard_success_{int(time.time())}.png"
            page.screenshot(path=str(success_shot))
            log.info("Dashboard screenshot saved: %s", success_shot)
            log.info("Keeping browser open for 15 seconds so you can see it...")
            page.wait_for_timeout(15000)
            return True
        else:
            log.warning("User Services not detected. Current URL: %s", page.url)
            err_shot = SCREENSHOT_DIR / f"login_attempt_{int(time.time())}.png"
            page.screenshot(path=str(err_shot))
            log.info("Screenshot saved: %s", err_shot)
            return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test SBI FI Portal login with Tesseract and Mobile OTP Relay")
    parser.add_argument("--url", default=DEFAULT_URL, help="SBI Login URL")
    parser.add_argument("--username", default=DEFAULT_USER, help="SBI Username")
    parser.add_argument("--password", default=DEFAULT_PASS, help="SBI Password")
    parser.add_argument("--relay-url", default=DEFAULT_RELAY_URL, help="OTP Relay URL")
    parser.add_argument("--auto-captcha", action="store_true", help="Auto-fill Tesseract captcha without prompting")
    parser.add_argument("--headless", action="store_true", help="Run headless instead of visible window")

    args = parser.parse_args()

    success = run_login_test(
        url=args.url,
        username=args.username,
        password=args.password,
        relay_url=args.relay_url,
        auto_captcha=args.auto_captcha,
        headless=args.headless,
    )
    sys.exit(0 if success else 1)
