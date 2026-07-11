"""Tests for stream_ad_monitor.monitor."""

import os
from unittest.mock import MagicMock, call, patch

import pytest

from stream_ad_monitor.monitor import StreamAdMonitor, title_has_keyword
from stream_ad_monitor.config import Config
from stream_ad_monitor.rules import Rule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "streamer",
    "REDDIT_USERNAME": "ads_user",
    "REDDIT_PASSWORD": "ads_password",
    "REDDIT_ADS_ACCOUNT_ID": "acct123",
    "REDDIT_CAMPAIGN_ID": "camp_456",
}


def _make_config(rules=None):
    """Return a Config with mocked credentials and the given list of Rules.

    If *rules* is None, a default single-rule config (legacy env vars) is used.
    """
    with patch.dict(os.environ, REQUIRED_ENV, clear=True):
        cfg = Config()
    if rules is not None:
        cfg.rules = rules
    return cfg


def _make_monitor(stream_sequence, rules=None):
    """Return a StreamAdMonitor with mocked clients.

    *stream_sequence* is a list of values returned by successive calls to
    ``twitch.get_stream``.  Each element is either a dict (live) or None
    (offline).  *rules* overrides the default single rule.
    """
    cfg = _make_config(rules)

    twitch = MagicMock()
    twitch.get_stream.side_effect = stream_sequence

    reddit = MagicMock()
    reddit.enable_campaign.return_value = {"status": "ACTIVE"}
    reddit.disable_campaign.return_value = {"status": "PAUSED"}

    monitor = StreamAdMonitor(cfg, twitch_client=twitch, reddit_ad_client=reddit)
    return monitor, twitch, reddit


def _live(title="Spark session today!"):
    return {"id": "1", "user_login": "streamer", "title": title, "type": "live"}


# ---------------------------------------------------------------------------
# title_has_keyword
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, keyword, expected",
    [
        ("Learning Apache Spark today!", "Spark", True),
        ("apache spark introduction", "Spark", True),  # case-insensitive
        ("SPARK everything", "Spark", True),
        ("Python and Pandas", "Spark", False),
        ("", "Spark", False),
        ("SparkSQL deep dive", "Spark", True),
    ],
)
def test_title_has_keyword(title, keyword, expected):
    assert title_has_keyword(title, keyword) == expected


# ---------------------------------------------------------------------------
# StreamAdMonitor.check – single-rule state transitions
# ---------------------------------------------------------------------------


def test_check_enables_ad_when_stream_goes_live_with_keyword():
    monitor, twitch, reddit = _make_monitor([_live("Apache Spark tutorial")])
    monitor.check()
    reddit.enable_campaign.assert_called_once_with("camp_456")
    reddit.disable_campaign.assert_not_called()
    assert monitor._rule_enabled[0] is True


def test_check_does_not_enable_ad_when_title_lacks_keyword():
    monitor, twitch, reddit = _make_monitor([_live("Just chatting today")])
    monitor.check()
    reddit.enable_campaign.assert_not_called()
    reddit.disable_campaign.assert_not_called()
    assert monitor._rule_enabled[0] is False


def test_check_does_not_enable_ad_when_offline():
    monitor, twitch, reddit = _make_monitor([None])
    monitor.check()
    reddit.enable_campaign.assert_not_called()
    reddit.disable_campaign.assert_not_called()
    assert monitor._rule_enabled[0] is False


def test_check_disables_ad_when_stream_ends():
    monitor, twitch, reddit = _make_monitor([_live(), None])
    monitor.check()  # goes live → enable
    monitor.check()  # goes offline → disable
    reddit.enable_campaign.assert_called_once_with("camp_456")
    reddit.disable_campaign.assert_called_once_with("camp_456")
    assert monitor._rule_enabled[0] is False


def test_check_disables_ad_when_title_no_longer_matches():
    monitor, twitch, reddit = _make_monitor([_live(), _live("Just chatting")])
    monitor.check()  # Spark title → enable
    monitor.check()  # non-Spark title → disable
    reddit.enable_campaign.assert_called_once()
    reddit.disable_campaign.assert_called_once()
    assert monitor._rule_enabled[0] is False


