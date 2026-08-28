"""Tests for stream_ad_monitor.config."""

import os
import textwrap
import pytest
from unittest.mock import patch

from stream_ad_monitor import mask_credential
from stream_ad_monitor.config import Config, _sanitize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Base env vars that are always required (Twitch + Reddit Ads login)
BASE_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "some_channel",
    "REDDIT_USERNAME": "ads_user",
    "REDDIT_PASSWORD": "ads_password",
    "REDDIT_ADS_ACCOUNT_ID": "acct123",
}

# Legacy single-rule env vars
LEGACY_ENV = {**BASE_ENV, "REDDIT_CAMPAIGN_ID": "2470329120103230906"}


# ---------------------------------------------------------------------------
# _sanitize() tests
# ---------------------------------------------------------------------------


def test_sanitize_strips_double_quotes():
    assert _sanitize('"abc123"') == "abc123"


def test_sanitize_strips_single_quotes():
    assert _sanitize("'abc123'") == "abc123"


def test_sanitize_strips_whitespace_and_cr():
    assert _sanitize("  abc123\r\n") == "abc123"


def test_sanitize_strips_quotes_and_whitespace():
    assert _sanitize('  "abc123"  \r') == "abc123"


def test_sanitize_preserves_clean_values():
    assert _sanitize("abc123") == "abc123"


def test_sanitize_preserves_mismatched_quotes():
    assert _sanitize("'abc123\"") == "'abc123\""


# ---------------------------------------------------------------------------
# mask_credential() tests
# ---------------------------------------------------------------------------


def test_mask_short_value():
    assert mask_credential("ab") == "a***b"


def test_mask_single_char():
    assert mask_credential("a") == "***"


def test_mask_long_value():
    assert mask_credential("abcdefghij") == "abc***hij"


def test_mask_exactly_eight():
    assert mask_credential("12345678") == "1***8"


# ---------------------------------------------------------------------------
# Legacy single-rule mode (backward compatibility)
# ---------------------------------------------------------------------------


def test_legacy_config_loads_all_required_env_vars():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert cfg.twitch_client_id == "twitch_id"
    assert cfg.twitch_client_secret == "twitch_secret"
    assert cfg.twitch_channel_login == "some_channel"
    assert cfg.reddit_username == "ads_user"
    assert cfg.reddit_password == "ads_password"
    assert cfg.reddit_ads_account_id == "acct123"


def test_legacy_config_creates_single_rule():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert len(cfg.rules) == 1
    rule = cfg.rules[0]
    assert rule.campaign_ids == ["2470329120103230906"]
    assert rule.keywords == ["Spark"]  # default


def test_legacy_config_accepts_deprecated_reddit_ad_group_id_alias():
    """REDDIT_AD_GROUP_ID is still accepted for back-compat (treated as a campaign ID)."""
    env = {k: v for k, v in BASE_ENV.items()}
    env["REDDIT_AD_GROUP_ID"] = "old_alias_id"
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.rules[0].campaign_ids == ["old_alias_id"]


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
    "missing_key, expected_message_fragment",
    [
        ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_ID"),
        ("TWITCH_CLIENT_SECRET", "TWITCH_CLIENT_SECRET"),
        ("TWITCH_CHANNEL_LOGIN", "TWITCH_CHANNEL_LOGIN"),
        ("REDDIT_USERNAME", "REDDIT_USERNAME"),
        ("REDDIT_PASSWORD", "REDDIT_PASSWORD"),
        ("REDDIT_CAMPAIGN_ID", "REDDIT_CAMPAIGN_ID"),
    ],
)
def test_config_raises_when_required_var_missing(missing_key, expected_message_fragment):
    env = {k: v for k, v in LEGACY_ENV.items() if k != missing_key}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match=expected_message_fragment):
            Config()


