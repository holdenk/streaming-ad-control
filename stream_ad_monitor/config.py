"""Configuration loaded from environment variables."""

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    return value


class Config:
    """Holds all runtime configuration for the stream-ad monitor."""

    def __init__(self) -> None:
        # Twitch settings
        self.twitch_client_id: str = _require("TWITCH_CLIENT_ID")
        self.twitch_client_secret: str = _require("TWITCH_CLIENT_SECRET")
        self.twitch_channel_login: str = _require("TWITCH_CHANNEL_LOGIN")

        # Reddit Ads settings
        self.reddit_client_id: str = _require("REDDIT_CLIENT_ID")
        self.reddit_client_secret: str = _require("REDDIT_CLIENT_SECRET")
        self.reddit_ads_account_id: str = _require("REDDIT_ADS_ACCOUNT_ID")
        # The specific ad group to enable/disable
        self.reddit_ad_group_id: str = _require("REDDIT_AD_GROUP_ID")

        # How often to poll Twitch (seconds)
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "60"))

        # Keyword that triggers the Reddit ad (case-insensitive)
        self.trigger_keyword: str = os.environ.get("TRIGGER_KEYWORD", "Spark")
