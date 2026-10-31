"""Main monitoring loop tying Twitch stream detection to ad-network control."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from .announcer import StreamAnnouncer, build_announcer
from .config import Config
from .networks import NETWORKS_BY_KEY
from .reddit_ad_client import RedditAdClient
from .rules import Rule
from .trafficstars_client import TrafficStarsClient
from .twitch_client import TwitchClient

logger = logging.getLogger(__name__)

# (network key, campaign id) — one physical campaign.
CampaignKey = Tuple[str, str]


def title_has_keyword(title: str, keyword: str) -> bool:
    """Return True when *keyword* appears in *title* (case-insensitive substring)."""
    return keyword.lower() in title.lower()


class StreamAdMonitor:
    """Polls Twitch and manages ad campaigns based on stream state.

    A campaign should run when the stream is live **and** at least one rule
    that lists it matches the title; otherwise it should be paused. State is
    tracked per campaign, not per rule, so a campaign shared by two rules
    (say an FHI rule and a Spark rule) stays on while either matches instead
    of the two rules fighting over it.

    Toggles are issued only on a campaign's state edge. Each toggle is
    isolated: one failing campaign is logged and retried next poll, and never
    stops the others from being enabled or paused.

    Ad-network clients are only constructed for networks that at least one
    rule targets, so e.g. a TrafficStars-only setup never launches the
    headless Chromium that the Reddit client needs.

    The same poll also feeds the optional :class:`StreamAnnouncer`, which
    posts the stream links to X, Bluesky, and Mastodon. Announcements are
    best-effort and never interfere with ad control.
    """

    def __init__(
        self,
        config: Config,
        twitch_client: Optional[TwitchClient] = None,
        reddit_ad_client: Optional[RedditAdClient] = None,
        trafficstars_client: Optional[TrafficStarsClient] = None,
        announcer: Optional[StreamAnnouncer] = None,
        ad_clients: Optional[Dict[str, object]] = None,
    ) -> None:
        self.config = config
        self.twitch = twitch_client or TwitchClient(
            config.twitch_client_id,
            config.twitch_client_secret,
        )

        needed = {
            key for rule in config.rules for key in rule.campaigns_by_network()
        }

        # Injected clients win (tests, callers sharing a client); anything
        # else a rule needs is built here.
        self.clients: Dict[str, object] = dict(ad_clients or {})
        if reddit_ad_client is not None:
            self.clients["reddit"] = reddit_ad_client
        if trafficstars_client is not None:
            self.clients["trafficstars"] = trafficstars_client
        for key in needed - set(self.clients):
            if key == "reddit":
                self.clients[key] = RedditAdClient(
                    username=config.reddit_username,
                    password=config.reddit_password,
                    ads_account_id=config.reddit_ads_account_id,
                    cookie_jar_path=config.reddit_cookie_jar_path,
                    patch_body_pause=config.reddit_patch_body_pause,
                    patch_body_resume=config.reddit_patch_body_resume,
                )
            else:
                network = NETWORKS_BY_KEY[key]
                self.clients[key] = network.factory(
                    getattr(config, network.config_attr)
                )
        self.reddit = self.clients.get("reddit")
        self.trafficstars = self.clients.get("trafficstars")

        # Announcements are opt-in: build_announcer returns None unless a
        # social platform's credentials are configured.
        self.announcer = announcer
        if self.announcer is None:
            self.announcer = build_announcer(
                config.twitch_channel_login, config.announce
            )

        # Per-rule "currently matching" flag, indexed like config.rules. Used
        # for logging rule transitions; the toggles key off _campaign_enabled.
        self._rule_enabled: List[bool] = [False] * len(config.rules)
        # Last known state per campaign. Missing means paused (run() pauses
        # everything at startup); None means a toggle failed and the state
        # is unknown, which forces a retry on the next poll.
        self._campaign_enabled: Dict[CampaignKey, Optional[bool]] = {}

        self._log_shared_campaigns()

    def _log_shared_campaigns(self) -> None:
        owners: Dict[CampaignKey, List[str]] = {}
        for rule in self.config.rules:
            for key in self._rule_campaigns(rule):
                owners.setdefault(key, []).append(rule.name)
        for (network, cid), names in owners.items():
            if len(names) > 1:
                logger.info(
                    "%s campaign %s is shared by rules %s; it runs while any of "
                    "them matches.",
                    NETWORKS_BY_KEY[network].label,
                    cid,
                    names,
                )

    def _rule_campaigns(self, rule: Rule) -> List[CampaignKey]:
        """(network, campaign_id) pairs for the rule's networks that have a client."""
        return [
            (network, cid)
            for network, ids in rule.campaigns_by_network().items()
            if self.clients.get(network) is not None
            for cid in ids
        ]

    def _desired_states(self, title: str, live: bool) -> Dict[CampaignKey, bool]:
        """Desired state of every campaign: on iff some matching rule lists it.

        Also records and logs per-rule match transitions.
        """
        desired: Dict[CampaignKey, bool] = {}
        for idx, rule in enumerate(self.config.rules):
            matched = live and rule.matches_title(title)
            if matched != self._rule_enabled[idx]:
                logger.info(
                    "Rule '%s': %s (%d campaign(s)).",
                    rule.name,
                    f"live with matching title {title!r}"
                    if matched
                    else ("stream ended" if not live else "title no longer matches"),
                    rule.campaign_count(),
                )
            self._rule_enabled[idx] = matched
            for key in self._rule_campaigns(rule):
                desired[key] = desired.get(key, False) or matched
        return desired

    def _toggle(self, key: CampaignKey, enable: bool) -> Optional[str]:
        """Drive one campaign; return an error description or None on success."""
        network, cid = key
        client = self.clients[network]
        try:
            if enable:
                client.enable_campaign(cid)
            else:
                client.disable_campaign(cid)
        except Exception as exc:
            verb = "enable" if enable else "disable"
            logger.exception(
                "Failed to %s %s campaign '%s'; will retry.",
                verb,
                NETWORKS_BY_KEY[network].label,
                cid,
            )
            self._campaign_enabled[key] = None
            return f"{verb} {network}:{cid} ({exc!r})"
        self._campaign_enabled[key] = enable
        return None

    # ------------------------------------------------------------------
    # Single poll cycle (public for testability)
    # ------------------------------------------------------------------

    def check(self) -> None:
        """Perform a single poll cycle: inspect stream state and act on edges."""
        stream = self.twitch.get_stream(self.config.twitch_channel_login)
        title = stream.get("title", "") if stream is not None else ""

        for key, want in self._desired_states(title, stream is not None).items():
            if self._campaign_enabled.get(key, False) is want:
                continue
            self._toggle(key, want)

        # Announce last: a social post or a YouTube lookup can each sit on a
        # 30s timeout, and the campaign toggles shouldn't queue behind that on
        # the poll that flips the stream live.
        self._announce(stream)

    def _announce(self, stream: Optional[dict]) -> None:
        """Feed the poll result to the announcer, swallowing any failure.

        Posting to a social platform is strictly secondary to keeping the ad
        campaigns in the right state, so nothing that happens in here is
        allowed to abort the poll cycle.
        """
        if self.announcer is None:
            return
        try:
            self.announcer.handle_stream(stream)
        except Exception:
            logger.exception(
                "Stream announcement failed; ad control is unaffected."
            )

    # ------------------------------------------------------------------
    # Stateless desired-state application (for external triggers)
    # ------------------------------------------------------------------

    def apply(self, title: str, live: bool) -> None:
        """Drive every campaign to its desired state for one ``(title, live)`` snapshot.

        This is the stateless counterpart to :meth:`check`. ``check`` runs
        inside the polling loop and toggles a campaign only on an *edge*;
        ``apply`` is for one-shot, externally-triggered invocations — e.g. an
        OBS streaming-start/stop hook — where the process is short-lived and
        there is no prior state to diff against. It therefore sets every
        campaign's desired state directly. Re-activating an already-active
        campaign or pausing an already-paused one is idempotent.

        Default-deny: a campaign is enabled only when ``live`` is True **and**
        a rule listing it matches the title; every other case pauses it, so
        ad spend never runs on a stream that isn't live and on-topic.

        Every campaign is attempted even if some toggles raise. If any
        failed, a :class:`RuntimeError` summarising them is raised at the end.
        """
        errors = [
            err
            for key, want in self._desired_states(title, live).items()
            if (err := self._toggle(key, want)) is not None
        ]
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
        for client in [*self.clients.values(), self.twitch]:
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
        """Pause every campaign across all rules and reset internal state.

        Called once at startup to ensure no stale ads are left running from a
        previous invocation that may have crashed or been stopped mid-session.
        A campaign that fails to pause is marked unknown, so the first poll
        retries it if it should be off.
        """
        logger.info(
            "Startup: disabling all campaigns across %d rule(s) to ensure clean state.",
            len(self.config.rules),
        )
        keys = dict.fromkeys(
            key for rule in self.config.rules for key in self._rule_campaigns(rule)
        )
        for key in keys:
            self._toggle(key, False)
        self._rule_enabled = [False] * len(self.config.rules)

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
