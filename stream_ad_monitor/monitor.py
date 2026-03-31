"""Main monitoring loop that ties Twitch stream detection to Reddit ad control."""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import Config
from .reddit_ad_client import RedditAdClient
from .twitch_client import TwitchClient

logger = logging.getLogger(__name__)


def title_has_keyword(title: str, keyword: str) -> bool:
    """Return True when *keyword* appears in *title* (case-insensitive)."""
    return keyword.lower() in title.lower()


class StreamAdMonitor:
    """Polls Twitch and manages a Reddit ad group based on stream state.

    State machine
    -------------
    - When the channel goes *live* **and** the title contains the trigger
      keyword → enable the Reddit ad group.
    - When the channel goes *offline* (or comes back live without the keyword)
      → disable the Reddit ad group.
    - Enables/disables are only issued on transitions to avoid redundant API
      calls.
    """

    def __init__(
        self,
        config: Config,
        twitch_client: Optional[TwitchClient] = None,
        reddit_ad_client: Optional[RedditAdClient] = None,
    ) -> None:
        self.config = config
        self.twitch = twitch_client or TwitchClient(
            config.twitch_client_id,
            config.twitch_client_secret,
        )
        self.reddit = reddit_ad_client or RedditAdClient(
            config.reddit_client_id,
            config.reddit_client_secret,
            config.reddit_ads_account_id,
        )
        # Track whether the ad is currently enabled so we only call the API on
        # state transitions.
        self._ad_enabled: bool = False

    # ------------------------------------------------------------------
    # Single poll cycle (public for testability)
    # ------------------------------------------------------------------

    def check(self) -> None:
        """Perform a single poll cycle: inspect stream state and act if needed."""
        stream = self.twitch.get_stream(self.config.twitch_channel_login)
        should_enable = (
            stream is not None
            and title_has_keyword(
                stream.get("title", ""), self.config.trigger_keyword
            )
        )

        if should_enable and not self._ad_enabled:
            logger.info(
                "Stream is live with matching title '%s'. Enabling Reddit ad group.",
                stream.get("title"),
            )
            self.reddit.enable_ad_group(self.config.reddit_ad_group_id)
            self._ad_enabled = True

        elif not should_enable and self._ad_enabled:
            reason = "stream ended" if stream is None else "title no longer matches"
            logger.info(
                "Ad group should be disabled (%s). Disabling Reddit ad group.",
                reason,
            )
            self.reddit.disable_ad_group(self.config.reddit_ad_group_id)
            self._ad_enabled = False

        else:
            logger.debug(
                "No state change (ad_enabled=%s, should_enable=%s).",
                self._ad_enabled,
                should_enable,
            )

    # ------------------------------------------------------------------
    # Continuous monitoring loop
    # ------------------------------------------------------------------

    def run(self, stop_after: Optional[int] = None) -> None:
        """Poll indefinitely (or *stop_after* iterations, useful for testing).

        Args:
            stop_after: If given, stop after this many poll cycles.
        """
        logger.info(
            "Starting monitor for channel '%s' (keyword=%r, interval=%ds).",
            self.config.twitch_channel_login,
            self.config.trigger_keyword,
            self.config.poll_interval,
        )
        iteration = 0
        while stop_after is None or iteration < stop_after:
            try:
                self.check()
            except Exception:
                logger.exception("Error during poll cycle; will retry.")
            iteration += 1
            if stop_after is None or iteration < stop_after:
                time.sleep(self.config.poll_interval)
