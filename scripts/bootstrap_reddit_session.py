#!/usr/bin/env python3
"""Interactive bootstrap to seed REDDIT_COOKIE_JAR.

Auto-navigates to login, fills your credentials, submits. Only blocks for a
human if Reddit hits us with a CAPTCHA. On success, saves cookies +
localStorage to ``REDDIT_COOKIE_JAR`` and exits.

Required env: REDDIT_USERNAME, REDDIT_PASSWORD, REDDIT_COOKIE_JAR,
REDDIT_ADS_ACCOUNT_ID (the id in the dashboard URL:
ads.reddit.com/account/<id>/dashboard)

Usage:
    set -a; source .env; set +a
    python scripts/bootstrap_reddit_session.py
"""

from __future__ import annotations

import logging
import os
import sys
import time

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
    cookie_jar_path = os.environ.get("REDDIT_COOKIE_JAR", "").strip()
    ads_account_id = os.environ.get("REDDIT_ADS_ACCOUNT_ID", "").strip()
    if not (username and password and cookie_jar_path):
        print(
            "error: REDDIT_USERNAME, REDDIT_PASSWORD, and REDDIT_COOKIE_JAR all required.",
            file=sys.stderr,
        )
        return 2
    # ads_account_id is optional; the client auto-discovers it after login.

    client = RedditAdClient(
        username=username,
        password=password,
        ads_account_id=ads_account_id,
        cookie_jar_path=cookie_jar_path,
        headless=False,
    )

    try:
        # If the jar already restores a live session, there's nothing to do.
        if client._restore_session():
            account = client.ads_account_id or "unknown"
            print(
                f"Cookie jar at {cookie_jar_path} already has a live Reddit "
                f"session (ads account: {account}). Already bootstrapped — "
                "nothing to do."
            )
            return 0

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

        client._save_session()
        account = client.ads_account_id or "unknown"
        print(f"Session saved to {cookie_jar_path} (ads account: {account}).")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
