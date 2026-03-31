"""Tests for stream_ad_monitor.monitor."""

import os
from unittest.mock import MagicMock, call, patch

import pytest

from stream_ad_monitor.monitor import StreamAdMonitor, title_has_keyword
from stream_ad_monitor.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_ENV = {
    "TWITCH_CLIENT_ID": "twitch_id",
    "TWITCH_CLIENT_SECRET": "twitch_secret",
    "TWITCH_CHANNEL_LOGIN": "streamer",
    "REDDIT_CLIENT_ID": "reddit_id",
    "REDDIT_CLIENT_SECRET": "reddit_secret",
    "REDDIT_ADS_ACCOUNT_ID": "acct_123",
    "REDDIT_AD_GROUP_ID": "adg_456",
}


def _make_monitor(stream_sequence):
    """Return a StreamAdMonitor with mocked clients.

    *stream_sequence* is a list of values returned by successive calls to
    ``twitch.get_stream``.  Each element is either a dict (live) or None
    (offline).
    """
    with patch.dict(os.environ, REQUIRED_ENV, clear=True):
        cfg = Config()

    twitch = MagicMock()
    twitch.get_stream.side_effect = stream_sequence

    reddit = MagicMock()
    reddit.enable_ad_group.return_value = {"status": "ACTIVE"}
    reddit.disable_ad_group.return_value = {"status": "PAUSED"}

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
# StreamAdMonitor.check – state transitions
# ---------------------------------------------------------------------------


def test_check_enables_ad_when_stream_goes_live_with_keyword():
    monitor, twitch, reddit = _make_monitor([_live("Apache Spark tutorial")])
    monitor.check()
    reddit.enable_ad_group.assert_called_once_with("adg_456")
    reddit.disable_ad_group.assert_not_called()
    assert monitor._ad_enabled is True


def test_check_does_not_enable_ad_when_title_lacks_keyword():
    monitor, twitch, reddit = _make_monitor([_live("Just chatting today")])
    monitor.check()
    reddit.enable_ad_group.assert_not_called()
    reddit.disable_ad_group.assert_not_called()
    assert monitor._ad_enabled is False


def test_check_does_not_enable_ad_when_offline():
    monitor, twitch, reddit = _make_monitor([None])
    monitor.check()
    reddit.enable_ad_group.assert_not_called()
    reddit.disable_ad_group.assert_not_called()
    assert monitor._ad_enabled is False


def test_check_disables_ad_when_stream_ends():
    monitor, twitch, reddit = _make_monitor([_live(), None])
    monitor.check()  # goes live → enable
    monitor.check()  # goes offline → disable
    reddit.enable_ad_group.assert_called_once_with("adg_456")
    reddit.disable_ad_group.assert_called_once_with("adg_456")
    assert monitor._ad_enabled is False


def test_check_disables_ad_when_title_no_longer_matches():
    monitor, twitch, reddit = _make_monitor([_live(), _live("Just chatting")])
    monitor.check()  # Spark title → enable
    monitor.check()  # non-Spark title → disable
    reddit.enable_ad_group.assert_called_once()
    reddit.disable_ad_group.assert_called_once()
    assert monitor._ad_enabled is False


def test_check_no_redundant_enable_calls_while_live():
    """Consecutive live+matching polls should not re-enable the ad."""
    monitor, twitch, reddit = _make_monitor([_live(), _live(), _live()])
    for _ in range(3):
        monitor.check()
    reddit.enable_ad_group.assert_called_once()  # only on first transition
    reddit.disable_ad_group.assert_not_called()


def test_check_no_redundant_disable_calls_while_offline():
    """Consecutive offline polls should not re-disable the ad."""
    monitor, twitch, reddit = _make_monitor([None, None, None])
    for _ in range(3):
        monitor.check()
    reddit.disable_ad_group.assert_not_called()


def test_check_re_enables_ad_when_stream_comes_back_live_with_keyword():
    monitor, twitch, reddit = _make_monitor([_live(), None, _live()])
    monitor.check()  # enable
    monitor.check()  # disable
    monitor.check()  # re-enable
    assert reddit.enable_ad_group.call_count == 2
    assert reddit.disable_ad_group.call_count == 1


def test_check_error_is_tolerated_by_run():
    """run() should catch exceptions from check() and continue."""
    with patch.dict(os.environ, REQUIRED_ENV, clear=True):
        cfg = Config()

    twitch = MagicMock()
    twitch.get_stream.side_effect = [RuntimeError("network error"), _live("Spark")]

    reddit = MagicMock()
    reddit.enable_ad_group.return_value = {"status": "ACTIVE"}
    reddit.disable_ad_group.return_value = {"status": "PAUSED"}

    monitor = StreamAdMonitor(cfg, twitch_client=twitch, reddit_ad_client=reddit)

    with patch("time.sleep"):
        monitor.run(stop_after=2)

    # Second poll succeeded and enabled the ad
    reddit.enable_ad_group.assert_called_once_with("adg_456")


# ---------------------------------------------------------------------------
# StreamAdMonitor.run – loop mechanics
# ---------------------------------------------------------------------------


def test_run_sleeps_between_polls():
    monitor, twitch, reddit = _make_monitor([None, None, None])
    with patch("time.sleep") as mock_sleep, patch.dict(os.environ, REQUIRED_ENV, clear=True):
        monitor.run(stop_after=3)
    # Should sleep twice between 3 polls (not after the last one)
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args == call(monitor.config.poll_interval)
