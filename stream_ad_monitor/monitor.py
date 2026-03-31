"""Main monitoring loop that ties Twitch stream detection to Reddit ad control."""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .config import Config
from .reddit_ad_client import RedditAdClient
from .rules import Rule
from .twitch_client import TwitchClient

logger = logging.getLogger(__name__)


def title_has_keyword(title: str, keyword: str) -> bool:
    """Return True when *keyword* appears in *title* (case-insensitive)."""
    return keyword.lower() in title.lower()


class StreamAdMonitor:
    """Polls Twitch and manages Reddit ad groups based on stream state.

    For each configured rule, the monitor independently tracks whether that
    rule's ad groups are currently enabled.  A rule's ad groups are enabled
    when the stream is live **and** the title matches at least one of the
    rule's keywords, and disabled otherwise.

    State transitions only — API calls are issued at most once per edge so
    there are no redundant enable/disable requests.
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
        # Per-rule enabled flag; indexed in the same order as config.rules.
        self._rule_enabled: List[bool] = [False] * len(config.rules)

    # ------------------------------------------------------------------
    # Single poll cycle (public for testability)
    # ------------------------------------------------------------------

    def check(self) -> None:
        """Perform a single poll cycle: inspect stream state and act if needed."""
        stream = self.twitch.get_stream(self.config.twitch_channel_login)
        title = stream.get("title", "") if stream is not None else ""

        for idx, rule in enumerate(self.config.rules):
            should_enable = stream is not None and rule.matches_title(title)
            currently_enabled = self._rule_enabled[idx]

            if should_enable and not currently_enabled:
                logger.info(
                    "Rule '%s': stream is live with matching title '%s'. "
                    "Enabling %d ad group(s).",
                    rule.name,
                    title,
                    len(rule.ad_group_ids),
                )
                for ad_group_id in rule.ad_group_ids:
                    self.reddit.enable_ad_group(ad_group_id)
                self._rule_enabled[idx] = True

            elif not should_enable and currently_enabled:
                reason = "stream ended" if stream is None else "title no longer matches"
                logger.info(
                    "Rule '%s': disabling %d ad group(s) (%s).",
                    rule.name,
                    len(rule.ad_group_ids),
                    reason,
                )
                for ad_group_id in rule.ad_group_ids:
                    self.reddit.disable_ad_group(ad_group_id)
                self._rule_enabled[idx] = False

            else:
                logger.debug(
                    "Rule '%s': no state change (enabled=%s, should_enable=%s).",
                    rule.name,
                    currently_enabled,
                    should_enable,
                )

    # ------------------------------------------------------------------
    # Startup housekeeping
    # ------------------------------------------------------------------

    def disable_all(self) -> None:
        """Disable every ad group across all rules and reset internal state.

        Called once at startup to ensure no stale ads are left running from a
        previous invocation that may have crashed or been stopped mid-session.
        """
        logger.info(
            "Startup: disabling all ad groups across %d rule(s) to ensure clean state.",
            len(self.config.rules),
        )
        for idx, rule in enumerate(self.config.rules):
            for ad_group_id in rule.ad_group_ids:
                try:
                    self.reddit.disable_ad_group(ad_group_id)
                except Exception:
                    logger.exception(
                        "Startup: failed to disable ad group '%s' for rule '%s'; continuing.",
                        ad_group_id,
                        rule.name,
                    )
            self._rule_enabled[idx] = False

    # ------------------------------------------------------------------
    # Continuous monitoring loop
    # ------------------------------------------------------------------

    def run(self, stop_after: Optional[int] = None) -> None:
        """Poll indefinitely (or *stop_after* iterations, useful for testing).

        On first call, all ad groups are unconditionally disabled so that any
        ads left running from a previous (possibly crashed) session are cleaned
        up before the state machine takes over.

        Args:
            stop_after: If given, stop after this many poll cycles.
        """
        rule_summary = ", ".join(
            f"'{r.name}' ({r.keywords})" for r in self.config.rules
        )
        logger.info(
            "Starting monitor for channel '%s' with %d rule(s): [%s] (interval=%ds).",
            self.config.twitch_channel_login,
            len(self.config.rules),
            rule_summary,
            self.config.poll_interval,
        )
        self.disable_all()
        iteration = 0
        while stop_after is None or iteration < stop_after:
            try:
                self.check()
            except Exception:
                logger.exception("Error during poll cycle; will retry.")
            iteration += 1
            if stop_after is None or iteration < stop_after:
                time.sleep(self.config.poll_interval)