def test_config_sanitizes_quoted_env_vars():
    """Values with surrounding quotes should be stripped automatically."""
    env = {k: f'"{v}"' for k, v in LEGACY_ENV.items()}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.twitch_client_id == "twitch_id"
    assert cfg.reddit_username == "ads_user"


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
                campaign_ids:
                  - camp_spark
              - name: "Home Assistant"
                keywords:
                  - home assistant
                  - homeassistant
                campaign_ids:
                  - camp_ha
                  - camp_rpi
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()

    assert len(cfg.rules) == 2

    spark_rule = cfg.rules[0]
    assert spark_rule.name == "Spark"
    assert spark_rule.keywords == ["Spark"]
    assert spark_rule.campaign_ids == ["camp_spark"]

    ha_rule = cfg.rules[1]
    assert ha_rule.name == "Home Assistant"
    assert "home assistant" in ha_rule.keywords
    assert "homeassistant" in ha_rule.keywords
    assert "camp_ha" in ha_rule.campaign_ids
    assert "camp_rpi" in ha_rule.campaign_ids


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
                campaign_ids:
                  - camp_1
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="no keywords"):
            Config()


def test_yaml_config_raises_when_rule_has_no_campaigns(tmp_path):
    rules_yaml = tmp_path / "bad.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad rule"
                keywords:
                  - Spark
                campaign_ids: []
        """)
    )
    env = {**BASE_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="no campaign_ids"):
            Config()


# ---------------------------------------------------------------------------
# TrafficStars settings
# ---------------------------------------------------------------------------

# Twitch-only env — used to prove per-network creds are only required when a
# rule actually targets that network.
TWITCH_ONLY_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "some_channel",
}


def _write_ts_only_rules(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "TS only"
                keywords:
                  - Spark
                trafficstars_campaign_ids:
                  - 123456
        """)
    )
    return rules_yaml


def test_trafficstars_only_rules_do_not_require_reddit_creds(tmp_path):
    rules_yaml = _write_ts_only_rules(tmp_path)
    env = {
        **TWITCH_ONLY_ENV,
        "RULES_FILE": str(rules_yaml),
        "TRAFFICSTARS_API_KEY": "ts_key",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.trafficstars_api_key == "ts_key"
    assert cfg.reddit_username == ""
    assert cfg.reddit_password == ""
    assert cfg.reddit_ads_account_id == ""


def test_trafficstars_rules_require_api_key(tmp_path):
    rules_yaml = _write_ts_only_rules(tmp_path)
    env = {**TWITCH_ONLY_ENV, "RULES_FILE": str(rules_yaml)}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="TRAFFICSTARS_API_KEY"):
            Config()


def test_reddit_only_rules_do_not_require_trafficstars_key():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert cfg.trafficstars_api_key == ""


def test_reddit_account_id_is_optional():
    """The ads account id is auto-discovered after login, so it's not required."""
    env = {k: v for k, v in LEGACY_ENV.items() if k != "REDDIT_ADS_ACCOUNT_ID"}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert cfg.reddit_ads_account_id == ""


def test_reddit_account_id_override_is_honored():
    with patch.dict(os.environ, LEGACY_ENV, clear=True):
        cfg = Config()
    assert cfg.reddit_ads_account_id == "acct123"


