#!/usr/bin/env python3
"""Verify enable then disable round-trip on a campaign on any REST ad network.

Works for every token-based network: trafficstars, exoclick, propellerads,
meta. (Reddit has its own scripts — test_enable_disable.py — because it needs
the bootstrapped browser session.) Each client raises if the network reports
the toggle failed. Always pauses at the end so a mid-test failure doesn't
leave the campaign running.

Usage:
    set -a; source .env; set +a   # needs that network's token, e.g. META_ACCESS_TOKEN
    python scripts/test_enable_disable_network.py <network> <campaign_id>
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.networks import NETWORKS, NETWORKS_BY_KEY

_REST_KEYS = [n.key for n in NETWORKS if n.factory is not None]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if len(sys.argv) < 3 or sys.argv[1] not in _REST_KEYS:
        print(
            f"usage: test_enable_disable_network.py {{{','.join(_REST_KEYS)}}} <campaign_id>",
            file=sys.stderr,
        )
        return 2
    network = NETWORKS_BY_KEY[sys.argv[1]]
    campaign_id = sys.argv[2]

    token = os.environ.get(network.credential_env, "").strip()
    if not token:
        print(f"{network.credential_env} is not set.", file=sys.stderr)
        return 2

    client = network.factory(token)
    try:
        print(f"\n>>> {network.label}: enable_campaign({campaign_id})")
        print(f"    response -> {client.enable_campaign(campaign_id)!r}")

        print(f"\n>>> {network.label}: disable_campaign({campaign_id})")
        print(f"    response -> {client.disable_campaign(campaign_id)!r}")

        print(f"\nPASS — {network.label} enable then disable round-trip works")
        return 0
    except Exception as exc:
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
