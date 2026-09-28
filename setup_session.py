#!/usr/bin/env python3
"""
Interactive Sprinklr login. Opens a real browser window, you sign in and clear 2FA,
then it saves the logged-in session to ~/.netflix_sprinklr_session.json so the scrape
(sprinklr_to_asana.fetch_cases_from_dashboard) can reuse it headlessly.

Run when the scrape reports SESSION_EXPIRED:
    cd ~/Desktop/MISC/netflix-ms-sla-refresh && .venv/bin/python setup_session.py

The Sprinklr login is Netflix SSO plus a second factor, which cannot be automated, so
this step is manual. It is the only manual step in the pipeline.
"""
import os
import sys

SESSION_FILE = os.path.expanduser("~/.netflix_sprinklr_session.json")
DASHBOARD_URL = "https://netflix.sprinklr.com/social/engagement/dashboard/665a42eb0f76ce53e5fd151e"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit('Playwright missing. Run:  .venv/bin/python -m pip install playwright && '
             '.venv/bin/python -m playwright install chromium')


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
        )
        page = context.new_page()
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)

        print("\n" + "=" * 68)
        print("  A browser window opened. Sign in to Sprinklr and clear 2FA.")
        print("  Wait until the dashboard is fully loaded and you can see cases.")
        print("  Then come back here and press Enter to save the session.")
        print("=" * 68 + "\n")
        input("Press Enter once you are logged in and the dashboard is visible... ")

        url = page.url
        if "login" in url or "netflix-app.sprinklr.com" in url or "tfa" in url:
            print(f"\nStill on a login/2FA page ({url}). Not saving. Re-run once you are in.")
            browser.close()
            sys.exit(1)

        context.storage_state(path=SESSION_FILE)
        browser.close()

    try:
        size = os.path.getsize(SESSION_FILE)
    except OSError:
        size = 0
    print(f"\nSaved session to {SESSION_FILE} ({size} bytes).")
    print("Next: re-run the scrape, and update the GitHub secret so the cloud job works:")
    print('  gh secret set SPRINKLR_SESSION_JSON < ~/.netflix_sprinklr_session.json')


if __name__ == "__main__":
    main()
