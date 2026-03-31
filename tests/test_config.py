"""Tests for stream_ad_monitor.config."""

import os
import pytest
from unittest.mock import patch

from stream_ad_monitor.config import Config


REQUIRED_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "some_channel",
    "REDDIT_CLIENT_ID": "reddit_id",
    "REDDIT_CLIENT_SECRET": "reddit_secret",
    "REDDIT_ADS_ACCOUNT_ID": "acct_123",
    "REDDIT_AD_GROUP_ID": "adg_456",
}


def test_config_loads_all_required_env_vars():
    with patch.dict(os.environ, REQUIRED_ENV, clear=False):
        cfg = Config()
    assert cfg.twitch_client_id == "twitch_id"
    assert cfg.twitch_client_secret == "twitch_secret"
    assert cfg.twitch_channel_login == "some_channel"
    assert cfg.reddit_client_id == "reddit_id"
    assert cfg.reddit_client_secret == "reddit_secret"
    assert cfg.reddit_ads_account_id == "acct_123"
    assert cfg.reddit_ad_group_id == "adg_456"


def test_config_default_poll_interval():
    env = {**REQUIRED_ENV}
    env.pop("POLL_INTERVAL", None)
    with patch.dict(os.environ, env, clear=False):
        cfg = Config()
    assert cfg.poll_interval == 60


def test_config_custom_poll_interval():
    env = {**REQUIRED_ENV, "POLL_INTERVAL": "30"}
    with patch.dict(os.environ, env, clear=False):
        cfg = Config()
    assert cfg.poll_interval == 30


def test_config_default_trigger_keyword():
    with patch.dict(os.environ, REQUIRED_ENV, clear=False):
        cfg = Config()
    assert cfg.trigger_keyword == "Spark"


def test_config_custom_trigger_keyword():
    env = {**REQUIRED_ENV, "TRIGGER_KEYWORD": "Flink"}
    with patch.dict(os.environ, env, clear=False):
        cfg = Config()
    assert cfg.trigger_keyword == "Flink"


@pytest.mark.parametrize(
    "missing_key",
    [
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "TWITCH_CHANNEL_LOGIN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_ADS_ACCOUNT_ID",
        "REDDIT_AD_GROUP_ID",
    ],
)
def test_config_raises_when_required_var_missing(missing_key):
    env = {k: v for k, v in REQUIRED_ENV.items() if k != missing_key}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match=missing_key):
            Config()