def test_check_no_redundant_enable_calls_while_live():
    """Consecutive live+matching polls should not re-enable the ad."""
    monitor, twitch, reddit = _make_monitor([_live(), _live(), _live()])
    for _ in range(3):
        monitor.check()
    reddit.enable_campaign.assert_called_once()  # only on first transition
    reddit.disable_campaign.assert_not_called()


def test_check_no_redundant_disable_calls_while_offline():
    """Consecutive offline polls should not re-disable the ad."""
    monitor, twitch, reddit = _make_monitor([None, None, None])
    for _ in range(3):
        monitor.check()
    reddit.disable_campaign.assert_not_called()


def test_check_re_enables_ad_when_stream_comes_back_live_with_keyword():
    monitor, twitch, reddit = _make_monitor([_live(), None, _live()])
    monitor.check()  # enable
    monitor.check()  # disable
    monitor.check()  # re-enable
    assert reddit.enable_campaign.call_count == 2
    assert reddit.disable_campaign.call_count == 1


def test_check_error_is_tolerated_by_run():
    """run() should catch exceptions from check() and continue."""
    cfg = _make_config()

    twitch = MagicMock()
    twitch.get_stream.side_effect = [RuntimeError("network error"), _live("Spark")]

    reddit = MagicMock()
    reddit.enable_campaign.return_value = {"status": "ACTIVE"}
    reddit.disable_campaign.return_value = {"status": "PAUSED"}

    monitor = StreamAdMonitor(cfg, twitch_client=twitch, reddit_ad_client=reddit)

    with patch("time.sleep"):
        monitor.run(stop_after=2)

    # Second poll succeeded and enabled the ad
    reddit.enable_campaign.assert_called_once_with("camp_456")


# ---------------------------------------------------------------------------
# StreamAdMonitor.check – multi-rule scenarios
# ---------------------------------------------------------------------------


def test_multi_rule_only_matching_rule_fires():
    """Only the rule whose keyword matches should enable its ad groups."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HomeAssistant", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    monitor, twitch, reddit = _make_monitor([_live("Apache Spark tutorial")], rules=rules)
    monitor.check()

    reddit.enable_campaign.assert_called_once_with("camp_spark")
    reddit.disable_campaign.assert_not_called()
    assert monitor._rule_enabled[0] is True
    assert monitor._rule_enabled[1] is False


def test_multi_rule_both_rules_fire_when_both_keywords_present():
    """Both rules should activate when the title matches both keywords."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HomeAssistant", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    title = "Spark and home assistant project"
    monitor, twitch, reddit = _make_monitor([_live(title)], rules=rules)
    monitor.check()

    assert reddit.enable_campaign.call_count == 2
    reddit.enable_campaign.assert_any_call("camp_spark")
    reddit.enable_campaign.assert_any_call("camp_ha")
    assert monitor._rule_enabled[0] is True
    assert monitor._rule_enabled[1] is True


