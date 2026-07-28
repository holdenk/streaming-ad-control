"""Main monitoring loop tying Twitch stream detection to ad-network control."""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from .config import Config
from .reddit_ad_client import RedditAdClient
from .rules import Rule
from .trafficstars_client import TrafficStarsClient
from .twitch_client import TwitchClient

logger = logging.getLogger(__name__)


def title_has_keyword(title: str, keyword: str) -> bool:
    """Return True when *keyword* appears in *title* (case-insensitive)."""
    return keyword.lower() in title.lower()


def _rule_campaign_count(rule: Rule) -> int:
    return len(rule.campaign_ids) + len(rule.trafficstars_campaign_ids)


class StreamAdMonitor:
    """Polls Twitch and manages ad campaigns based on stream state.

    For each configured rule, the monitor independently tracks whether that
    rule's campaigns are currently enabled. A rule's campaigns are enabled
    when the stream is live **and** the title matches at least one of the
    rule's keywords, and disabled otherwise. Each rule may target Reddit
    campaigns, TrafficStars campaigns, or both.

    State transitions only — network toggles are issued at most once per
    edge, so there are no redundant enable/disable operations.

    Ad-network clients are only constructed for networks that at least one
    rule targets, so e.g. a TrafficStars-only setup never launches the
    headless Chromium that the Reddit client needs.
    """

    def __init__(
        self,
        config: Config,
        twitch_client: Optional[TwitchClient] = None,
        reddit_ad_client: Optional[RedditAdClient] = None,
        trafficstars_client: Optional[TrafficStarsClient] = None,
    ) -> None:
        self.config = config
        self.twitch = twitch_client or TwitchClient(
            config.twitch_client_id,
            config.twitch_client_secret,
        )

        needs_reddit = any(rule.campaign_ids for rule in config.rules)
        needs_trafficstars = any(
            rule.trafficstars_campaign_ids for rule in config.rules
        )

        self.reddit = reddit_ad_client
        if self.reddit is None and needs_reddit:
            self.reddit = RedditAdClient(
                username=config.reddit_username,
                password=config.reddit_password,
                ads_account_id=config.reddit_ads_account_id,
                cookie_jar_path=config.reddit_cookie_jar_path,
                patch_body_pause=config.reddit_patch_body_pause,
                patch_body_resume=config.reddit_patch_body_resume,
            )

        self.trafficstars = trafficstars_client
        if self.trafficstars is None and needs_trafficstars:
            self.trafficstars = TrafficStarsClient(config.trafficstars_api_key)

        # Per-rule enabled flag; indexed in the same order as config.rules.
        self._rule_enabled: List[bool] = [False] * len(config.rules)

    def _rule_targets(self, rule: Rule) -> List[Tuple[str, object, List[str]]]:
        """Yield (network_name, client, campaign_ids) for the rule's networks."""
        targets = []
        if rule.campaign_ids and self.reddit is not None:
            targets.append(("reddit", self.reddit, rule.campaign_ids))
        if rule.trafficstars_campaign_ids and self.trafficstars is not None:
            targets.append(
                ("trafficstars", self.trafficstars, rule.trafficstars_campaign_ids)
            )
        return targets

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
                    "Enabling %d campaign(s).",
                    rule.name,
                    title,
                    _rule_campaign_count(rule),
                )
                for _network, client, campaign_ids in self._rule_targets(rule):
                    for campaign_id in campaign_ids:
                        client.enable_campaign(campaign_id)
                self._rule_enabled[idx] = True

            elif not should_enable and currently_enabled:
                reason = "stream ended" if stream is None else "title no longer matches"
                logger.info(
                    "Rule '%s': disabling %d campaign(s) (%s).",
                    rule.name,
                    _rule_campaign_count(rule),
                    reason,
                )
                for _network, client, campaign_ids in self._rule_targets(rule):
                    for campaign_id in campaign_ids:
                        client.disable_campaign(campaign_id)
                self._rule_enabled[idx] = False

            else:
                logger.debug(
                    "Rule '%s': no state change (enabled=%s, should_enable=%s).",
                    rule.name,
                    currently_enabled,
                    should_enable,
                )

    # ------------------------------------------------------------------
    # Stateless desired-state application (for external triggers)
    # ------------------------------------------------------------------

    def apply(self, title: str, live: bool) -> None:
        """Drive every campaign to its desired state for one ``(title, live)`` snapshot.

        This is the stateless counterpart to :meth:`check`. ``check`` runs
        inside the polling loop and toggles a campaign only on an *edge* (it
        diffs against the in-process ``_rule_enabled`` tracker); ``apply`` is
        for one-shot, externally-triggered invocations — e.g. an OBS
        streaming-start/stop hook — where the process is short-lived and there
        is no prior state to diff against. It therefore sets the desired state
        directly. Re-activating an already-active campaign or pausing an
        already-paused one is idempotent on both networks.

        Default-deny: a campaign is enabled only when ``live`` is True **and**
        the title matches its rule; every other case (offline, off-topic title,
        unknown title) pauses it, so ad spend never runs on a stream that isn't
        live and on-topic.

        Every campaign is attempted even if some toggles raise, so a single
        failure can't strand the others in the wrong state. If any toggle
        failed, a :class:`RuntimeError` summarising them is raised at the end.
        """
        errors: List[str] = []
        for idx, rule in enumerate(self.config.rules):
            should_enable = live and rule.matches_title(title)
            logger.info(
                "Rule '%s': %s %d campaign(s) (live=%s, title=%r).",
                rule.name,
                "enabling" if should_enable else "pausing",
                _rule_campaign_count(rule),
                live,
                title,
            )
            for network, client, campaign_ids in self._rule_targets(rule):
                for campaign_id in campaign_ids:
                    try:
                        if should_enable:
                            client.enable_campaign(campaign_id)
                        else:
                            client.disable_campaign(campaign_id)
                    except Exception as exc:
                        verb = "enable" if should_enable else "disable"
                        logger.exception(
                            "Rule '%s': failed to %s %s campaign '%s'; continuing.",
                            rule.name,
                            verb,
                            network,
                            campaign_id,
                        )
                        errors.append(f"{verb} {network}:{campaign_id} ({exc!r})")
            self._rule_enabled[idx] = should_enable

        if errors:
            raise RuntimeError(
                f"{len(errors)} campaign toggle(s) failed: " + "; ".join(errors)
            )

    def close(self) -> None:
        """Release the network clients (headless browser, HTTP sessions). Idempotent.

        The polling loop runs forever and never calls this, but one-shot callers
        such as the OBS gate should, so the headless Chromium is shut down
        promptly instead of lingering until process exit.
        """
        for client in (self.reddit, self.trafficstars, self.twitch):
            if client is None:
                continue
            closer = getattr(client, "close", None)
            if closer is None:
                continue  # an injected test double need not implement close()
            try:
                closer()
            except Exception:
                logger.debug("Client close raised; ignoring.", exc_info=True)

    # ------------------------------------------------------------------
    # Startup housekeeping
    # ------------------------------------------------------------------

    def disable_all(self) -> None:
        """Disable every campaign across all rules and reset internal state.

        Called once at startup to ensure no stale ads are left running from a
        previous invocation that may have crashed or been stopped mid-session.
        """
        logger.info(
            "Startup: disabling all campaigns across %d rule(s) to ensure clean state.",
            len(self.config.rules),
        )
        for idx, rule in enumerate(self.config.rules):
            for network, client, campaign_ids in self._rule_targets(rule):
                for campaign_id in campaign_ids:
                    try:
                        client.disable_campaign(campaign_id)
                    except Exception:
                        logger.exception(
                            "Startup: failed to disable %s campaign '%s' for "
                            "rule '%s'; continuing.",
                            network,
                            campaign_id,
                            rule.name,
                        )
            self._rule_enabled[idx] = False

    # ------------------------------------------------------------------
    # Continuous monitoring loop
    # ------------------------------------------------------------------

    def run(self, stop_after: Optional[int] = None) -> None:
        """Poll indefinitely (or *stop_after* iterations, useful for testing).

        On first call, all campaigns are unconditionally disabled so that any
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
