"""Configuration loaded from environment variables (and optionally a YAML rules file)."""

import logging
import os
from typing import List

from . import mask_credential
from .rules import Rule, load_rules_from_yaml

logger = logging.getLogger(__name__)


def _sanitize(value: str) -> str:
    """Strip surrounding quotes, whitespace, and carriage returns from a value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    sanitized = _sanitize(value)
    if sanitized != value:
        logger.warning(
            "Environment variable '%s' was sanitized (had extra quotes/whitespace).",
            name,
        )
    return sanitized


# Default body shapes for the campaign-state PATCH. Verified against
# ads-api.reddit.com — the body must wrap the field in a "data" key, and
# the byte length matches the dashboard's captured PATCH (content-length: 39).
_DEFAULT_PATCH_BODY_PAUSE = '{"data":{"configured_status":"PAUSED"}}'
_DEFAULT_PATCH_BODY_RESUME = '{"data":{"configured_status":"ACTIVE"}}'


class Config:
    """Holds all runtime configuration for the stream-ad monitor.

    Rules can be supplied in one of two ways (in order of precedence):

    1. **YAML file** – set ``RULES_FILE`` to the path of a YAML file.
       See ``rules.example.yaml`` for the expected structure. Rules may
       target Reddit campaigns (``campaign_ids``), TrafficStars campaigns
       (``trafficstars_campaign_ids``), or both.
    2. **Legacy env vars** – set ``REDDIT_CAMPAIGN_ID`` and/or
       ``TRAFFICSTARS_CAMPAIGN_ID`` plus ``TRIGGER_KEYWORD`` (optional,
       default ``Spark``). A single rule is synthesised automatically from
       these values. ``REDDIT_AD_GROUP_ID`` is accepted as a deprecated
       alias for ``REDDIT_CAMPAIGN_ID``.

    Per-network credentials are only required when at least one rule
    targets that network:

    * Reddit: ``REDDIT_USERNAME`` + ``REDDIT_PASSWORD``. The ads account id
      is auto-discovered after login; ``REDDIT_ADS_ACCOUNT_ID`` is an
      optional override.
    * TrafficStars: ``TRAFFICSTARS_API_KEY`` (generate on
      https://admin.trafficstars.com/profile/)
    """

    def __init__(self, *, require_twitch: bool = True) -> None:
        # Twitch settings. The polling daemon always needs these. Externally
        # triggered callers that don't poll Twitch — e.g. the OBS gate handling
        # a stop event, or a start event with an explicit --title — pass
        # ``require_twitch=False`` so a missing Twitch credential can't stop
        # them from pausing (or explicitly toggling) a campaign.
        if require_twitch:
            self.twitch_client_id: str = _require("TWITCH_CLIENT_ID")
            self.twitch_client_secret: str = _require("TWITCH_CLIENT_SECRET")
            self.twitch_channel_login: str = _require("TWITCH_CHANNEL_LOGIN")
        else:
            self.twitch_client_id = _sanitize(os.environ.get("TWITCH_CLIENT_ID", ""))
            self.twitch_client_secret = _sanitize(
                os.environ.get("TWITCH_CLIENT_SECRET", "")
            )
            self.twitch_channel_login = _sanitize(
                os.environ.get("TWITCH_CHANNEL_LOGIN", "")
            )

        # How often to poll Twitch (seconds)
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "60"))

        # Load rules ---------------------------------------------------
        rules_file = os.environ.get("RULES_FILE")
        if rules_file:
            self.rules: List[Rule] = load_rules_from_yaml(rules_file)
        else:
            self.rules = [self._legacy_rule_from_env()]

        needs_reddit = any(rule.campaign_ids for rule in self.rules)
        needs_trafficstars = any(
            rule.trafficstars_campaign_ids for rule in self.rules
        )

        # Reddit Ads settings — selenium drives the ads.reddit.com dashboard.
        # Optional: where to persist cookies + localStorage between runs so we
        # don't re-login every poll. Empty string disables persistence. Read
        # first because it decides whether the login credentials are required.
        self.reddit_cookie_jar_path: str = _sanitize(
            os.environ.get("REDDIT_COOKIE_JAR", "")
        )
        # Username/password are only required when a rule targets Reddit *and*
        # there's no bootstrapped cookie jar to restore a session from. With a
        # cookie jar (the recommended setup — see bootstrap_reddit_session.py)
        # the client runs headlessly off the saved session and never needs
        # them; this mirrors RedditAdClient's own constructor contract.
        if needs_reddit and not self.reddit_cookie_jar_path:
            self.reddit_username: str = _require("REDDIT_USERNAME")
            self.reddit_password: str = _require("REDDIT_PASSWORD")
        else:
            self.reddit_username = _sanitize(os.environ.get("REDDIT_USERNAME", ""))
            self.reddit_password = _sanitize(os.environ.get("REDDIT_PASSWORD", ""))
        # Optional. The ads account id from the dashboard URL
        # (ads.reddit.com/account/<id>/dashboard). When unset, the client
        # auto-discovers it after login (a logged-in hit on ads.reddit.com
        # redirects to the account-scoped dashboard). Set it to skip discovery
        # or to pin a specific account when the login has more than one.
        self.reddit_ads_account_id: str = _sanitize(
            os.environ.get("REDDIT_ADS_ACCOUNT_ID", "")
        )
        # Optional: override the JSON body sent on the campaigns PATCH if the
        # dashboard's exact shape differs from the documented default.
        self.reddit_patch_body_pause: str = _sanitize(
            os.environ.get("REDDIT_PATCH_BODY_PAUSE", _DEFAULT_PATCH_BODY_PAUSE)
        )
        self.reddit_patch_body_resume: str = _sanitize(
            os.environ.get("REDDIT_PATCH_BODY_RESUME", _DEFAULT_PATCH_BODY_RESUME)
        )

        # TrafficStars settings — plain REST API, authenticated with the
        # account API key. Only required when a rule targets TrafficStars.
        self.trafficstars_api_key: str = (
            _require("TRAFFICSTARS_API_KEY") if needs_trafficstars else ""
        )

        logger.info(
            "Config loaded: twitch_client_id=%s, reddit_user=%s, "
            "trafficstars_key=%s, channel=%s, poll_interval=%d, rules=%d, "
            "cookie_jar=%s",
            mask_credential(self.twitch_client_id) if self.twitch_client_id else "<unused>",
            mask_credential(self.reddit_username)
            if needs_reddit and self.reddit_username
            else ("<cookie-jar>" if needs_reddit else "<unused>"),
            mask_credential(self.trafficstars_api_key)
            if needs_trafficstars
            else "<unused>",
            self.twitch_channel_login,
            self.poll_interval,
            len(self.rules),
            self.reddit_cookie_jar_path or "<not persisted>",
        )

    @staticmethod
    def _legacy_rule_from_env() -> Rule:
        """Synthesise a single rule from the pre-YAML environment variables."""
        reddit_campaign_id = (
            os.environ.get("REDDIT_CAMPAIGN_ID")
            or os.environ.get("REDDIT_AD_GROUP_ID")
        )
        trafficstars_campaign_id = os.environ.get("TRAFFICSTARS_CAMPAIGN_ID")
        if not reddit_campaign_id and not trafficstars_campaign_id:
            raise ValueError(
                "Required environment variable 'REDDIT_CAMPAIGN_ID' (or "
                "'TRAFFICSTARS_CAMPAIGN_ID') is not set, and no RULES_FILE "
                "was provided."
            )
        keyword = os.environ.get("TRIGGER_KEYWORD", "Spark")
        return Rule(
            name="default",
            keywords=[keyword],
            campaign_ids=(
                [_sanitize(reddit_campaign_id)] if reddit_campaign_id else []
            ),
            trafficstars_campaign_ids=(
                [_sanitize(trafficstars_campaign_id)]
                if trafficstars_campaign_id
                else []
            ),
        )
