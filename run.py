#!/usr/bin/env python3
"""Entry point for the stream-ad monitor agent.

Reddit auth: a headless Chromium logs into ads.reddit.com with username +
password. Cookies + localStorage are persisted to REDDIT_COOKIE_JAR (if set)
so subsequent runs reuse the session. 2FA is not supported.

TrafficStars auth: plain REST — the account API key (from
https://admin.trafficstars.com/profile/) is exchanged for a bearer token.

Go-live announcements (optional): when X, Bluesky, and/or Mastodon
credentials are set, the same poll that toggles the ads also posts the
stream link. The YouTube
link only exists a little while after the broadcast starts, so it arrives as
a threaded reply once the channel's /live page resolves to it.

Required env vars:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL_LOGIN
  RULES_FILE   — YAML rules file (recommended), OR legacy single-rule mode:
                 REDDIT_CAMPAIGN_ID and/or TRAFFICSTARS_CAMPAIGN_ID
                 + optional TRIGGER_KEYWORD

Required when any rule targets Reddit campaigns:
  REDDIT_USERNAME, REDDIT_PASSWORD

Required when any rule targets TrafficStars campaigns:
  TRAFFICSTARS_API_KEY

Required to announce the stream on X (all four, from the app's
"Keys and tokens" tab; the app needs Read and write permission):
  TWITTER_API_KEY, TWITTER_API_SECRET,
  TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET

Required to announce the stream on Bluesky:
  BLUESKY_HANDLE, BLUESKY_APP_PASSWORD

Required to announce the stream on Mastodon (Preferences → Development →
New application, scope write:statuses):
  MASTODON_ACCESS_TOKEN

Optional env vars:
  REDDIT_ADS_ACCOUNT_ID       Ads account id (from the dashboard URL:
                              ads.reddit.com/account/<id>/dashboard).
                              Auto-discovered after login when unset.
  REDDIT_COOKIE_JAR           Persist session to this file path between runs
  REDDIT_PATCH_BODY_PAUSE     Override the JSON body for pause (default
                              '{"data":{"configured_status":"PAUSED"}}')
  REDDIT_PATCH_BODY_RESUME    Override the JSON body for resume (default
                              '{"data":{"configured_status":"ACTIVE"}}')
  POLL_INTERVAL               Seconds between Twitch polls (default 60)
  LOG_LEVEL                   DEBUG, INFO, WARNING, ERROR (default INFO)

  Announcements:
  ANNOUNCE_ENABLED            false to switch announcements off (default true)
  ANNOUNCE_KEYWORDS           Comma-separated; only announce matching titles
                              (default: announce every stream)
  ANNOUNCE_TEMPLATE           Post body. Placeholders: {title} {channel}
                              {twitch_url} {youtube_url} {links}. Use \\n for
                              a line break (default
                              '🔴 Live now: {title}\\n\\n{links}')
  ANNOUNCE_YOUTUBE_TEMPLATE   Follow-up post body (default
                              'Also streaming on YouTube: {youtube_url}')
  ANNOUNCE_TITLE_MAX_CHARS    Trim long titles so the links fit (default 140)
  ANNOUNCE_WAIT_FOR_YOUTUBE_SEC
                              Hold the announcement this long so one post can
                              carry both links (default 0 = post immediately
                              and reply with YouTube later)
  ANNOUNCE_STATE_FILE         Persist announcement state here so a restart
                              mid-stream doesn't post twice
  BLUESKY_PDS_URL             Non-default PDS (default https://bsky.social)
  MASTODON_INSTANCE_URL       Mastodon instance (default https://tech.lgbt)
  MASTODON_VISIBILITY         public, unlisted, private, direct
                              (default public)
  MASTODON_MAX_CHARS          Pin the instance's post limit (default: read
                              it from the instance on first post)
  YOUTUBE_CHANNEL_HANDLE      @name — enables the YouTube follow-up
  YOUTUBE_CHANNEL_ID          UC… — alternative to the handle
  YOUTUBE_LIVE_URL            Explicit /live URL, if neither form fits
  YOUTUBE_LOOKUP_INTERVAL     Seconds between YouTube checks (default 60)
  YOUTUBE_LOOKUP_TIMEOUT      Give up on the YouTube link this long after
                              going live (default 1800)

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
