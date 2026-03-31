#!/usr/bin/env python3
"""Entry point for the stream-ad monitor agent.

Usage::

    export TWITCH_CLIENT_ID=...
    export TWITCH_CLIENT_SECRET=...
    export TWITCH_CHANNEL_LOGIN=your_channel
    export REDDIT_CLIENT_ID=...
    export REDDIT_CLIENT_SECRET=...
    export REDDIT_ADS_ACCOUNT_ID=...
    export REDDIT_AD_GROUP_ID=...
    # Optional:
    export POLL_INTERVAL=60          # seconds between Twitch polls (default 60)
    export TRIGGER_KEYWORD=Spark     # keyword to watch for in title (default Spark)

    python run.py
"""

import logging

from stream_ad_monitor.config import Config
from stream_ad_monitor.monitor import StreamAdMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    config = Config()
    monitor = StreamAdMonitor(config)
    monitor.run()
