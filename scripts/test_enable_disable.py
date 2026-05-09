#!/usr/bin/env python3
"""Verify enable then disable round-trip on a campaign.

Reads the configured_status field returned by each PATCH and asserts it
matches the requested state. Always disables at the end so a mid-test failure
doesn't leave the campaign ACTIVE.

Usage:
    set -a; source .env; set +a
    python scripts/test_enable_disable.py <campaign_id>
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.reddit_ad_client import RedditAdClient


def _status_from_response(resp: dict) -> str:
    """Pull configured_status out of the ads-api PATCH response shape."""
    if not isinstance(resp, dict):
        return "<non-dict>"
    data = resp.get("data") or resp
    return data.get("configured_status", "<missing>")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if len(sys.argv) < 2:
        print("usage: test_enable_disable.py <campaign_id>", file=sys.stderr)
        return 2
    campaign_id = sys.argv[1]

    client = RedditAdClient(
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        cookie_jar_path=os.environ.get("REDDIT_COOKIE_JAR", ""),
    )

    failures: list[str] = []
    try:
        # --- Step 1: ENABLE ---
        print(f"\n>>> enable_campaign({campaign_id})")
        resp = client.enable_campaign(campaign_id)
        status = _status_from_response(resp)
        print(f"    configured_status -> {status!r}")
        if status == "ACTIVE":
            print("    PASS: campaign reports ACTIVE")
        else:
            failures.append(f"after enable, expected ACTIVE, got {status!r}")
            print(f"    FAIL: {failures[-1]}")

        # --- Step 2: DISABLE ---
        print(f"\n>>> disable_campaign({campaign_id})")
        resp = client.disable_campaign(campaign_id)
        status = _status_from_response(resp)
        print(f"    configured_status -> {status!r}")
        if status == "PAUSED":
            print("    PASS: campaign reports PAUSED")
        else:
            failures.append(f"after disable, expected PAUSED, got {status!r}")
            print(f"    FAIL: {failures[-1]}")

        print()
        if failures:
            print(f"FAILED ({len(failures)} step(s) wrong):")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("PASS — enable then disable round-trip works")
        return 0
    except Exception as exc:
        # Ensure campaign isn't left ACTIVE if something blew up mid-flight.
        print(f"\nERROR: {exc!r}", file=sys.stderr)
        try:
            print("Attempting cleanup disable...", file=sys.stderr)
            client.disable_campaign(campaign_id)
        except Exception as cleanup_exc:
            print(f"Cleanup also failed: {cleanup_exc!r}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
