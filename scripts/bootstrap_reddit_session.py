#!/usr/bin/env python3
"""Interactive bootstrap to seed REDDIT_COOKIE_JAR.

Auto-navigates to login, fills your credentials, submits. Only blocks for a
human if Reddit hits us with a CAPTCHA. On success, saves cookies +
localStorage to ``REDDIT_COOKIE_JAR`` and exits.

Required env: REDDIT_COOKIE_JAR. REDDIT_USERNAME and REDDIT_PASSWORD are
optional; if the saved session is missing or expired, the script prompts for
any credentials not already in the environment. REDDIT_ADS_ACCOUNT_ID (the id
in the dashboard URL: ads.reddit.com/account/<id>/dashboard) is also optional.

Usage:
    set -a; source .env; set +a
    python scripts/bootstrap_reddit_session.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from getpass import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.reddit_ad_client import (
    RedditAdClient,
    RedditBlockedError,
    RedditCaptchaRequired,
)


def _wait_for_human(prompt: str) -> None:
    print()
    print("=" * 64)
    print(prompt)
    print("Press ENTER here when done.")
    print("=" * 64)
    input()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    username = os.environ.get("REDDIT_USERNAME", "").strip()
    password = os.environ.get("REDDIT_PASSWORD", "").strip()
    cookie_jar_path = os.path.expanduser(os.environ.get("REDDIT_COOKIE_JAR", "").strip())
    ads_account_id = os.environ.get("REDDIT_ADS_ACCOUNT_ID", "").strip()
    if not cookie_jar_path:
        print("error: REDDIT_COOKIE_JAR is required.", file=sys.stderr)
        return 2

    client = RedditAdClient(
        username=username,
        password=password,
        ads_account_id=ads_account_id,
        cookie_jar_path=cookie_jar_path,
        headless=False,
    )

    try:
        if client._restore_session():
            if not client._save_session():
                print(f"error: could not save session to {cookie_jar_path}.", file=sys.stderr)
                return 1
            account = client.ads_account_id or "unknown"
            print(
                f"Cookie jar at {cookie_jar_path} already has a live Reddit "
                f"session (ads account: {account}). Already bootstrapped — "
                "refreshed and saved."
            )
            return 0

        if not username or not password:
            print("The saved Reddit session is missing or expired. Log in to refresh it.")
            try:
                if not username:
                    username = input("Reddit username: ").strip()
                if not password:
                    password = getpass("Reddit password (input hidden): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted; no new session was saved.", file=sys.stderr)
                return 130
            if not username or not password:
                print("error: a Reddit username and password are required.", file=sys.stderr)
                return 2
            client.username = username
            client.password = password

        # Otherwise log in automatically with the configured credentials. The
        # client warms up via the reddit.com homepage before the login form to
        # avoid the network-security block, so a block here is transient — back
        # off and retry rather than dropping to a manual login. A CAPTCHA is
        # the one thing that genuinely needs a human.
        for attempt in (1, 2, 3):
            try:
                client._login_via_form()
                print(f"Auto-login succeeded on attempt {attempt}.")
                break
            except RedditCaptchaRequired as exc:
                print(f"CAPTCHA blocking auto-login (attempt {attempt}): {exc}")
                _wait_for_human(
                    "Solve the CAPTCHA in the Chromium window. If it returns "
                    "you to the login page, that's fine — leave the form blank, "
                    "I'll re-fill it."
                )
                # Loop and retry.
            except RedditBlockedError as exc:
                backoff = 10 * attempt
                print(
                    f"Network-security block (attempt {attempt}): {exc} "
                    f"Backing off {backoff}s and retrying the automated flow."
                )
                time.sleep(backoff)
                # Loop and retry — no manual login.
        else:
            print(
                "error: still blocked after 3 automated attempts; giving up. "
                "The egress IP is likely flagged (VPN/datacenter) — retry from "
                "a residential connection.",
                file=sys.stderr,
            )
            return 1

        # Verify against ads.reddit.com (which redirects a logged-in session to
        # the account-scoped dashboard, letting the client discover the account
        # id), then persist.
        client.driver.get(client._ads_home_url)
        if not client._is_logged_in():
            print(
                f"warning: dashboard unreachable (url={client.driver.current_url!r}). "
                "Saving cookies anyway.",
                file=sys.stderr,
            )

        if not client._save_session():
            print(f"error: could not save session to {cookie_jar_path}.", file=sys.stderr)
            return 1
        account = client.ads_account_id or "unknown"
        print(f"Session saved to {cookie_jar_path} (ads account: {account}).")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
