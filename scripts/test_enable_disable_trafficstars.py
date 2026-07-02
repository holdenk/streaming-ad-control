#!/usr/bin/env python3
"""Verify enable then disable round-trip on a TrafficStars campaign.

The v2 run/pause endpoints report per-campaign success; the client raises if
the campaign lands in the "failed" list. Always pauses at the end so a
mid-test failure doesn't leave the campaign running.

Usage:
    set -a; source .env; set +a   # needs TRAFFICSTARS_API_KEY
    python scripts/test_enable_disable_trafficstars.py <campaign_id>
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.trafficstars_client import TrafficStarsClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if len(sys.argv) < 2:
        print("usage: test_enable_disable_trafficstars.py <campaign_id>", file=sys.stderr)
        return 2
    campaign_id = sys.argv[1]

    api_key = os.environ.get("TRAFFICSTARS_API_KEY")
    if not api_key:
        print("TRAFFICSTARS_API_KEY is not set.", file=sys.stderr)
        return 2

    client = TrafficStarsClient(api_key)
    try:
        print(f"\n>>> enable_campaign({campaign_id})")
        resp = client.enable_campaign(campaign_id)
        print(f"    response -> {resp!r}")
        print("    PASS: campaign reported in 'success' list")

        print(f"\n>>> disable_campaign({campaign_id})")
        resp = client.disable_campaign(campaign_id)
        print(f"    response -> {resp!r}")
        print("    PASS: campaign reported in 'success' list")

        print("\nPASS — enable then disable round-trip works")
        return 0
    except Exception as exc:
        # Ensure campaign isn't left running if something blew up mid-flight.
        print(f"\nERROR: {exc!r}", file=sys.stderr)
        try:
            print("Attempting cleanup pause...", file=sys.stderr)
            client.disable_campaign(campaign_id)
        except Exception as cleanup_exc:
            print(f"Cleanup also failed: {cleanup_exc!r}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
