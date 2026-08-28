#!/usr/bin/env python3
"""Verify the go-live announcement path without waiting for a real stream.

Renders the announcement exactly as the daemon would, checks the YouTube
lookup against the live channel page, and (unless --dry-run) actually posts
to every configured platform. Run it once after configuring credentials —
finding out that an access token is read-only is much nicer here than at the
top of a stream.

Usage:
    set -a; source /etc/streaming-ad-monitor/env; set +a

    # Show what would be posted, and probe YouTube. Posts nothing.
    python scripts/test_announce.py --dry-run

    # Actually post (and thread a follow-up if the channel is live on YouTube).
    python scripts/test_announce.py --title "test post, ignore"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.announcer import build_announcer
from stream_ad_monitor.config import Config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title",
        default="[test] streaming-ad-monitor announcement check",
        help="Stream title to render into the announcement.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and probe only; post nothing.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config()
    announcer = build_announcer(config.twitch_channel_login, config.announce)
    if announcer is None:
        print(
            "No announcement targets configured. Set TWITTER_* and/or "
            "BLUESKY_* in the environment.",
            file=sys.stderr,
        )
        return 2

    try:
        return _run(announcer, args)
    finally:
        announcer.close()


def _run(announcer, args) -> int:
    print(f"\nTargets: {', '.join(sorted(announcer.targets))}")

    # 1. YouTube lookup -------------------------------------------------
    youtube_url = ""
    if announcer.youtube is None:
        print(
            "YouTube: not configured (set YOUTUBE_CHANNEL_HANDLE, "
            "YOUTUBE_CHANNEL_ID, or YOUTUBE_LIVE_URL to enable)."
        )
    else:
        print(f"YouTube: checking {announcer.youtube.live_url}")
        try:
            video = announcer.youtube.find_live_video()
        except Exception as exc:
            print(f"    ERROR: lookup failed: {exc!r}", file=sys.stderr)
            video = None
        if video is None:
            print(
                "    not live right now — the daemon would keep checking for "
                f"{announcer.settings.youtube_lookup_timeout}s after going live"
            )
        else:
            youtube_url = video.url
            print(f"    live: {youtube_url} ({video.title})")

    # 2. Render ---------------------------------------------------------
    if not youtube_url and announcer.settings.wait_for_youtube_sec:
        print(
            "\nNote: ANNOUNCE_WAIT_FOR_YOUTUBE_SEC="
            f"{announcer.settings.wait_for_youtube_sec} — for a real stream the "
            "daemon would hold this post that long for the YouTube link. This "
            "check posts right away."
        )

    text = announcer.render_announcement(args.title, youtube_url)
    print("\n--- announcement ---")
    print(text)
    print("--------------------")
    followup = ""
    if not youtube_url:
        followup = announcer.render_youtube_followup(
            args.title, "https://www.youtube.com/watch?v=EXAMPLE"
        )
        print("\n--- YouTube follow-up (posted as a reply, once YT goes live) ---")
        print(followup)
        print("----------------------------------------------------------------")

    if args.dry_run:
        print("\n--dry-run: nothing posted.")
        return 0

    # 3. Post -----------------------------------------------------------
    failures = []
    for name, client in sorted(announcer.targets.items()):
        print(f"\n>>> posting to {name}")
        try:
            ref = client.post(text)
            print(f"    PASS: {ref.get('url') or ref}")
        except Exception as exc:
            failures.append(name)
            print(f"    FAIL: {exc!r}", file=sys.stderr)

    if failures:
        print(f"\nFAIL — could not post to: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nPASS — announcement posted everywhere. Delete the test posts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
