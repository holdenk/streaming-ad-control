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
       See ``rules.example.yaml`` for the expected structure.
    2. **Legacy env vars** – set ``REDDIT_CAMPAIGN_ID`` (required) and
       ``TRIGGER_KEYWORD`` (optional, default ``Spark``). A single rule is
       synthesised automatically from these values. ``REDDIT_AD_GROUP_ID``
       is accepted as a deprecated alias.
    """

    def __init__(self) -> None:
        # Twitch settings
        self.twitch_client_id: str = _require("TWITCH_CLIENT_ID")
        self.twitch_client_secret: str = _require("TWITCH_CLIENT_SECRET")
        self.twitch_channel_login: str = _require("TWITCH_CHANNEL_LOGIN")

        # Reddit Ads settings — selenium drives the ads.reddit.com dashboard.
        self.reddit_username: str = _require("REDDIT_USERNAME")
        self.reddit_password: str = _require("REDDIT_PASSWORD")
        # Optional: where to persist cookies + localStorage between runs so
        # we don't re-login every poll. Empty string disables persistence.
        self.reddit_cookie_jar_path: str = _sanitize(
            os.environ.get("REDDIT_COOKIE_JAR", "")
        )
        # Optional: override the JSON body sent on the campaigns PATCH if the
        # dashboard's exact shape differs from the documented default.
        self.reddit_patch_body_pause: str = _sanitize(
            os.environ.get("REDDIT_PATCH_BODY_PAUSE", _DEFAULT_PATCH_BODY_PAUSE)
        )
        self.reddit_patch_body_resume: str = _sanitize(
            os.environ.get("REDDIT_PATCH_BODY_RESUME", _DEFAULT_PATCH_BODY_RESUME)
        )

        # How often to poll Twitch (seconds)
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "60"))

        # Load rules ---------------------------------------------------
        rules_file = os.environ.get("RULES_FILE")
        if rules_file:
            self.rules: List[Rule] = load_rules_from_yaml(rules_file)
        else:
            campaign_id = (
                os.environ.get("REDDIT_CAMPAIGN_ID")
                or os.environ.get("REDDIT_AD_GROUP_ID")
            )
            if not campaign_id:
                raise ValueError(
                    "Required environment variable 'REDDIT_CAMPAIGN_ID' is not set "
                    "(and no RULES_FILE was provided)."
                )
            keyword = os.environ.get("TRIGGER_KEYWORD", "Spark")
            self.rules = [
                Rule(
                    name="default",
                    keywords=[keyword],
                    campaign_ids=[_sanitize(campaign_id)],
                )
            ]

        logger.info(
            "Config loaded: twitch_client_id=%s, reddit_user=%s, "
            "channel=%s, poll_interval=%d, rules=%d, cookie_jar=%s",
            mask_credential(self.twitch_client_id),
            mask_credential(self.reddit_username),
            self.twitch_channel_login,
            self.poll_interval,
            len(self.rules),
            self.reddit_cookie_jar_path or "<not persisted>",
        )