def test_multi_rule_each_rule_disabled_independently():
    """When the title switches, only the rule that stopped matching disables."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HomeAssistant", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    combined_title = "Spark and home assistant project"
    spark_only_title = "Spark deep dive"
    monitor, twitch, reddit = _make_monitor(
        [_live(combined_title), _live(spark_only_title)], rules=rules
    )

    monitor.check()  # both fire
    reddit.reset_mock()

    monitor.check()  # HA no longer matches → disable HA; Spark still on
    reddit.disable_campaign.assert_called_once_with("camp_ha")
    reddit.enable_campaign.assert_not_called()
    assert monitor._rule_enabled[0] is True
    assert monitor._rule_enabled[1] is False


def test_multi_rule_multiple_ad_groups_per_rule():
    """A rule with multiple ad groups should enable/disable all of them."""
    rules = [
        Rule(
            name="HomeAssistant",
            keywords=["home assistant"],
            campaign_ids=["camp_ha", "camp_rpi"],
        ),
    ]
    monitor, twitch, reddit = _make_monitor(
        [_live("home assistant stream"), None], rules=rules
    )

    monitor.check()  # enable both ad groups
    assert reddit.enable_campaign.call_count == 2
    reddit.enable_campaign.assert_any_call("camp_ha")
    reddit.enable_campaign.assert_any_call("camp_rpi")

    reddit.reset_mock()
    monitor.check()  # stream ends → disable both ad groups
    assert reddit.disable_campaign.call_count == 2
    reddit.disable_campaign.assert_any_call("camp_ha")
    reddit.disable_campaign.assert_any_call("camp_rpi")


def test_multi_rule_keyword_matching_is_case_insensitive():
    """Keyword matching should be case-insensitive for YAML-defined rules."""
    rules = [
        Rule(name="HA", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    monitor, twitch, reddit = _make_monitor([_live("Home Assistant Stream")], rules=rules)
    monitor.check()
    reddit.enable_campaign.assert_called_once_with("camp_ha")


# ---------------------------------------------------------------------------
# StreamAdMonitor.run – loop mechanics
# ---------------------------------------------------------------------------


def test_run_sleeps_between_polls():
    monitor, twitch, reddit = _make_monitor([None, None, None])
    with patch("time.sleep") as mock_sleep:
        monitor.run(stop_after=3)
    # Should sleep twice between 3 polls (not after the last one)
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args == call(monitor.config.poll_interval)


# ---------------------------------------------------------------------------
# StreamAdMonitor.disable_all – startup housekeeping
# ---------------------------------------------------------------------------


def test_disable_all_calls_disable_for_every_ad_group():
    """disable_all() should call disable_campaign for every ad group across all rules."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HomeAssistant", keywords=["home assistant"], campaign_ids=["camp_ha", "camp_rpi"]),
    ]
    monitor, twitch, reddit = _make_monitor([], rules=rules)
    monitor.disable_all()

    assert reddit.disable_campaign.call_count == 3
    reddit.disable_campaign.assert_any_call("camp_spark")
    reddit.disable_campaign.assert_any_call("camp_ha")
    reddit.disable_campaign.assert_any_call("camp_rpi")
    reddit.enable_campaign.assert_not_called()


