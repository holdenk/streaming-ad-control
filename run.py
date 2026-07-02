#!/usr/bin/env python3
"""Entry point for the stream-ad monitor agent.

Reddit auth: a headless Chromium logs into ads.reddit.com with username +
password. Cookies + localStorage are persisted to REDDIT_COOKIE_JAR (if set)
so subsequent runs reuse the session. 2FA is not supported.

TrafficStars auth: plain REST — the account API key (from
https://admin.trafficstars.com/profile/) is exchanged for a bearer token.

Required env vars:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL_LOGIN
  RULES_FILE   — YAML rules file (recommended), OR legacy single-rule mode:
                 REDDIT_CAMPAIGN_ID and/or TRAFFICSTARS_CAMPAIGN_ID
                 + optional TRIGGER_KEYWORD

Required when any rule targets Reddit campaigns:
  REDDIT_USERNAME, REDDIT_PASSWORD
  REDDIT_ADS_ACCOUNT_ID  — the id in the dashboard URL:
                           ads.reddit.com/account/<id>/dashboard

Required when any rule targets TrafficStars campaigns:
  TRAFFICSTARS_API_KEY

Optional env vars:
  REDDIT_COOKIE_JAR           Persist session to this file path between runs
  REDDIT_PATCH_BODY_PAUSE     Override the JSON body for pause (default
                              '{"data":{"configured_status":"PAUSED"}}')
  REDDIT_PATCH_BODY_RESUME    Override the JSON body for resume (default
                              '{"data":{"configured_status":"ACTIVE"}}')
  POLL_INTERVAL               Seconds between Twitch polls (default 60)
  LOG_LEVEL                   DEBUG, INFO, WARNING, ERROR (default INFO)

Usage:
    python run.py
"""

import logging
import os

from stream_ad_monitor.config import Config
from stream_ad_monitor.monitor import StreamAdMonitor


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = Config()
    monitor = StreamAdMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
