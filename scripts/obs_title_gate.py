#!/usr/bin/env python3
"""One-shot ad-control gate for OBS (or any external streaming trigger).

The polling daemon (``run.py``) watches Twitch on a timer. This gate is the
event-driven alternative: OBS fires it once when you go live and once when you
stop, and it drives your campaigns to the matching state immediately — no
polling latency. It's the process ``contrib/obs_ad_control.py`` shells out to,
but it's a normal CLI you can also run by hand.

Events:

  * ``started`` — determine the current stream title, then for every rule whose
    keywords match it, enable that rule's campaigns; leave the rest paused.
  * ``stopped`` — pause every campaign across all rules, unconditionally.

Title resolution for ``started`` (default-deny — anything unresolved stays
paused, so ad spend never runs on an off-topic or mistitled stream):

  * ``--title "..."`` — use this exact title. Twitch is never contacted, so no
    Twitch credentials are needed. Handy when OBS (or you) can supply the title
    directly, and for testing.
  * otherwise — read the live title from Twitch's API. Twitch lags OBS going
    live by a few seconds, so we poll until the channel reports live or
    ``--twitch-timeout`` elapses. Needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
    / TWITCH_CHANNEL_LOGIN. If Twitch never reports live, every campaign is
    left paused.

Configuration is the same environment / RULES_FILE the daemon uses (see
run.py). ``--env-file`` loads a KEY=value file first, so OBS can point at one
file instead of exporting a shell environment.

Usage:
    # pause everything (stream stopped) — never needs Twitch creds
    python scripts/obs_title_gate.py stopped --env-file /path/to/obs-ad-control.env

    # went live: read the title from Twitch and enable matching campaigns
    python scripts/obs_title_gate.py started --env-file /path/to/obs-ad-control.env

    # went live with an explicit title (no Twitch lookup)
    python scripts/obs_title_gate.py started --title "Apache Spark deep dive"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.config import Config
from stream_ad_monitor.monitor import StreamAdMonitor

logger = logging.getLogger("obs_title_gate")

_DEFAULT_TWITCH_TIMEOUT_SEC = 45.0
_DEFAULT_TWITCH_POLL_SEC = 3.0


def _load_env_file(path: str) -> int:
    """Load ``KEY=value`` lines from *path* into ``os.environ``.

    Mirrors the ``.env`` files the README's ``set -a; source .env`` flow uses,
    so OBS can hand the gate one file instead of a pre-populated shell
    environment. Blank lines and ``#`` comments are skipped; a leading
    ``export`` is tolerated; surrounding single/double quotes are stripped.

    Existing environment variables are **not** overwritten, so a value set in
    the real process environment always wins over the file (useful for
    debugging / one-off overrides). Returns the number of variables set.
    """
    count = 0
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                logger.warning("Ignoring malformed env-file line: %r", raw.rstrip())
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if not key:
                continue
            if key in os.environ:
                logger.debug("env-file: %s already set in environment; keeping it.", key)
                continue
            os.environ[key] = value
            count += 1
    logger.debug("Loaded %d variable(s) from env file %s.", count, path)
    return count


def _wait_for_live_stream(
    twitch,
    channel: str,
    timeout: float,
    poll_interval: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Optional[dict]:
    """Poll Twitch until *channel* reports live, or *timeout* seconds elapse.

    Twitch's Helix API lags OBS by a few seconds when a stream starts, so a
    single lookup right after ``STREAMING_STARTED`` often still reports
    offline. We retry every *poll_interval* seconds until the channel is live
    or the deadline passes. Returns the live stream dict, or None on timeout.
    Transient API errors are swallowed and retried so one blip doesn't abort
    the wait.
    """
    deadline = now() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            stream = twitch.get_stream(channel)
        except Exception as exc:  # noqa: BLE001 — retry through transient API errors
            logger.warning("Twitch lookup attempt %d failed: %r", attempt, exc)
            stream = None
        if stream is not None:
            logger.info(
                "Twitch reports '%s' live after %d attempt(s).", channel, attempt
            )
            return stream
        if now() >= deadline:
            logger.warning(
                "Twitch never reported '%s' live within %.0fs (%d attempt(s)).",
                channel,
                timeout,
                attempt,
            )
            return None
        logger.debug(
            "'%s' not live yet (attempt %d); retrying in %.0fs.",
            channel,
            attempt,
            poll_interval,
        )
        sleep(poll_interval)


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enable/disable ad campaigns for an OBS streaming event.",
    )
    parser.add_argument(
        "event",
        choices=["started", "stopped"],
        help="'started' = enable matching campaigns; 'stopped' = pause everything.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Explicit stream title. Skips the Twitch lookup (and its creds).",
    )
    parser.add_argument(
        "--from-twitch",
        dest="from_twitch",
        action="store_true",
        default=None,
        help="Force reading the title from Twitch even if --title is given.",
    )
    parser.add_argument(
        "--twitch-timeout",
        type=float,
        default=_DEFAULT_TWITCH_TIMEOUT_SEC,
        help="Max seconds to wait for Twitch to report the stream live "
        f"(default {_DEFAULT_TWITCH_TIMEOUT_SEC:.0f}).",
    )
    parser.add_argument(
        "--twitch-poll",
        type=float,
        default=_DEFAULT_TWITCH_POLL_SEC,
        help=f"Seconds between Twitch polls (default {_DEFAULT_TWITCH_POLL_SEC:.0f}).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Load KEY=value pairs from this file into the environment first.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.env_file:
        try:
            _load_env_file(args.env_file)
        except OSError as exc:
            logger.error("Could not read env file %s: %s", args.env_file, exc)
            return 2

    # A 'stopped' event pauses everything and never looks at a title, so it
    # never needs Twitch. On 'started' we only need Twitch when we're going to
    # fetch the title from it (no explicit --title, or --from-twitch forced).
    use_twitch = args.event == "started" and (
        args.from_twitch or args.title is None
    )

    try:
        config = Config(require_twitch=use_twitch)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    monitor = StreamAdMonitor(config)
    try:
        if args.event == "stopped":
            logger.info("OBS stream stopped — pausing all campaigns.")
            monitor.apply(title="", live=False)
            return 0

        # event == "started"
        if not use_twitch:
            title = args.title or ""
            logger.info("OBS stream started — using supplied title %r.", title)
            monitor.apply(title=title, live=True)
            return 0

        # Resolve the title from Twitch, riding out the API's go-live lag.
        if not config.twitch_channel_login:
            logger.error(
                "Reading the title from Twitch needs TWITCH_CHANNEL_LOGIN "
                "(and TWITCH_CLIENT_ID/SECRET). Set them, or pass --title. "
                "Leaving all campaigns paused."
            )
            monitor.apply(title="", live=False)
            return 2

        logger.info(
            "OBS stream started — waiting up to %.0fs for Twitch to report "
            "'%s' live, then reading its title.",
            args.twitch_timeout,
            config.twitch_channel_login,
        )
        stream = _wait_for_live_stream(
            monitor.twitch,
            config.twitch_channel_login,
            timeout=args.twitch_timeout,
            poll_interval=args.twitch_poll,
        )
        if stream is None:
            # Default-deny: couldn't confirm a live, on-topic stream.
            monitor.apply(title="", live=False)
            return 0
        title = stream.get("title", "")
        monitor.apply(title=title, live=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — surface any toggle failure as non-zero
        logger.error("Gate failed: %s", exc)
        return 1
    finally:
        monitor.close()


if __name__ == "__main__":
    sys.exit(main())
