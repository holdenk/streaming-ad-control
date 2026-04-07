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


class Config:
    """Holds all runtime configuration for the stream-ad monitor.

    Rules can be supplied in one of two ways (in order of precedence):

    1. **YAML file** – set ``RULES_FILE`` to the path of a YAML file.
       See ``rules.example.yaml`` for the expected structure.
    2. **Legacy env vars** – set ``REDDIT_AD_GROUP_ID`` (required) and
       ``TRIGGER_KEYWORD`` (optional, default ``Spark``).  A single rule is
       synthesised automatically from these values.
    """

    def __init__(self) -> None:
        # Twitch settings
        self.twitch_client_id: str = _require("TWITCH_CLIENT_ID")
        self.twitch_client_secret: str = _require("TWITCH_CLIENT_SECRET")
        self.twitch_channel_login: str = _require("TWITCH_CHANNEL_LOGIN")

        # Reddit Ads settings
        self.reddit_client_id: str = _require("REDDIT_CLIENT_ID")
        self.reddit_client_secret: str = _require("REDDIT_CLIENT_SECRET")
        self.reddit_ads_account_id: str = _require("REDDIT_ADS_ACCOUNT_ID")
        self.reddit_username: str = os.environ.get("REDDIT_USERNAME", "")

        # How often to poll Twitch (seconds)
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "60"))

        # Load rules ---------------------------------------------------
        rules_file = os.environ.get("RULES_FILE")
        if rules_file:
            self.rules: List[Rule] = load_rules_from_yaml(rules_file)
        else:
            # Legacy single-rule mode: require REDDIT_AD_GROUP_ID and
            # optionally TRIGGER_KEYWORD.
            ad_group_id = _require("REDDIT_AD_GROUP_ID")
            keyword = os.environ.get("TRIGGER_KEYWORD", "Spark")
            self.rules = [
                Rule(
                    name="default",
                    keywords=[keyword],
                    ad_group_ids=[ad_group_id],
                )
            ]

        logger.info(
            "Config loaded: twitch_client_id=%s, reddit_client_id=%s, "
            "reddit_ads_account_id=%s, channel=%s, poll_interval=%d, rules=%d",
            mask_credential(self.twitch_client_id),
            mask_credential(self.reddit_client_id),
            self.reddit_ads_account_id,
            self.twitch_channel_login,
            self.poll_interval,
            len(self.rules),
        )
