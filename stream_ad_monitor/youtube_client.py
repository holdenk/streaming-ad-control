"""Finds a channel's current live video by reading its public ``/live`` page.

A simulcast's YouTube watch URL is not knowable when the Twitch stream
starts — YouTube only exposes the live video once the broadcast has been
ingesting for a bit — so the announcer polls this client until one shows up.

There is a Data API for this, but it wants a Google Cloud project, an API key,
and careful quota budgeting (a ``search.list`` costs 100 of a default
10,000 units/day). None of that is necessary: ``youtube.com/@handle/live``
already resolves to whatever the channel is currently broadcasting. So this is
a plain GET with no credentials to configure or rotate.

Reading it takes some care, because that page is a JavaScript shell — the
``<link rel="canonical">`` is literally ``"undefined"`` and there are no
``og:`` tags. Everything real is in the ``ytInitialData`` blob, so we match
against it directly. Observed on live vs. offline channels:

  * **Live** → the page is a *watch* page: it contains
    ``twoColumnWatchNextResults``, the primary video's id is the first
    ``"videoId"`` after that marker, and the primary view counter renders as
    a live one (``"videoViewCountRenderer":{…,"isLive":true}`` — "N watching
    now"). Anchoring on the primary renderer matters: a sidebar full of other
    people's live videos would otherwise read as live.
  * **Offline** → the page is the *channel* page instead
    (``twoColumnBrowseResultsRenderer``), with no watch-next block and no live
    view counter, so the check falls through to None.
  * **Scheduled** → a watch page with ``"isUpcoming":true``. Announcing that
    link would send viewers to a countdown, so it vetoes the whole check.

Because this parses an HTML page rather than a versioned API, treat a lookup
failure as "unknown for now" — the announcer does, and simply posts the Twitch
link on its own if YouTube never resolves. If YouTube reshapes the page, the
failure mode is a missing follow-up post, never a wrong link.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
_LIVE_URL_BY_HANDLE = "https://www.youtube.com/{handle}/live"
_LIVE_URL_BY_CHANNEL_ID = "https://www.youtube.com/channel/{channel_id}/live"

_REQUEST_TIMEOUT_SEC = 30

# Ask for the page the way a browser would; YouTube serves a stripped-down
# body to obvious bots, and ytInitialData is missing from it.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
        "Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Pre-answers the EU consent interstitial that would otherwise replace the page.
_CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}

# Marks the page as a watch page rather than the channel's browse page.
_WATCH_PAGE_MARKER = "twoColumnWatchNextResults"
# The block holding the primary video, as opposed to the sidebar.
_PRIMARY_INFO_MARKER = "videoPrimaryInfoRenderer"

_VIDEO_ID_RE = re.compile(r'"videoId":"([\w-]{11})"')
_CANONICAL_RE = re.compile(r'<link\s+rel="canonical"\s+href="([^"]+)"', re.IGNORECASE)

# "N watching now" on the primary video — the most reliable liveness signal,
# and present on every live page observed.
_LIVE_VIEW_COUNT_RE = re.compile(
    r'"videoViewCountRenderer":\{.{0,600}?"isLive":true', re.DOTALL
)
# Belt and braces: the player payload says so too, when the page carries one.
_LIVE_BROADCAST_RE = re.compile(
    r'"liveBroadcastDetails":\{[^}]{0,300}?"isLiveNow":true', re.DOTALL
)
_LIVE_DETAILS_RE = re.compile(r'"videoDetails":\{.{0,1500}?"isLive":true', re.DOTALL)
_LIVE_RES = (_LIVE_VIEW_COUNT_RE, _LIVE_BROADCAST_RE, _LIVE_DETAILS_RE)
_UPCOMING_MARKER = '"isUpcoming":true'

_PRIMARY_TITLE_RE = re.compile(
    r'"videoPrimaryInfoRenderer":\{"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"'
)
_DETAILS_TITLE_RE = re.compile(
    r'"videoDetails":\{.{0,400}?"title":"((?:[^"\\]|\\.)*)"', re.DOTALL
)


@dataclass(frozen=True)
class LiveVideo:
    """A YouTube video that is currently live-broadcasting."""

    video_id: str
    title: str = ""

    @property
    def url(self) -> str:
        return _WATCH_URL.format(video_id=self.video_id)


class YouTubeClient:
    """Resolves one channel's currently-live video from its ``/live`` page."""

    def __init__(
        self,
        channel_handle: str = "",
        channel_id: str = "",
        live_url: str = "",
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.channel_handle = self._normalize_handle(channel_handle)
        self.channel_id = channel_id.strip()
        self.live_url = live_url.strip() or self._default_live_url()
        if not self.live_url:
            raise ValueError(
                "YouTubeClient requires a channel handle (@name), a channel id "
                "(UC…), or an explicit live URL."
            )
        self._session = session or requests.Session()
        self._session.headers.update(_BROWSER_HEADERS)
        self._session.cookies.update(_CONSENT_COOKIES)

    @staticmethod
    def _normalize_handle(handle: str) -> str:
        """Return *handle* with exactly one leading ``@`` (or '' when empty)."""
        handle = handle.strip()
        if not handle:
            return ""
        return "@" + handle.lstrip("@")

    def _default_live_url(self) -> str:
        if self.channel_handle:
            return _LIVE_URL_BY_HANDLE.format(handle=self.channel_handle)
        if self.channel_id:
            return _LIVE_URL_BY_CHANNEL_ID.format(channel_id=self.channel_id)
        return ""

    # ------------------------------------------------------------------
    # Live lookup
    # ------------------------------------------------------------------

    def find_live_video(self) -> Optional[LiveVideo]:
        """Return the channel's currently live video, or None if there isn't one.

        Raises:
            requests.HTTPError: If the page can't be fetched. Callers are
                expected to treat that as "unknown for now" rather than fatal.
        """
        logger.debug("Checking %s for a live broadcast.", self.live_url)
        response = self._session.get(
            self.live_url, timeout=_REQUEST_TIMEOUT_SEC, allow_redirects=True
        )
        if not response.ok:
            logger.error(
                "YouTube live-page fetch failed: status=%d, url=%s",
                response.status_code,
                self.live_url,
            )
        response.raise_for_status()

        page = response.text
        if _UPCOMING_MARKER in page:
            logger.debug(
                "%s has a scheduled broadcast that hasn't started; waiting.",
                self.live_url,
            )
            return None

        video_id = _extract_video_id(page, response.url)
        if not video_id:
            logger.debug("No broadcast on %s; channel is not live.", self.live_url)
            return None

        if not any(pattern.search(page) for pattern in _LIVE_RES):
            logger.debug(
                "YouTube video %s is not broadcasting (likely the channel's "
                "last stream or upload); ignoring.",
                video_id,
            )
            return None

        video = LiveVideo(video_id=video_id, title=_extract_title(page))
        logger.info("Found live YouTube video: %s (%s)", video.url, video.title)
        return video

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)


