#!/usr/bin/env python3
"""End-to-end smoke test for the Reddit ads client.

Spins up the headless Chromium, logs in (or restores cached session), and runs
disable -> enable -> disable on the given campaign. Prints each result. Leaves
the campaign PAUSED so it's safe to re-run.

Reads the same env vars the daemon does:
  REDDIT_USERNAME, REDDIT_PASSWORD            (required)
  REDDIT_COOKIE_JAR                           (optional, persists session)
  REDDIT_PATCH_BODY_PAUSE                     (optional, override default body)
  REDDIT_PATCH_BODY_RESUME                    (optional)

Usage:
    python scripts/smoke_test_reddit.py <campaign_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.reddit_ad_client import RedditAdClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_id", help="Reddit campaign ID to toggle.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    username = os.environ.get("REDDIT_USERNAME", "").strip()
    password = os.environ.get("REDDIT_PASSWORD", "").strip()
    cookie_jar = os.environ.get("REDDIT_COOKIE_JAR", "").strip()
    # With a valid cookie jar the client restores the saved session, so
    # credentials are only needed as a fallback when the jar is absent.
    if not cookie_jar and not (username and password):
        print(
            "error: set REDDIT_COOKIE_JAR (bootstrapped session) or "
            "REDDIT_USERNAME + REDDIT_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    client = RedditAdClient(
        username=username,
        password=password,
        ads_account_id=os.environ.get("REDDIT_ADS_ACCOUNT_ID", "").strip(),
        cookie_jar_path=cookie_jar,
        patch_body_pause=os.environ.get(
            "REDDIT_PATCH_BODY_PAUSE", '{"data":{"configured_status":"PAUSED"}}'
        ),
        patch_body_resume=os.environ.get(
            "REDDIT_PATCH_BODY_RESUME", '{"data":{"configured_status":"ACTIVE"}}'
        ),
    )

    print(f"--- smoke test: campaign={args.campaign_id}")
    try:
        for step, action in (
            ("disable", client.disable_campaign),
            ("enable", client.enable_campaign),
            ("disable (cleanup)", client.disable_campaign),
        ):
            print(f"\n>>> {step}")
            try:
                result = action(args.campaign_id)
            except Exception as exc:
                print(f"FAILED: {exc!r}", file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2, default=str))
    finally:
        client.close()

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
