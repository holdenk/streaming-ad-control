#!/usr/bin/env python3
"""One-shot (or watching) ad-control gate for OBS and other streaming triggers.

The polling daemon (``run.py``) watches Twitch on a timer. This gate is the
event-driven alternative that OBS drives directly: it flips your campaigns the
instant you go live with an on-topic title, and pauses them when you stop. It's
what ``contrib/obs_ad_control.py`` shells out to, but it's a normal CLI too.

Events:

  * ``started`` — resolve the current title and enable the campaigns whose
    keywords match it (others stay paused). With ``--watch`` it then keeps
    running as a self-terminating *guard*: it polls until the stream actually
    ends (or the title stops matching) and pauses everything before exiting, so
    the campaign is torn down even if OBS is force-killed and never fires a
    stop event.
  * ``stopped`` — stop any running guard and pause every campaign,
    unconditionally.

Title resolution for ``started`` (default-deny — anything unresolved stays
paused, so ad spend never runs on an off-topic or mistitled stream):

  * ``--title "..."`` — use this exact title. No network, no credentials.
    Incompatible with ``--watch`` (there's nothing to detect the stream ending).
  * ``--title-source public`` (default) — read the live title from Twitch's
    **public** GQL endpoint. No developer app, no client id/secret — only your
    channel name (``--channel`` or ``TWITCH_CHANNEL_LOGIN``), which isn't a
    secret. This is the credential-free path.
  * ``--title-source helix`` — read it from the official Helix API instead.
    Needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET (a registered Twitch app).

Twitch lags OBS going live by a few seconds, so the gate polls until the
channel reports live (up to ``--twitch-timeout``) before reading the title.

Configuration is the same environment / RULES_FILE the daemon uses (see
run.py). ``--env-file`` loads a KEY=value file first so OBS can point at one
file instead of exporting a shell environment.

Usage:
    # went live: read the title from Twitch (no creds) and watch until it ends
    python scripts/obs_title_gate.py started --watch --env-file ~/.config/obs-ad-control.env

    # went live, one-shot, credential-free auto title
    python scripts/obs_title_gate.py started --env-file ~/.config/obs-ad-control.env

    # went live with an explicit title (no network at all)
    python scripts/obs_title_gate.py started --title "Apache Spark deep dive"

    # stopped: stop the guard and pause everything (never needs Twitch)
    python scripts/obs_title_gate.py stopped --env-file ~/.config/obs-ad-control.env
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import tempfile
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_ad_monitor.config import Config
from stream_ad_monitor.monitor import StreamAdMonitor
from stream_ad_monitor.twitch_client import TwitchClient
from stream_ad_monitor.twitch_public_client import TwitchPublicTitleClient

logger = logging.getLogger("obs_title_gate")

_DEFAULT_TWITCH_TIMEOUT_SEC = 45.0
_DEFAULT_TWITCH_POLL_SEC = 3.0
_DEFAULT_WATCH_POLL_SEC = 60.0
# How many consecutive offline reads confirm the stream really ended (rather
# than a single transient blip) before the guard tears down and exits.
_DEFAULT_OFFLINE_CONFIRM = 2
_DEFAULT_PIDFILE = os.path.join(
    tempfile.gettempdir(), "streaming-ad-control-guard.pid"
)


# ---------------------------------------------------------------------------
# Env file
# ---------------------------------------------------------------------------


def _load_env_file(path: str) -> int:
    """Load ``KEY=value`` lines from *path* into ``os.environ``.

    Mirrors the ``.env`` files the README's ``set -a; source .env`` flow uses,
    so OBS can hand the gate one file instead of a pre-populated shell
    environment. Blank lines and ``#`` comments are skipped; a leading
    ``export`` is tolerated; surrounding single/double quotes are stripped.

    Existing environment variables are **not** overwritten, so a value set in
    the real process environment always wins over the file. Returns the number
    of variables set.
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


# ---------------------------------------------------------------------------
# Twitch title source + liveness polling
# ---------------------------------------------------------------------------


