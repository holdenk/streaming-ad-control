#!/usr/bin/env python3
"""Interactive bootstrap to seed REDDIT_COOKIE_JAR.

Auto-navigates to login, fills your credentials, submits. Only blocks for a
human if Reddit hits us with a CAPTCHA. On success, saves cookies +
localStorage to ``REDDIT_COOKIE_JAR`` and exits.

Required env: REDDIT_USERNAME, REDDIT_PASSWORD, REDDIT_COOKIE_JAR

Usage:
    set -a; source .env; set +a
    python scripts/bootstrap_reddit_session.py
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.reddit_ad_client import RedditAdClient, RedditCaptchaRequired


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
    if not (username and password and cookie_jar_path):
        print(
            "error: REDDIT_USERNAME, REDDIT_PASSWORD, and REDDIT_COOKIE_JAR all required.",
            file=sys.stderr,
        )
        return 2

    client = RedditAdClient(
        username=username,
        password=password,
        cookie_jar_path=cookie_jar_path,
        headless=False,
    )

    try:
        # Try to auto-login. If we hit a CAPTCHA we ask the human and retry.
        for attempt in (1, 2, 3):
            try:
                client._login_via_form()
                print(f"Auto-login succeeded on attempt {attempt}.")
                break
            except RedditCaptchaRequired as exc:
                print(f"CAPTCHA blocking auto-login: {exc}")
                _wait_for_human(
                    "Solve the CAPTCHA in the Chromium window. If it returns "
                    "you to the login page, that's fine — leave the form blank, "
                    "I'll re-fill it."
                )
                # Loop and retry.
        else:
            print("error: still blocked after 3 CAPTCHA attempts; giving up.", file=sys.stderr)
            return 1

        if not client._is_logged_in():
            print(
                f"warning: dashboard unreachable (url={client.driver.current_url!r}). "
                "Saving cookies anyway.",
                file=sys.stderr,
            )

        client._save_session()
        print(f"Session saved to {cookie_jar_path}.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
