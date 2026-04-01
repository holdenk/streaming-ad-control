#!/usr/bin/env python3
"""Entry point for the stream-ad monitor agent.

Rules can be configured in two ways:

**Option A – YAML rules file (recommended, supports multiple rules):**

    export TWITCH_CLIENT_ID=...
    export TWITCH_CLIENT_SECRET=...
    export TWITCH_CHANNEL_LOGIN=your_channel
    export REDDIT_CLIENT_ID=...
    export REDDIT_CLIENT_SECRET=...
    export REDDIT_ADS_ACCOUNT_ID=...
    export RULES_FILE=/path/to/rules.yaml  # see rules.example.yaml
    # Optional:
    export POLL_INTERVAL=60                # seconds between polls (default 60)
    export LOG_LEVEL=DEBUG                 # DEBUG, INFO, WARNING, ERROR (default INFO)

    python run.py

**Option B – Legacy single-rule env vars (backward compatible):**

    export TWITCH_CLIENT_ID=...
    export TWITCH_CLIENT_SECRET=...
    export TWITCH_CHANNEL_LOGIN=your_channel
    export REDDIT_CLIENT_ID=...
    export REDDIT_CLIENT_SECRET=...
    export REDDIT_ADS_ACCOUNT_ID=...
    export REDDIT_AD_GROUP_ID=...
    # Optional:
    export POLL_INTERVAL=60          # seconds between polls (default 60)
    export TRIGGER_KEYWORD=Spark     # keyword to watch for in title (default Spark)
    export LOG_LEVEL=DEBUG           # DEBUG, INFO, WARNING, ERROR (default INFO)

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