def _extract_video_id(page: str, final_url: str) -> str:
    """Find the id of the video the ``/live`` page resolved to ('' if none)."""
    # Cheapest and most explicit: the request was redirected to the watch page.
    video_id = _video_id_from_url(final_url)
    if video_id:
        return video_id

    # Some responses do carry a real canonical link; most set it to the string
    # "undefined" and fill it in from JavaScript, which _video_id_from_url
    # rejects on its own.
    canonical = _CANONICAL_RE.search(page)
    if canonical:
        video_id = _video_id_from_url(html.unescape(canonical.group(1)))
        if video_id:
            return video_id

    # Otherwise read it out of ytInitialData, anchored past the marker that
    # distinguishes a watch page from the channel's browse page.
    anchor = page.find(_WATCH_PAGE_MARKER)
    if anchor == -1:
        return ""
    primary = page.find(_PRIMARY_INFO_MARKER, anchor)
    match = _VIDEO_ID_RE.search(page, primary if primary != -1 else anchor)
    return match.group(1) if match else ""


def _video_id_from_url(url: str) -> str:
    """Pull the video id out of a watch/youtu.be URL ('' if it isn't one)."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [""])[0]
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.lstrip("/")
    return ""


def _extract_title(page: str) -> str:
    """Best-effort broadcast title; only used for logging, so '' is fine."""
    for pattern in (_PRIMARY_TITLE_RE, _DETAILS_TITLE_RE):
        match = pattern.search(page)
        if not match:
            continue
        raw = match.group(1)
        try:
            # The blob is JSON, so the title arrives with \u… and \" escapes.
            return json.loads(f'"{raw}"')
        except ValueError:
            return raw
    return ""
