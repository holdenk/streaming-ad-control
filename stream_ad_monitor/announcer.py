"""Posts the stream links to X, Bluesky, and Mastodon when the stream goes live.

The awkward bit is that the two links don't become available at the same
time. Twitch tells us the stream is live (and gives us its title) on the poll
that flips the channel online, but a simulcast's YouTube watch URL only shows
up in the Data API once the broadcast has been ingesting for a while — often
a minute or two, sometimes longer. Waiting for both would delay the
announcement past the point where it's useful.

So the flow is:

1. Stream goes live and the title matches (or no keywords are configured) →
   post the announcement with the Twitch link right away.
2. Keep checking the channel's YouTube ``/live`` page on its own timer
   (``YOUTUBE_LOOKUP_INTERVAL``, independent of the Twitch poll interval)
   until the live video appears or ``YOUTUBE_LOOKUP_TIMEOUT`` elapses.
3. When it appears, post the YouTube link as a *reply* to the original
   announcement on each platform, so it threads instead of landing as a
   context-free orphan post.

Set ``ANNOUNCE_WAIT_FOR_YOUTUBE_SEC`` to hold step 1 for a bit if you'd
rather have one post carrying both links than a thread.

Two properties this module works hard to keep, because the failure modes are
public and embarrassing:

* **Never post twice for the same stream.** State is keyed by the Twitch
  stream id (a new broadcast gets a new one), state survives a daemon
  restart via ``ANNOUNCE_STATE_FILE``, and a stream that briefly reads as
  offline — the Twitch API does drop the occasional poll — does not reset it.
* **Never break ad control.** Every post and lookup is best-effort; the
  monitor calls in from inside a try/except, and failures here are logged
  and retried on a budget rather than raised.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .bluesky_client import BlueskyClient
from .mastodon_client import DEFAULT_INSTANCE_URL, MastodonClient
from .twitter_client import TwitterClient
from .youtube_client import YouTubeClient

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "🔴 Live now: {title}\n\n{links}"
DEFAULT_YOUTUBE_TEMPLATE = "Also streaming on YouTube: {youtube_url}"
_FALLBACK_TEMPLATE = "🔴 Live now: {title}\n\n{links}"

_TWITCH_URL_TEMPLATE = "https://twitch.tv/{channel}"

# Stop re-posting a stream that keeps failing rather than retrying every poll
# for the length of the broadcast.
_MAX_ANNOUNCE_ATTEMPTS = 5
_MAX_FOLLOWUP_ATTEMPTS = 3
# Consecutive YouTube API errors before we stop spending quota on this stream.
_MAX_YOUTUBE_FAILURES = 3

# Fields of _StreamState that survive a restart. The rest are timers that are
# only meaningful within one process.
_PERSISTED_FIELDS = (
    "stream_id",
    "announced",
    "youtube_url",
    "youtube_done",
    "youtube_pending",
    "refs",
    "abandoned",
    "announce_attempts",
    "followup_attempts",
)


@dataclass
class AnnounceSettings:
    """Everything the announcer needs, as plain values (see :mod:`config`)."""

    enabled: bool = True
    keywords: List[str] = field(default_factory=list)
    template: str = DEFAULT_TEMPLATE
    youtube_template: str = DEFAULT_YOUTUBE_TEMPLATE
    title_max_chars: int = 140
    wait_for_youtube_sec: int = 0
    youtube_lookup_interval: int = 60
    youtube_lookup_timeout: int = 1800
    state_path: str = ""

    twitter_api_key: str = ""
    twitter_api_secret: str = ""
    twitter_access_token: str = ""
    twitter_access_token_secret: str = ""

    bluesky_handle: str = ""
    bluesky_app_password: str = ""
    bluesky_pds_url: str = ""

    mastodon_access_token: str = ""
    mastodon_instance_url: str = ""
    mastodon_visibility: str = ""
    mastodon_max_chars: int = 0

    youtube_channel_handle: str = ""
    youtube_channel_id: str = ""
    youtube_live_url: str = ""

    @property
    def twitter_configured(self) -> bool:
        return all(
            (
                self.twitter_api_key,
                self.twitter_api_secret,
                self.twitter_access_token,
                self.twitter_access_token_secret,
            )
        )

    @property
    def bluesky_configured(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)

    @property
    def mastodon_configured(self) -> bool:
        # The instance URL has a default, so the token is the only requirement.
        return bool(self.mastodon_access_token)

    @property
    def youtube_configured(self) -> bool:
        return bool(
            self.youtube_channel_handle
            or self.youtube_channel_id
            or self.youtube_live_url
        )

    @property
    def any_target_configured(self) -> bool:
        return (
            self.twitter_configured
            or self.bluesky_configured
            or self.mastodon_configured
        )


@dataclass
class _StreamState:
    """Announcement progress for one Twitch broadcast."""

    stream_id: str
    announced: bool = False
    youtube_url: str = ""
    youtube_done: bool = False
    # Targets whose announcement landed but that still owe a YouTube reply.
    youtube_pending: List[str] = field(default_factory=list)
    # target name -> post ref, used as the reply parent for the follow-up.
    refs: Dict[str, dict] = field(default_factory=dict)
    abandoned: bool = False
    announce_attempts: int = 0
    followup_attempts: int = 0

    # Transient (not persisted): monotonic timers for this process only.
    match_since: Optional[float] = None
    last_lookup: float = 0.0
    youtube_failures: int = 0

    @property
    def settled(self) -> bool:
        """True when there is nothing left to do for this stream."""
        return self.abandoned or (self.announced and self.youtube_done)


class StreamAnnouncer:
    """Drives the go-live announcement across the configured platforms."""

    def __init__(
        self,
        twitch_channel: str,
        settings: AnnounceSettings,
        targets: Dict[str, object],
        youtube: Optional[YouTubeClient] = None,
    ) -> None:
        self.twitch_channel = twitch_channel
        self.settings = settings
        self.targets = targets
        self.youtube = youtube
        self.twitch_url = _TWITCH_URL_TEMPLATE.format(channel=twitch_channel)
        self._validate_templates()
        self._state: Optional[_StreamState] = self._load_state()

    def _validate_templates(self) -> None:
        """Swap in the defaults for templates that don't render, at startup.

        Better to find out about a typo'd placeholder now than on the first
        poll of the next stream.
        """
        for attr, default in (
            ("template", DEFAULT_TEMPLATE),
            ("youtube_template", DEFAULT_YOUTUBE_TEMPLATE),
        ):
            template = getattr(self.settings, attr)
            try:
                template.format(**self._placeholders("check", "https://youtu.be/x"))
            except Exception:
                logger.warning(
                    "Announcement template %s=%r is not a valid format string; "
                    "using the default instead. Valid placeholders: %s",
                    attr,
                    template,
                    ", ".join(sorted(self._placeholders("", "").keys())),
                )
                setattr(self.settings, attr, default)

    # ------------------------------------------------------------------
    # Message composition
    # ------------------------------------------------------------------

    def _placeholders(self, title: str, youtube_url: str) -> Dict[str, str]:
        links = [self.twitch_url]
        if youtube_url:
            links.append(youtube_url)
        return {
            "title": title,
            "channel": self.twitch_channel,
            "twitch_url": self.twitch_url,
            "youtube_url": youtube_url,
            "links": "\n".join(links),
        }

    def _render(self, template: str, title: str, youtube_url: str) -> str:
        values = self._placeholders(self._truncate_title(title), youtube_url)
        try:
            return template.format(**values).strip()
        except Exception:
            logger.exception(
                "Failed to render announcement template %r; falling back.", template
            )
            return _FALLBACK_TEMPLATE.format(**values).strip()

    def _truncate_title(self, title: str) -> str:
        """Keep long titles from crowding the links out of the post."""
        limit = self.settings.title_max_chars
        if limit <= 0 or len(title) <= limit:
            return title
        return title[: limit - 1].rstrip() + "…"

    def render_announcement(self, title: str, youtube_url: str = "") -> str:
        """Public for the smoke-test script: the initial go-live post."""
        return self._render(self.settings.template, title, youtube_url)

    def render_youtube_followup(self, title: str, youtube_url: str) -> str:
        """Public for the smoke-test script: the YouTube follow-up post."""
        return self._render(self.settings.youtube_template, title, youtube_url)

    # ------------------------------------------------------------------
    # Poll entry point
    # ------------------------------------------------------------------

    def handle_stream(self, stream: Optional[dict]) -> None:
        """Advance the announcement state machine for one poll cycle.

        Args:
            stream: The Twitch stream object, or None when offline. An
                offline poll deliberately does *not* clear state: the Twitch
                API occasionally reports a live channel as offline for a
                cycle, and re-announcing on the way back would be worse than
                doing nothing. A genuinely new broadcast carries a new
                stream id, which is what actually resets things.
        """
        if stream is None:
            return

        stream_id = str(stream.get("id") or "")
        if not stream_id:
            logger.warning(
                "Twitch stream object has no id; skipping announcement (a "
                "missing id makes duplicate posts impossible to prevent)."
            )
            return
        title = stream.get("title", "") or ""

        state = self._state
        if state is None or state.stream_id != stream_id:
            state = _StreamState(stream_id=stream_id)
            self._state = state
            logger.info(
                "New Twitch broadcast %s ('%s'); announcement pending.",
                stream_id,
                title,
            )

        if state.settled:
            return

        if not self._title_matches(title):
            logger.debug(
                "Stream title '%s' does not match ANNOUNCE_KEYWORDS %s; not "
                "announcing (will re-check in case the title changes).",
                title,
                self.settings.keywords,
            )
            return

        if state.match_since is None:
            state.match_since = time.monotonic()

        if not state.announced:
            self._announce(state, title)
        elif not state.youtube_done:
            self._youtube_followup(state, title)

    def _title_matches(self, title: str) -> bool:
        if not self.settings.keywords:
            return True
        lowered = title.lower()
        return any(kw.lower() in lowered for kw in self.settings.keywords)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _announce(self, state: _StreamState, title: str) -> None:
        youtube_url = "" if state.youtube_done else self._lookup_youtube(state)

        waited = time.monotonic() - (state.match_since or 0.0)
        if (
            not youtube_url
            and not state.youtube_done
            and waited < self.settings.wait_for_youtube_sec
        ):
            logger.debug(
                "Holding the announcement for the YouTube link (%.0fs of %ds).",
                waited,
                self.settings.wait_for_youtube_sec,
            )
            return

        text = self._render(self.settings.template, title, youtube_url)
        state.announce_attempts += 1
        refs = self._post_to_all(text)

        if not refs:
            if state.announce_attempts >= _MAX_ANNOUNCE_ATTEMPTS:
                logger.error(
                    "Announcement for stream %s failed on every platform %d "
                    "times; giving up on this broadcast.",
                    state.stream_id,
                    state.announce_attempts,
                )
                state.abandoned = True
                self._save_state()
            return

        state.refs = refs
        state.announced = True
        if youtube_url:
            state.youtube_url = youtube_url
            state.youtube_done = True
        else:
            state.youtube_pending = list(refs)
        logger.info(
            "Announced stream %s on %s.", state.stream_id, ", ".join(sorted(refs))
        )
        self._save_state()

    def _youtube_followup(self, state: _StreamState, title: str) -> None:
        youtube_url = state.youtube_url or self._lookup_youtube(state)
        if not youtube_url:
            if state.youtube_done:
                self._save_state()
            return

        state.youtube_url = youtube_url
        state.followup_attempts += 1
        text = self._render(self.settings.youtube_template, title, youtube_url)

        for name in list(state.youtube_pending):
            client = self.targets.get(name)
            if client is None:
                # Target was reconfigured away between the announcement and
                # now; nothing to reply to.
                state.youtube_pending.remove(name)
                continue
            try:
                client.post(text, reply_to=state.refs.get(name))
                state.youtube_pending.remove(name)
            except Exception:
                logger.exception(
                    "Failed to post the YouTube link to %s (attempt %d/%d).",
                    name,
                    state.followup_attempts,
                    _MAX_FOLLOWUP_ATTEMPTS,
                )

        if not state.youtube_pending:
            state.youtube_done = True
            logger.info(
                "Posted the YouTube link for stream %s: %s",
                state.stream_id,
                youtube_url,
            )
        elif state.followup_attempts >= _MAX_FOLLOWUP_ATTEMPTS:
            logger.error(
                "Could not post the YouTube link to %s after %d attempts; "
                "giving up on this broadcast.",
                ", ".join(sorted(state.youtube_pending)),
                state.followup_attempts,
            )
            state.youtube_done = True
        self._save_state()

    def _lookup_youtube(self, state: _StreamState) -> str:
        """Return the live YouTube URL, '' if it isn't known (yet).

        Rate-limited to one page fetch per ``youtube_lookup_interval`` and
        abandoned after ``youtube_lookup_timeout`` — plenty of streams are
        never simulcast at all, and polling YouTube for the rest of the
        broadcast on their behalf is pure waste.
        """
        if self.youtube is None:
            state.youtube_done = True
            return ""

        now = time.monotonic()
        elapsed = now - (state.match_since or now)
        if elapsed >= self.settings.youtube_lookup_timeout:
            logger.info(
                "No live YouTube video for stream %s after %ds; stopping "
                "lookups for this broadcast.",
                state.stream_id,
                int(elapsed),
            )
            state.youtube_done = True
            return ""

        if state.last_lookup and now - state.last_lookup < self.settings.youtube_lookup_interval:
            return ""
        state.last_lookup = now

        try:
            video = self.youtube.find_live_video()
        except Exception:
            state.youtube_failures += 1
            logger.warning(
                "YouTube lookup failed (%d/%d).",
                state.youtube_failures,
                _MAX_YOUTUBE_FAILURES,
                exc_info=True,
            )
            if state.youtube_failures >= _MAX_YOUTUBE_FAILURES:
                logger.error(
                    "Giving up on the YouTube link for stream %s after %d "
                    "consecutive API errors.",
                    state.stream_id,
                    state.youtube_failures,
                )
                state.youtube_done = True
            return ""

        state.youtube_failures = 0
        return video.url if video else ""

    def _post_to_all(self, text: str) -> Dict[str, dict]:
        """Post *text* everywhere; return refs for the targets that accepted it.

        A platform that errors is simply absent from the result — the others
        still get their post, and the caller decides whether to retry.
        """
        refs: Dict[str, dict] = {}
        for name, client in self.targets.items():
            try:
                refs[name] = client.post(text)
            except Exception:
                logger.exception("Failed to post the announcement to %s.", name)
        return refs

    # ------------------------------------------------------------------
    # Restart-safe state
    # ------------------------------------------------------------------

    def _load_state(self) -> Optional[_StreamState]:
        path = self.settings.state_path
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning(
                "Could not read announcement state from %s; starting fresh.",
                path,
                exc_info=True,
            )
            return None

        if not isinstance(raw, dict) or not raw.get("stream_id"):
            return None
        known = {k: v for k, v in raw.items() if k in _PERSISTED_FIELDS}
        try:
            state = _StreamState(**known)
        except TypeError:
            logger.warning("Announcement state in %s is malformed; ignoring.", path)
            return None
        logger.info(
            "Restored announcement state for stream %s (announced=%s, "
            "youtube_done=%s) from %s.",
            state.stream_id,
            state.announced,
            state.youtube_done,
            path,
        )
        return state

    def _save_state(self) -> None:
        path = self.settings.state_path
        if not path or self._state is None:
            return
        payload = {
            k: v for k, v in asdict(self._state).items() if k in _PERSISTED_FIELDS
        }
        try:
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)
            # Write-then-rename so a crash mid-write can't leave a truncated
            # file that reads as "never announced".
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False
            ) as fh:
                json.dump(payload, fh)
                tmp_path = fh.name
            os.replace(tmp_path, path)
        except Exception:
            logger.warning(
                "Could not persist announcement state to %s; a restart during "
                "this stream may re-announce it.",
                path,
                exc_info=True,
            )

    def close(self) -> None:
        """Close every client. Idempotent, best-effort."""
        for client in list(self.targets.values()) + [self.youtube]:
            if client is None:
                continue
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                logger.debug("Client close raised; ignoring.", exc_info=True)


# ----------------------------------------------------------------------
# Construction from settings
# ----------------------------------------------------------------------


def build_announcer(
    twitch_channel: str, settings: AnnounceSettings
) -> Optional[StreamAnnouncer]:
    """Build a :class:`StreamAnnouncer`, or None when announcements are off.

    Announcements are opt-in by configuration: with no X, Bluesky, or
    Mastodon credentials there is nowhere to post, so this returns None and
    the monitor runs exactly as it did before. A client that fails to
    construct (bad credentials, missing optional dependency) is skipped
    rather than taking the daemon down with it.
    """
    if not settings.enabled:
        logger.info("Stream announcements disabled (ANNOUNCE_ENABLED=false).")
        return None
    if not settings.any_target_configured:
        logger.info(
            "Stream announcements disabled: no X, Bluesky, or Mastodon "
            "credentials configured."
        )
        return None

    targets: Dict[str, object] = {}
    if settings.twitter_configured:
        try:
            targets["twitter"] = TwitterClient(
                settings.twitter_api_key,
                settings.twitter_api_secret,
                settings.twitter_access_token,
                settings.twitter_access_token_secret,
            )
        except Exception:
            logger.exception("Could not set up the X client; skipping X posts.")
    if settings.bluesky_configured:
        try:
            targets["bluesky"] = BlueskyClient(
                settings.bluesky_handle,
                settings.bluesky_app_password,
                pds_url=settings.bluesky_pds_url or "https://bsky.social",
            )
        except Exception:
            logger.exception(
                "Could not set up the Bluesky client; skipping Bluesky posts."
            )
    if settings.mastodon_configured:
        try:
            targets["mastodon"] = MastodonClient(
                settings.mastodon_access_token,
                settings.mastodon_instance_url or DEFAULT_INSTANCE_URL,
                visibility=settings.mastodon_visibility,
                max_chars=settings.mastodon_max_chars,
            )
        except Exception:
            logger.exception(
                "Could not set up the Mastodon client; skipping Mastodon posts."
            )
    if not targets:
        logger.error(
            "Stream announcements were configured but no platform client "
            "could be built; announcements are off."
        )
        return None

    youtube: Optional[YouTubeClient] = None
    if settings.youtube_configured:
        try:
            youtube = YouTubeClient(
                settings.youtube_channel_handle,
                settings.youtube_channel_id,
                settings.youtube_live_url,
            )
        except Exception:
            logger.exception(
                "Could not set up the YouTube lookup; announcements will "
                "carry the Twitch link only."
            )
    else:
        logger.info(
            "No YOUTUBE_CHANNEL_HANDLE configured; announcing the Twitch "
            "link only."
        )

    logger.info(
        "Stream announcements enabled for %s → %s (YouTube lookup: %s).",
        twitch_channel,
        ", ".join(sorted(targets)),
        "on" if youtube is not None else "off",
    )
    return StreamAnnouncer(twitch_channel, settings, targets, youtube)
