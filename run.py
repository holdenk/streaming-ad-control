#!/usr/bin/env python3
"""Entry point for the stream-ad monitor agent.

Reddit auth: a headless Chromium logs into ads.reddit.com with username +
password. Cookies + localStorage are persisted to REDDIT_COOKIE_JAR (if set)
so subsequent runs reuse the session. 2FA is not supported.

Required env vars:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL_LOGIN
  REDDIT_USERNAME, REDDIT_PASSWORD
  RULES_FILE   — YAML rules file (recommended), OR
  REDDIT_CAMPAIGN_ID + optional TRIGGER_KEYWORD (legacy single-rule mode)

Optional env vars:
  REDDIT_COOKIE_JAR           Persist session to this file path between runs
  REDDIT_PATCH_BODY_PAUSE     Override the JSON body for pause (default
                              '{"configured_status": "PAUSED"}')
  REDDIT_PATCH_BODY_RESUME    Override the JSON body for resume (default
                              '{"configured_status": "ACTIVE"}')
  POLL_INTERVAL               Seconds between Twitch polls (default 60)
  LOG_LEVEL                   DEBUG, INFO, WARNING, ERROR (default INFO)

Usage:
    python run.py
"""

import logging
import os

from stream_ad_monitor.config import Config
from stream_ad_monitor.monitor import StreamAdMonitor

_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    config = Config()
    monitor = StreamAdMonitor(config)
    monitor.run()
