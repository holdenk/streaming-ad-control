"""Configuration loaded from environment variables (and optionally a YAML rules file)."""

import logging
import os
from typing import List

from . import mask_credential
from .announcer import (
    DEFAULT_TEMPLATE,
    DEFAULT_YOUTUBE_TEMPLATE,
    AnnounceSettings,
)
from .networks import NETWORKS
from .rules import MATCH_MODES, Rule, load_rules_from_yaml

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


def _optional(name: str, default: str = "") -> str:
    """Return a sanitized optional env var, or *default* when unset/empty."""
    return _sanitize(os.environ.get(name, "")) or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _optional(name)
    if not raw:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    logger.warning(
        "Environment variable '%s'=%r is not a boolean; using default %s.",
        name,
        raw,
        default,
    )
    return default


def _env_int(name: str, default: int) -> int:
    raw = _optional(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Environment variable '%s'=%r is not an integer; using default %d.",
            name,
            raw,
            default,
        )
        return default


def _env_list(name: str) -> List[str]:
    """Split a comma-separated env var into a list of non-empty entries."""
    return [part.strip() for part in _optional(name).split(",") if part.strip()]


def _env_template(name: str, default: str) -> str:
    """Read a message template, turning a literal ``\\n`` into a real newline.

    Templates live in an env file (systemd ``EnvironmentFile=``), which has no
    way to express a multi-line value, so ``\\n`` is spelled out there.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = _sanitize(raw)
    if not raw:
        return default
    return raw.replace("\\n", "\n")


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
    * ExoClick / PropellerAds / Meta: ``EXOCLICK_API_TOKEN`` /
      ``PROPELLERADS_API_TOKEN`` / ``META_ACCESS_TOKEN`` (see
      :mod:`stream_ad_monitor.networks`)

    Go-live announcements are independent of the rules and off until
    credentials appear: set ``TWITTER_*``, ``BLUESKY_*``, and/or
    ``MASTODON_ACCESS_TOKEN`` to post the stream link, plus
    ``YOUTUBE_CHANNEL_HANDLE`` to follow up with the simulcast link. See
    :class:`~stream_ad_monitor.announcer.AnnounceSettings`.
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

        needed = {
            key for rule in self.rules for key in rule.campaigns_by_network()
        }
        needs_reddit = "reddit" in needed

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

        # REST networks (TrafficStars, ExoClick, PropellerAds, Meta): one token
        # each, only required when a rule targets that network. Sets e.g.
        # ``self.trafficstars_api_key`` / ``self.meta_access_token``.
        self.trafficstars_api_key: str = ""
        self.exoclick_api_token: str = ""
        self.propellerads_api_token: str = ""
        self.meta_access_token: str = ""
        for network in NETWORKS:
            if network.credential_env is None:
                continue
            value = _require(network.credential_env) if network.key in needed else ""
            setattr(self, network.config_attr, value)

        # Go-live announcements (X / Bluesky / Mastodon). Entirely optional:
        # with no
        # credentials configured the monitor behaves exactly as before.
        self.announce: AnnounceSettings = self._load_announce_settings()

        logger.info(
            "Config loaded: twitch_client_id=%s, reddit_user=%s, "
            "networks=%s, channel=%s, poll_interval=%d, rules=%d, "
            "cookie_jar=%s",
            mask_credential(self.twitch_client_id) if self.twitch_client_id else "<unused>",
            mask_credential(self.reddit_username)
            if needs_reddit and self.reddit_username
            else ("<cookie-jar>" if needs_reddit else "<unused>"),
            ",".join(n.key for n in NETWORKS if n.key in needed) or "<none>",
            self.twitch_channel_login,
            self.poll_interval,
            len(self.rules),
            self.reddit_cookie_jar_path or "<not persisted>",
        )
        logger.info(
            "Announcements: %s (x=%s, bluesky=%s, mastodon=%s, youtube_lookup=%s).",
            "enabled" if self.announce.enabled and self.announce.any_target_configured
            else "disabled",
            "on" if self.announce.twitter_configured else "off",
            "on" if self.announce.bluesky_configured else "off",
            "on" if self.announce.mastodon_configured else "off",
            "on" if self.announce.youtube_configured else "off",
        )

    @staticmethod
    def _legacy_rule_from_env() -> Rule:
        """Synthesise a single rule from the pre-YAML environment variables."""
        ids_by_attr = {}
        for network in NETWORKS:
            value = os.environ.get(network.legacy_env)
            if network.key == "reddit":
                value = value or os.environ.get("REDDIT_AD_GROUP_ID")
            ids_by_attr[network.rule_attr] = [_sanitize(value)] if value else []
        if not any(ids_by_attr.values()):
            others = ", ".join(f"'{n.legacy_env}'" for n in NETWORKS[1:])
            raise ValueError(
                "Required environment variable 'REDDIT_CAMPAIGN_ID' (or one of "
                f"{others}) is not set, and no RULES_FILE was provided."
            )
        keyword = os.environ.get("TRIGGER_KEYWORD", "Spark")
        match = _optional("TRIGGER_MATCH", "word").lower()
        if match not in MATCH_MODES:
            raise ValueError(
                f"TRIGGER_MATCH must be one of {MATCH_MODES}, got {match!r}."
            )
        return Rule(
            name="default",
            keywords=[keyword],
            exclude_keywords=_env_list("TRIGGER_EXCLUDE_KEYWORDS"),
            match=match,
            **ids_by_attr,
        )

    @staticmethod
    def _load_announce_settings() -> AnnounceSettings:
        """Read the go-live announcement settings from the environment.

        Every value is optional. Announcements only run once at least one of
        the X, Bluesky, or Mastodon credential sets is complete, so an existing
        deployment picks up nothing new until it opts in.
        """
        return AnnounceSettings(
            enabled=_env_bool("ANNOUNCE_ENABLED", True),
            keywords=_env_list("ANNOUNCE_KEYWORDS"),
            template=_env_template("ANNOUNCE_TEMPLATE", DEFAULT_TEMPLATE),
            youtube_template=_env_template(
                "ANNOUNCE_YOUTUBE_TEMPLATE", DEFAULT_YOUTUBE_TEMPLATE
            ),
            title_max_chars=_env_int("ANNOUNCE_TITLE_MAX_CHARS", 140),
            wait_for_youtube_sec=_env_int("ANNOUNCE_WAIT_FOR_YOUTUBE_SEC", 0),
            youtube_lookup_interval=_env_int("YOUTUBE_LOOKUP_INTERVAL", 60),
            youtube_lookup_timeout=_env_int("YOUTUBE_LOOKUP_TIMEOUT", 1800),
            state_path=_optional("ANNOUNCE_STATE_FILE"),
            twitter_api_key=_optional("TWITTER_API_KEY"),
            twitter_api_secret=_optional("TWITTER_API_SECRET"),
            twitter_access_token=_optional("TWITTER_ACCESS_TOKEN"),
            twitter_access_token_secret=_optional("TWITTER_ACCESS_TOKEN_SECRET"),
            bluesky_handle=_optional("BLUESKY_HANDLE"),
            bluesky_app_password=_optional("BLUESKY_APP_PASSWORD"),
            bluesky_pds_url=_optional("BLUESKY_PDS_URL"),
            mastodon_access_token=_optional("MASTODON_ACCESS_TOKEN"),
            mastodon_instance_url=_optional("MASTODON_INSTANCE_URL"),
            mastodon_visibility=_optional("MASTODON_VISIBILITY"),
            mastodon_max_chars=_env_int("MASTODON_MAX_CHARS", 0),
            youtube_channel_handle=_optional("YOUTUBE_CHANNEL_HANDLE"),
            youtube_channel_id=_optional("YOUTUBE_CHANNEL_ID"),
            youtube_live_url=_optional("YOUTUBE_LIVE_URL"),
        )