def test_legacy_trafficstars_campaign_id_creates_single_rule():
    env = {
        **TWITCH_ONLY_ENV,
        "TRAFFICSTARS_CAMPAIGN_ID": "123456",
        "TRAFFICSTARS_API_KEY": "ts_key",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert len(cfg.rules) == 1
    assert cfg.rules[0].campaign_ids == []
    assert cfg.rules[0].trafficstars_campaign_ids == ["123456"]


def test_legacy_both_campaign_ids_land_in_one_rule():
    env = {
        **LEGACY_ENV,
        "TRAFFICSTARS_CAMPAIGN_ID": "123456",
        "TRAFFICSTARS_API_KEY": "ts_key",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = Config()
    assert len(cfg.rules) == 1
    assert cfg.rules[0].campaign_ids == ["2470329120103230906"]
    assert cfg.rules[0].trafficstars_campaign_ids == ["123456"]


# ---------------------------------------------------------------------------
# Announcement settings
# ---------------------------------------------------------------------------

ANNOUNCE_ENV = {
    "TWITTER_API_KEY": "tw_key",
    "TWITTER_API_SECRET": "tw_secret",
    "TWITTER_ACCESS_TOKEN": "tw_token",
    "TWITTER_ACCESS_TOKEN_SECRET": "tw_token_secret",
    "BLUESKY_HANDLE": "holden.bsky.social",
    "BLUESKY_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
    "MASTODON_ACCESS_TOKEN": "masto_token",
    "YOUTUBE_CHANNEL_HANDLE": "@holden",
}


def _config_with(**extra_env):
    with patch.dict(os.environ, {**LEGACY_ENV, **extra_env}, clear=True):
        return Config()


def test_announcements_are_off_without_credentials():
    announce = _config_with().announce
    assert announce.any_target_configured is False
    assert announce.twitter_configured is False
    assert announce.bluesky_configured is False
    assert announce.mastodon_configured is False
    assert announce.youtube_configured is False


def test_announce_credentials_are_read_from_the_environment():
    announce = _config_with(**ANNOUNCE_ENV).announce
    assert announce.twitter_configured is True
    assert announce.bluesky_configured is True
    assert announce.mastodon_configured is True
    assert announce.youtube_configured is True
    assert announce.bluesky_handle == "holden.bsky.social"
    assert announce.youtube_channel_handle == "@holden"


def test_partial_twitter_credentials_do_not_enable_x():
    """Three of the four keys is a misconfiguration, not a working setup."""
    env = dict(ANNOUNCE_ENV)
    del env["TWITTER_ACCESS_TOKEN_SECRET"]
    assert _config_with(**env).announce.twitter_configured is False


def test_announce_can_be_disabled_explicitly():
    announce = _config_with(**ANNOUNCE_ENV, ANNOUNCE_ENABLED="false").announce
    assert announce.enabled is False


@pytest.mark.parametrize("raw, expected", [("true", True), ("0", False), ("YES", True)])
def test_announce_enabled_accepts_common_boolean_spellings(raw, expected):
    assert _config_with(ANNOUNCE_ENABLED=raw).announce.enabled is expected


def test_unparseable_boolean_falls_back_to_the_default():
    assert _config_with(ANNOUNCE_ENABLED="maybe").announce.enabled is True


def test_announce_keywords_are_split_on_commas():
    announce = _config_with(ANNOUNCE_KEYWORDS="Spark, home assistant ,,python").announce
    assert announce.keywords == ["Spark", "home assistant", "python"]


def test_announce_keywords_default_to_empty():
    assert _config_with().announce.keywords == []


def test_template_backslash_n_becomes_a_real_newline():
    """An env file has no way to spell a literal newline."""
    announce = _config_with(ANNOUNCE_TEMPLATE=r"live: {title}\n{links}").announce
    assert announce.template == "live: {title}\n{links}"


def test_empty_template_falls_back_to_the_default():
    from stream_ad_monitor.announcer import DEFAULT_TEMPLATE

    assert _config_with(ANNOUNCE_TEMPLATE="").announce.template == DEFAULT_TEMPLATE


def test_lookup_timers_are_read_as_integers():
    announce = _config_with(
        YOUTUBE_LOOKUP_INTERVAL="30",
        YOUTUBE_LOOKUP_TIMEOUT="600",
        ANNOUNCE_WAIT_FOR_YOUTUBE_SEC="90",
    ).announce
    assert (announce.youtube_lookup_interval, announce.youtube_lookup_timeout) == (30, 600)
    assert announce.wait_for_youtube_sec == 90


def test_unparseable_integer_falls_back_to_the_default():
    assert _config_with(YOUTUBE_LOOKUP_INTERVAL="soon").announce.youtube_lookup_interval == 60


def test_announce_state_file_is_optional():
    assert _config_with().announce.state_path == ""
    assert _config_with(ANNOUNCE_STATE_FILE="/var/lib/x/state.json").announce.state_path == (
        "/var/lib/x/state.json"
    )


def test_youtube_channel_id_alone_enables_lookup():
    assert _config_with(YOUTUBE_CHANNEL_ID="UC123").announce.youtube_configured is True


def test_mastodon_token_alone_enables_announcing():
    """The instance URL has a default, so the token is the only requirement."""
    announce = _config_with(MASTODON_ACCESS_TOKEN="tok").announce
    assert announce.mastodon_configured is True
    assert announce.any_target_configured is True
    assert announce.mastodon_instance_url == ""  # falls back to tech.lgbt


def test_mastodon_instance_and_visibility_are_read():
    announce = _config_with(
        MASTODON_ACCESS_TOKEN="tok",
        MASTODON_INSTANCE_URL="https://hachyderm.io",
        MASTODON_VISIBILITY="unlisted",
        MASTODON_MAX_CHARS="5000",
    ).announce
    assert announce.mastodon_instance_url == "https://hachyderm.io"
    assert announce.mastodon_visibility == "unlisted"
    assert announce.mastodon_max_chars == 5000
