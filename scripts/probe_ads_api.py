#!/usr/bin/env python3
"""Throwaway diagnostic: GET-probe a few ads-api paths from inside Chromium.

Restores the saved session, navigates to ads.reddit.com so fetch() inherits
the dashboard's auth, then does a GET on a list of candidate URLs and prints
the status + first 300 bytes of the response. No state-changing calls.

Usage:
    python scripts/probe_ads_api.py <campaign_id>
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.reddit_ad_client import RedditAdClient


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) < 2:
        print("usage: probe_ads_api.py <campaign_id>", file=sys.stderr)
        return 2
    cid = sys.argv[1]

    client = RedditAdClient(
        username=os.environ.get("REDDIT_USERNAME", ""),
        password=os.environ.get("REDDIT_PASSWORD", ""),
        cookie_jar_path=os.environ.get("REDDIT_COOKIE_JAR", ""),
    )
    try:
        client.authenticate()
        client.driver.get("https://ads.reddit.com/")
        print(f"after .get('https://ads.reddit.com/'), current_url = {client.driver.current_url!r}")
        print(f"page title = {client.driver.title!r}")

        # Save a screenshot so we can see if Chromium is actually on the dashboard.
        shot_path = "/tmp/probe_dashboard.png"
        try:
            client.driver.save_screenshot(shot_path)
            print(f"screenshot saved: {shot_path}")
        except Exception as exc:
            print(f"screenshot failed: {exc}")
        print()

        candidates = [
            # Same-origin first — proves whether fetch works from this page at all
            "https://ads.reddit.com/api/v3/me",
            f"https://ads.reddit.com/api/v3/campaigns/{cid}",
            # Cross-origin to ads-api (what the dashboard actually uses)
            f"https://ads-api.reddit.com/api/v3/campaigns/{cid}",
            "https://ads-api.reddit.com/api/v3/me",
            "https://ads-api.reddit.com/api/v3/me/accounts",
        ]

        # token_v2 is HttpOnly — invisible to document.cookie. Pull it out
        # via selenium's cookie API (which can see HttpOnly cookies) and pass
        # it into the JS as an explicit argument.
        all_cookies = client.driver.get_cookies()
        token_v2 = next((c["value"] for c in all_cookies if c["name"] == "token_v2"), "")
        print(f"token_v2 pulled from selenium: len={len(token_v2)}\n")

        script = """
        const url = arguments[0];
        const token = arguments[1];
        const cb = arguments[2];
        const headers = {'Accept': 'application/json'};
        if (token) headers['Authorization'] = 'Bearer ' + token;
        fetch(url, {method: 'GET', credentials: 'include', headers})
          .then(async r => cb({status: r.status, body: (await r.text()).slice(0, 500)}))
          .catch(e => cb({status: 0, body: String(e)}));
        """
        client.driver.set_script_timeout(15)
        for url in candidates:
            try:
                r = client.driver.execute_async_script(script, url, token_v2)
            except Exception as exc:
                print(f"GET {url}\n  ERROR: {exc!r}\n")
                continue
            print(f"GET {url}\n  status={r['status']}")
            print(f"  body={r['body'][:300]!r}\n")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
