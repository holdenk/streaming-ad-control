"""Tests for stream_ad_monitor.config."""

import os
import textwrap
import pytest
from unittest.mock import patch

from stream_ad_monitor.config import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Base env vars that are always required (Twitch + Reddit API credentials)
BASE_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "some_channel",
    "REDDIT_CLIENT_ID": "reddit_id",
    "REDDIT_CLIENT_SECRET": "reddit_secret",
    "REDDIT_ADS_ACCOUNT_ID": "acct_123",
}

# Legacy single-rule env vars
LEGACY_ENV = {**BASE_ENV, "REDDIT_AD_GROUP_ID": "adg_456"}


# ---------------------------------------------------------------------------
# Legacy single-rule mode (backward compatibility)
# ---------------------------------------------------------------------------


def test_legacy_config_loads_all_required_env_vars():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert cfg.twitch_client_id == "twitch_id"
    assert cfg.twitch_client_secret == "twitch_secret"
    assert cfg.twitch_channel_login == "some_channel"
    assert cfg.reddit_client_id == "reddit_id"
    assert cfg.reddit_client_secret == "reddit_secret"
    assert cfg.reddit_ads_account_id == "acct_123"


def test_legacy_config_creates_single_rule():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert len(cfg.rules) == 1
    rule = cfg.rules[0]
    assert rule.ad_group_ids == ["adg_456"]
    assert rule.keywords == ["Spark"]  # default


def test_legacy_config_custom_trigger_keyword():
    env = {**LEGACY_ENV, "TRIGGER_KEYWORD": "Flink"}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.rules[0].keywords == ["Flink"]


def test_config_default_poll_interval():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert cfg.poll_interval == 60


def test_config_custom_poll_interval():
    env = {**LEGACY_ENV, "POLL_INTERVAL": "30"}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.poll_interval == 30


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
    env = {k: v for k, v in LEGACY_ENV.items() if k != missing_key}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match=missing_key):
            Config()


# ---------------------------------------------------------------------------
# YAML rules file mode
# ---------------------------------------------------------------------------


def test_yaml_config_loads_multiple_rules(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Spark"
                keywords:
                  - Spark
                ad_group_ids:
                  - adg_spark
              - name: "Home Assistant"
                keywords:
                  - home assistant
                  - homeassistant
                ad_group_ids:
                  - adg_ha
                  - adg_rpi
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()

    assert len(cfg.rules) == 2

    spark_rule = cfg.rules[0]
    assert spark_rule.name == "Spark"
    assert spark_rule.keywords == ["Spark"]
    assert spark_rule.ad_group_ids == ["adg_spark"]

    ha_rule = cfg.rules[1]
    assert ha_rule.name == "Home Assistant"
    assert "home assistant" in ha_rule.keywords
    assert "homeassistant" in ha_rule.keywords
    assert "adg_ha" in ha_rule.ad_group_ids
    assert "adg_rpi" in ha_rule.ad_group_ids


def test_yaml_config_raises_when_rules_file_is_empty(tmp_path):
    rules_yaml = tmp_path / "empty.yaml"
    rules_yaml.write_text("rules: []\n")
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="No rules found"):
            Config()


def test_yaml_config_raises_when_rule_has_no_keywords(tmp_path):
    rules_yaml = tmp_path / "bad.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad rule"
                keywords: []
                ad_group_ids:
                  - adg_1
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="no keywords"):
            Config()


def test_yaml_config_raises_when_rule_has_no_ad_groups(tmp_path):
    rules_yaml = tmp_path / "bad.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad rule"
                keywords:
                  - Spark
                ad_group_ids: []
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="no ad_group_ids"):
            Config()