def _make_title_client(source: str, config: Config):
    """Build the client used to read live-state + title for *source*."""
    if source == "helix":
        return TwitchClient(config.twitch_client_id, config.twitch_client_secret)
    return TwitchPublicTitleClient()


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

    Twitch lags OBS by a few seconds when a stream starts, so a single lookup
    right after going live often still reports offline. We retry every
    *poll_interval* seconds until the channel is live or the deadline passes.
    Returns the live stream dict, or None on timeout. Transient errors are
    swallowed and retried so one blip doesn't abort the wait.
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
            logger.info("Twitch reports '%s' live after %d attempt(s).", channel, attempt)
            return stream
        if now() >= deadline:
            logger.warning(
                "Twitch never reported '%s' live within %.0fs (%d attempt(s)).",
                channel,
                timeout,
                attempt,
            )
            return None
        logger.debug("'%s' not live yet (attempt %d); retrying in %.0fs.", channel, attempt, poll_interval)
        sleep(poll_interval)


# ---------------------------------------------------------------------------
# Guard single-instance (pidfile)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pidfile(pidfile: str) -> Optional[int]:
    try:
        with open(pidfile, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _acquire_guard_slot(pidfile: str) -> None:
    """Ensure only one guard runs: terminate any previous live guard, record ours.

    The latest ``started --watch`` wins — a stop/restart cycle supersedes the
    old guard rather than stacking a second one that would fight over state.
    """
    old = _read_pidfile(pidfile)
    if old and old != os.getpid() and _pid_alive(old):
        logger.info("Guard: superseding previous guard (pid %d).", old)
        try:
            os.kill(old, signal.SIGTERM)
        except OSError as exc:
            logger.debug("Could not signal old guard %d: %s", old, exc)
    try:
        with open(pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError as exc:
        logger.warning("Guard: could not write pidfile %s: %s", pidfile, exc)


def _release_guard_slot(pidfile: str) -> None:
    """Remove the pidfile only if we still own it (a superseding guard may not)."""
    if _read_pidfile(pidfile) == os.getpid():
        try:
            os.remove(pidfile)
        except OSError:
            pass


def _terminate_guard(pidfile: str) -> None:
    """Best-effort: stop a running guard and clear its pidfile (used on stop)."""
    pid = _read_pidfile(pidfile)
    if pid and pid != os.getpid() and _pid_alive(pid):
        logger.info("Stopping running guard (pid %d).", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.debug("Could not signal guard %d: %s", pid, exc)
    try:
        os.remove(pidfile)
    except OSError:
        pass


def _install_guard_signal_handler() -> None:
    """Turn SIGTERM into a clean SystemExit so the guard's finally block runs."""
    def _handler(signum, frame):  # noqa: ANN001 - signal handler signature
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        logger.debug("Could not install SIGTERM handler (not main thread?).")


# ---------------------------------------------------------------------------
# Applying desired state
# ---------------------------------------------------------------------------


def _safe_apply(monitor: StreamAdMonitor, title: str, live: bool) -> bool:
    """Apply desired state, converting a toggle failure into a logged False.

    The guard must survive a transient toggle failure and retry next poll
    rather than crashing, so it swallows (and logs) errors here.
    """
    try:
        monitor.apply(title, live=live)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("apply failed (title=%r live=%s): %s", title, live, exc)
        return False


def _run_guard(monitor: StreamAdMonitor, channel: str, args: argparse.Namespace) -> int:
    """Enable on go-live, then watch until the stream ends; pause and exit.

    Survives an unclean OBS exit because it polls Twitch independently. Only one
    guard runs at a time (pidfile); a newer one supersedes it.
    """
    _install_guard_signal_handler()
    _acquire_guard_slot(args.pidfile)
    try:
        logger.info(
            "Guard: waiting up to %.0fs for '%s' to go live.", args.twitch_timeout, channel
        )
        stream = _wait_for_live_stream(
            monitor.twitch, channel, args.twitch_timeout, args.twitch_poll
        )
        if stream is None:
            logger.warning(
                "Guard: '%s' never reported live; leaving campaigns paused. Exiting.",
                channel,
            )
            _safe_apply(monitor, "", live=False)
            return 0

        logger.info(
            "Guard: '%s' live — enabling matching campaigns and watching until it "
            "ends (poll every %.0fs).",
            channel,
            args.watch_poll,
        )
        current_title = stream.get("title", "")
        offline_reads = 0
        while True:
            _safe_apply(monitor, current_title, live=True)
            time.sleep(args.watch_poll)
            stream = monitor.twitch.get_stream(channel)
            if stream is None:
                offline_reads += 1
                logger.info(
                    "Guard: '%s' read offline (%d/%d).",
                    channel,
                    offline_reads,
                    args.offline_confirm,
                )
                if offline_reads >= args.offline_confirm:
                    logger.info("Guard: stream ended — pausing campaigns and exiting.")
                    return 0 if _safe_apply(monitor, "", live=False) else 1
                continue  # tentative offline — keep current state, retry next poll
            offline_reads = 0
            current_title = stream.get("title", "")
    finally:
        _release_guard_slot(args.pidfile)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
        "--watch",
        action="store_true",
        help="After enabling, keep watching until the stream ends, then pause "
        "and exit (crash-safe backstop). Reads the live title from Twitch.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Explicit stream title. No network / credentials. Not usable with --watch.",
    )
    parser.add_argument(
        "--title-source",
        choices=["public", "helix"],
        default="public",
        help="Where to read the live title from: 'public' = Twitch's public GQL "
        "endpoint (no credentials, default); 'helix' = the official API "
        "(needs TWITCH_CLIENT_ID/SECRET).",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Twitch channel login to read the title from "
        "(defaults to TWITCH_CHANNEL_LOGIN). Not a secret.",
    )
    parser.add_argument(
        "--twitch-timeout",
        type=float,
        default=_DEFAULT_TWITCH_TIMEOUT_SEC,
        help=f"Max seconds to wait for Twitch to report live (default {_DEFAULT_TWITCH_TIMEOUT_SEC:.0f}).",
    )
    parser.add_argument(
        "--twitch-poll",
        type=float,
        default=_DEFAULT_TWITCH_POLL_SEC,
        help=f"Seconds between go-live polls (default {_DEFAULT_TWITCH_POLL_SEC:.0f}).",
    )
    parser.add_argument(
        "--watch-poll",
        type=float,
        default=_DEFAULT_WATCH_POLL_SEC,
        help=f"Seconds between polls while watching in --watch mode (default {_DEFAULT_WATCH_POLL_SEC:.0f}).",
    )
    parser.add_argument(
        "--offline-confirm",
        type=int,
        default=_DEFAULT_OFFLINE_CONFIRM,
        help="Consecutive offline reads before --watch tears down "
        f"(default {_DEFAULT_OFFLINE_CONFIRM}; debounces transient blips).",
    )
    parser.add_argument(
        "--pidfile",
        default=_DEFAULT_PIDFILE,
        help="Single-instance lock for the --watch guard (default in the temp dir).",
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

    # Stopping always stops any running guard first, even if config below is
    # broken — pausing ads must be maximally robust.
    if args.event == "stopped":
        _terminate_guard(args.pidfile)

    if args.watch and args.title is not None:
        logger.error(
            "--title can't be combined with --watch; --watch reads the live "
            "title from Twitch so it can also detect when the stream ends."
        )
        return 2

    # Fetching from Twitch is needed on 'started' unless an explicit title was
    # given (and never in --watch, which must poll liveness). Only the Helix
    # source needs credentials; the public source and the stop/explicit-title
    # paths do not.
    need_fetch = args.event == "started" and (args.watch or args.title is None)
    require_twitch = need_fetch and args.title_source == "helix"

    try:
        config = Config(require_twitch=require_twitch)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    title_client = _make_title_client(args.title_source, config) if need_fetch else None
    monitor = StreamAdMonitor(config, twitch_client=title_client)
    try:
        if args.event == "stopped":
            logger.info("OBS stream stopped — pausing all campaigns.")
            monitor.apply(title="", live=False)
            return 0

        # event == "started"
        if not need_fetch:
            title = args.title or ""
            logger.info("OBS stream started — using supplied title %r.", title)
            monitor.apply(title=title, live=True)
            return 0

        channel = (args.channel or config.twitch_channel_login).strip()
        if not channel:
            logger.error(
                "Reading the title from Twitch needs a channel (--channel or "
                "TWITCH_CHANNEL_LOGIN). Leaving all campaigns paused."
            )
            monitor.apply(title="", live=False)
            return 2

        if args.watch:
            return _run_guard(monitor, channel, args)

        logger.info(
            "OBS stream started — waiting up to %.0fs for Twitch to report '%s' "
            "live (source=%s), then reading its title.",
            args.twitch_timeout,
            channel,
            args.title_source,
        )
        stream = _wait_for_live_stream(
            monitor.twitch, channel, args.twitch_timeout, args.twitch_poll
        )
        if stream is None:
            monitor.apply(title="", live=False)  # default-deny
            return 0
        monitor.apply(title=stream.get("title", ""), live=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — surface any toggle failure as non-zero
        logger.error("Gate failed: %s", exc)
        return 1
    finally:
        monitor.close()
        if title_client is not None:
            try:
                title_client.close()
            except Exception:
                logger.debug("Title client close raised; ignoring.", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