def test_disable_all_resets_rule_enabled_flags():
    """disable_all() should reset _rule_enabled to all-False even if flags were True."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HA", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    monitor, twitch, reddit = _make_monitor([], rules=rules)
    monitor._rule_enabled = [True, True]

    monitor.disable_all()

    assert monitor._rule_enabled == [False, False]


def test_disable_all_tolerates_api_errors():
    """disable_all() should log and continue even if one disable call fails."""
    rules = [
        Rule(name="Spark", keywords=["Spark"], campaign_ids=["camp_spark"]),
        Rule(name="HA", keywords=["home assistant"], campaign_ids=["camp_ha"]),
    ]
    monitor, twitch, reddit = _make_monitor([], rules=rules)
    reddit.disable_campaign.side_effect = [RuntimeError("API error"), None]

    monitor.disable_all()  # must not raise

    assert reddit.disable_campaign.call_count == 2
    assert monitor._rule_enabled == [False, False]


def test_run_calls_disable_all_before_first_poll():
    """run() must call disable_all() before any poll cycle."""
    monitor, twitch, reddit = _make_monitor([None])
    call_order = []
    reddit.disable_campaign.side_effect = lambda *a, **kw: call_order.append("disable")
    twitch.get_stream.side_effect = lambda *a, **kw: call_order.append("poll") or None

    with patch("time.sleep"):
        monitor.run(stop_after=1)

    assert call_order[0] == "disable", "disable_all must be called before the first poll"


# ---------------------------------------------------------------------------
# TrafficStars wiring
# ---------------------------------------------------------------------------


def _make_dual_network_monitor(stream_sequence, rules):
    cfg = _make_config(rules)
    cfg.trafficstars_api_key = "ts_key"

    twitch = MagicMock()
    twitch.get_stream.side_effect = stream_sequence
    reddit = MagicMock()
    trafficstars = MagicMock()

    monitor = StreamAdMonitor(
        cfg,
        twitch_client=twitch,
        reddit_ad_client=reddit,
        trafficstars_client=trafficstars,
    )
    return monitor, twitch, reddit, trafficstars


def test_check_enables_campaigns_on_both_networks():
    rules = [
        Rule(
            name="Spark",
            keywords=["Spark"],
            campaign_ids=["reddit_camp"],
            trafficstars_campaign_ids=["123"],
        ),
    ]
    monitor, twitch, reddit, trafficstars = _make_dual_network_monitor(
        [_live("Spark stream")], rules
    )
    monitor.check()

    reddit.enable_campaign.assert_called_once_with("reddit_camp")
    trafficstars.enable_campaign.assert_called_once_with("123")
    assert monitor._rule_enabled[0] is True


def test_check_disables_campaigns_on_both_networks_when_stream_ends():
    rules = [
        Rule(
            name="Spark",
            keywords=["Spark"],
            campaign_ids=["reddit_camp"],
            trafficstars_campaign_ids=["123"],
        ),
    ]
    monitor, twitch, reddit, trafficstars = _make_dual_network_monitor(
        [_live("Spark stream"), None], rules
    )
    monitor.check()
    monitor.check()

    reddit.disable_campaign.assert_called_once_with("reddit_camp")
    trafficstars.disable_campaign.assert_called_once_with("123")
    assert monitor._rule_enabled[0] is False


def test_trafficstars_only_rule_never_touches_reddit():
    rules = [
        Rule(
            name="TS only",
            keywords=["Spark"],
            trafficstars_campaign_ids=["123", "456"],
        ),
    ]
    monitor, twitch, reddit, trafficstars = _make_dual_network_monitor(
        [_live("Spark stream"), None], rules
    )
    monitor.check()
    monitor.check()

    reddit.enable_campaign.assert_not_called()
    reddit.disable_campaign.assert_not_called()
    assert trafficstars.enable_campaign.call_count == 2
    assert trafficstars.disable_campaign.call_count == 2


def test_disable_all_covers_both_networks():
    rules = [
        Rule(
            name="Spark",
            keywords=["Spark"],
            campaign_ids=["reddit_camp"],
            trafficstars_campaign_ids=["123"],
        ),
    ]
    monitor, twitch, reddit, trafficstars = _make_dual_network_monitor([], rules)
    monitor.disable_all()

    reddit.disable_campaign.assert_called_once_with("reddit_camp")
    trafficstars.disable_campaign.assert_called_once_with("123")


def test_disable_all_tolerates_trafficstars_errors():
    rules = [
        Rule(
            name="Spark",
            keywords=["Spark"],
            campaign_ids=["reddit_camp"],
            trafficstars_campaign_ids=["123"],
        ),
    ]
    monitor, twitch, reddit, trafficstars = _make_dual_network_monitor([], rules)
    trafficstars.disable_campaign.side_effect = RuntimeError("API error")

    monitor.disable_all()  # must not raise

    reddit.disable_campaign.assert_called_once_with("reddit_camp")
    assert monitor._rule_enabled == [False]


def test_no_reddit_client_constructed_for_trafficstars_only_config():
    """A TrafficStars-only setup must not require Reddit creds or Chromium."""
    rules = [
        Rule(name="TS", keywords=["Spark"], trafficstars_campaign_ids=["123"]),
    ]
    cfg = _make_config(rules)
    cfg.reddit_username = ""
    cfg.reddit_password = ""
    cfg.trafficstars_api_key = "ts_key"

    monitor = StreamAdMonitor(cfg, twitch_client=MagicMock())

    assert monitor.reddit is None
    assert monitor.trafficstars is not None


def test_no_trafficstars_client_constructed_for_reddit_only_config():
    rules = [
        Rule(name="Reddit", keywords=["Spark"], campaign_ids=["reddit_camp"]),
    ]
    cfg = _make_config(rules)

    monitor = StreamAdMonitor(
        cfg, twitch_client=MagicMock(), reddit_ad_client=MagicMock()
    )

    assert monitor.trafficstars is None
