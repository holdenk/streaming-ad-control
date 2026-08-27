"""Tests for stream_ad_monitor.announcer."""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stream_ad_monitor import announcer as announcer_module
from stream_ad_monitor.announcer import (
    AnnounceSettings,
    StreamAnnouncer,
    build_announcer,
)
from stream_ad_monitor.youtube_client import LiveVideo

TWITCH_URL = "https://twitch.tv/holden"
YOUTUBE_URL = "https://www.youtube.com/watch?v=abc12345678"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeTarget:
    """Stands in for the X / Bluesky clients."""

    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.posts = []  # (text, reply_to)

    def post(self, text, reply_to=None):
        self.posts.append((text, reply_to))
        if self.fail:
            raise RuntimeError(f"{self.name} is down")
        return {"id": f"{self.name}-{len(self.posts)}"}

    @property
    def texts(self):
        return [text for text, _ in self.posts]


class FakeYouTube:
    """Returns queued lookup results; the last one repeats."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def find_live_video(self):
        self.calls += 1
        result = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(
        announcer_module, "time", SimpleNamespace(monotonic=fake.monotonic)
    )
    return fake


def _settings(**overrides):
    defaults = dict(youtube_lookup_interval=60, youtube_lookup_timeout=1800)
    defaults.update(overrides)
    return AnnounceSettings(**defaults)


def _make(youtube_results=None, targets=None, **settings_overrides):
    targets = targets or {"twitter": FakeTarget("twitter")}
    youtube = FakeYouTube(youtube_results) if youtube_results is not None else None
    announcer = StreamAnnouncer(
        "holden", _settings(**settings_overrides), targets, youtube
    )
    return announcer, targets, youtube


def _stream(stream_id="1", title="Spark stream"):
    return {"id": stream_id, "user_login": "holden", "title": title, "type": "live"}


# ---------------------------------------------------------------------------
# Announcing when the stream goes live
# ---------------------------------------------------------------------------


def test_announces_the_twitch_link_when_the_stream_goes_live(clock):
    announcer, targets, _ = _make()

    announcer.handle_stream(_stream(title="Apache Spark deep dive"))

    assert targets["twitter"].texts == [
        "🔴 Live now: Apache Spark deep dive\n\n" + TWITCH_URL
    ]


def test_announces_to_every_configured_platform(clock):
    targets = {"twitter": FakeTarget("twitter"), "bluesky": FakeTarget("bluesky")}
    announcer, targets, _ = _make(targets=targets)

    announcer.handle_stream(_stream())

    assert len(targets["twitter"].posts) == 1
    assert len(targets["bluesky"].posts) == 1


def test_does_not_re_announce_on_later_polls(clock):
    announcer, targets, _ = _make()

    for _ in range(5):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert len(targets["twitter"].posts) == 1


def test_offline_poll_does_not_trigger_a_re_announcement(clock):
    """The Twitch API drops the occasional poll; that must not double-post."""
    announcer, targets, _ = _make()

    announcer.handle_stream(_stream())
    announcer.handle_stream(None)  # spurious offline reading
    announcer.handle_stream(_stream())  # same stream id, still live

    assert len(targets["twitter"].posts) == 1


def test_a_new_broadcast_is_announced_again(clock):
    announcer, targets, _ = _make()

    announcer.handle_stream(_stream(stream_id="1", title="First"))
    announcer.handle_stream(None)
    announcer.handle_stream(_stream(stream_id="2", title="Second"))

    assert len(targets["twitter"].posts) == 2
    assert "Second" in targets["twitter"].texts[1]


def test_offline_only_never_announces(clock):
    announcer, targets, _ = _make()
    announcer.handle_stream(None)
    assert targets["twitter"].posts == []


def test_stream_without_an_id_is_skipped(clock):
    """Without an id there is no way to tell a repeat poll from a new stream."""
    announcer, targets, _ = _make()
    announcer.handle_stream({"title": "no id", "type": "live"})
    assert targets["twitter"].posts == []


# ---------------------------------------------------------------------------
# Keyword gating
# ---------------------------------------------------------------------------


def test_non_matching_title_is_not_announced(clock):
    announcer, targets, _ = _make(keywords=["spark"])
    announcer.handle_stream(_stream(title="Just chatting"))
    assert targets["twitter"].posts == []


def test_keyword_matching_is_case_insensitive(clock):
    announcer, targets, _ = _make(keywords=["home assistant"])
    announcer.handle_stream(_stream(title="Home Assistant tinkering"))
    assert len(targets["twitter"].posts) == 1


def test_title_edited_into_matching_mid_stream_is_announced(clock):
    """Streamers routinely fix the title a few minutes in."""
    announcer, targets, _ = _make(keywords=["spark"])

    announcer.handle_stream(_stream(title="starting soon"))
    assert targets["twitter"].posts == []

    clock.advance(60)
    announcer.handle_stream(_stream(title="Spark internals"))
    assert len(targets["twitter"].posts) == 1


def test_no_keywords_announces_every_stream(clock):
    announcer, targets, _ = _make()
    announcer.handle_stream(_stream(title="anything at all"))
    assert len(targets["twitter"].posts) == 1


# ---------------------------------------------------------------------------
# YouTube follow-up
# ---------------------------------------------------------------------------


def test_youtube_link_is_posted_as_a_reply_once_it_appears(clock):
    announcer, targets, youtube = _make(youtube_results=[None, LiveVideo("abc12345678")])

    announcer.handle_stream(_stream())  # announce with the Twitch link only
    clock.advance(60)
    announcer.handle_stream(_stream())  # YouTube link has shown up

    text, reply_to = targets["twitter"].posts[1]
    assert text == f"Also streaming on YouTube: {YOUTUBE_URL}"
    assert reply_to == {"id": "twitter-1"}


def test_youtube_link_is_posted_only_once(clock):
    announcer, targets, _ = _make(youtube_results=[None, LiveVideo("abc12345678")])

    for _ in range(5):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert len(targets["twitter"].posts) == 2


def test_youtube_lookup_is_rate_limited_independently_of_the_poll(clock):
    """Twitch is polled every 60s; YouTube must not be hit that often."""
    announcer, _, youtube = _make(youtube_results=[None], youtube_lookup_interval=300)

    for _ in range(6):  # polls at t=0,60,…,300
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert youtube.calls == 2  # only t=0 and t=300


def test_youtube_lookup_stops_after_the_timeout(clock):
    announcer, targets, youtube = _make(
        youtube_results=[None], youtube_lookup_interval=60, youtube_lookup_timeout=300
    )

    for _ in range(10):
        announcer.handle_stream(_stream())
        clock.advance(60)
    calls_at_timeout = youtube.calls

    for _ in range(5):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert youtube.calls == calls_at_timeout
    assert len(targets["twitter"].posts) == 1  # the Twitch announcement only


def test_youtube_errors_are_tolerated_then_abandoned(clock):
    announcer, targets, youtube = _make(youtube_results=[RuntimeError("502 from YouTube")])

    for _ in range(6):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert youtube.calls == 3  # _MAX_YOUTUBE_FAILURES
    assert len(targets["twitter"].posts) == 1


def test_youtube_error_then_success_still_posts_the_link(clock):
    announcer, targets, _ = _make(
        youtube_results=[RuntimeError("blip"), LiveVideo("abc12345678")]
    )

    announcer.handle_stream(_stream())
    clock.advance(60)
    announcer.handle_stream(_stream())

    assert YOUTUBE_URL in targets["twitter"].texts[1]


def test_no_youtube_configured_means_no_follow_up(clock):
    announcer, targets, _ = _make(youtube_results=None)

    for _ in range(3):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert len(targets["twitter"].posts) == 1


def test_youtube_link_already_live_is_included_in_the_first_post(clock):
    announcer, targets, _ = _make(youtube_results=[LiveVideo("abc12345678")])

    announcer.handle_stream(_stream(title="Spark"))
    clock.advance(60)
    announcer.handle_stream(_stream(title="Spark"))

    assert targets["twitter"].texts == [
        f"🔴 Live now: Spark\n\n{TWITCH_URL}\n{YOUTUBE_URL}"
    ]


def test_wait_for_youtube_holds_the_announcement_then_combines_the_links(clock):
    announcer, targets, _ = _make(
        youtube_results=[None, LiveVideo("abc12345678")],
        wait_for_youtube_sec=300,
    )

    announcer.handle_stream(_stream(title="Spark"))
    assert targets["twitter"].posts == []  # held, waiting for YouTube

    clock.advance(60)
    announcer.handle_stream(_stream(title="Spark"))

    assert targets["twitter"].texts == [
        f"🔴 Live now: Spark\n\n{TWITCH_URL}\n{YOUTUBE_URL}"
    ]


def test_wait_for_youtube_gives_up_and_posts_the_twitch_link(clock):
    announcer, targets, _ = _make(youtube_results=[None], wait_for_youtube_sec=120)

    announcer.handle_stream(_stream(title="Spark"))
    assert targets["twitter"].posts == []

    clock.advance(180)
    announcer.handle_stream(_stream(title="Spark"))

    assert targets["twitter"].texts == [f"🔴 Live now: Spark\n\n{TWITCH_URL}"]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_one_failing_platform_does_not_block_the_other(clock):
    targets = {
        "twitter": FakeTarget("twitter", fail=True),
        "bluesky": FakeTarget("bluesky"),
    }
    announcer, targets, _ = _make(targets=targets)

    announcer.handle_stream(_stream())

    assert len(targets["bluesky"].posts) == 1


def test_follow_up_only_goes_to_platforms_that_got_the_announcement(clock):
    targets = {
        "twitter": FakeTarget("twitter", fail=True),
        "bluesky": FakeTarget("bluesky"),
    }
    announcer, targets, _ = _make(
        targets=targets, youtube_results=[None, LiveVideo("abc12345678")]
    )

    announcer.handle_stream(_stream())
    clock.advance(60)
    announcer.handle_stream(_stream())

    # X never got the announcement, so a reply there would be an orphan post.
    assert len(targets["twitter"].posts) == 1
    assert len(targets["bluesky"].posts) == 2


def test_announcement_is_retried_when_every_platform_fails(clock):
    announcer, targets, _ = _make(targets={"twitter": FakeTarget("twitter", fail=True)})

    announcer.handle_stream(_stream())
    clock.advance(60)
    announcer.handle_stream(_stream())

    assert len(targets["twitter"].posts) == 2


def test_announcement_retries_are_bounded(clock):
    announcer, targets, _ = _make(targets={"twitter": FakeTarget("twitter", fail=True)})

    for _ in range(10):
        announcer.handle_stream(_stream())
        clock.advance(60)

    assert len(targets["twitter"].posts) == 5  # _MAX_ANNOUNCE_ATTEMPTS


def test_follow_up_retries_are_bounded(clock):
    target = FakeTarget("twitter")
    announcer, targets, _ = _make(
        targets={"twitter": target}, youtube_results=[None, LiveVideo("abc12345678")]
    )

    announcer.handle_stream(_stream())  # announcement succeeds
    target.fail = True
    for _ in range(10):
        clock.advance(60)
        announcer.handle_stream(_stream())

    # 1 announcement + _MAX_FOLLOWUP_ATTEMPTS failed replies
    assert len(target.posts) == 4


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_custom_template_placeholders(clock):
    announcer, targets, _ = _make(
        template="{channel} is live: {title} — {twitch_url}",
    )
    announcer.handle_stream(_stream(title="Spark"))
    assert targets["twitter"].texts == [f"holden is live: Spark — {TWITCH_URL}"]


def test_custom_youtube_template(clock):
    announcer, targets, _ = _make(
        youtube_results=[None, LiveVideo("abc12345678")],
        youtube_template="YT mirror of {title}: {youtube_url}",
    )
    announcer.handle_stream(_stream(title="Spark"))
    clock.advance(60)
    announcer.handle_stream(_stream(title="Spark"))
    assert targets["twitter"].texts[1] == f"YT mirror of Spark: {YOUTUBE_URL}"


def test_invalid_template_falls_back_to_the_default(clock):
    announcer, targets, _ = _make(template="Live: {nonexistent_placeholder}")
    announcer.handle_stream(_stream(title="Spark"))
    assert targets["twitter"].texts == [f"🔴 Live now: Spark\n\n{TWITCH_URL}"]


def test_long_titles_are_truncated_so_the_links_survive(clock):
    announcer, targets, _ = _make(title_max_chars=20)
    announcer.handle_stream(_stream(title="x" * 100))
    text = targets["twitter"].texts[0]
    assert "x" * 19 + "…" in text
    assert TWITCH_URL in text


def test_title_truncation_can_be_disabled(clock):
    announcer, targets, _ = _make(title_max_chars=0)
    announcer.handle_stream(_stream(title="y" * 100))
    assert "y" * 100 in targets["twitter"].texts[0]


# ---------------------------------------------------------------------------
# Restart-safe state
# ---------------------------------------------------------------------------


def test_state_is_persisted_after_announcing(clock, tmp_path):
    path = tmp_path / "announce-state.json"
    announcer, _, _ = _make(state_path=str(path))

    announcer.handle_stream(_stream(stream_id="42"))

    saved = json.loads(path.read_text())
    assert saved["stream_id"] == "42"
    assert saved["announced"] is True


def test_restart_mid_stream_does_not_re_announce(clock, tmp_path):
    path = str(tmp_path / "announce-state.json")

    first, first_targets, _ = _make(state_path=path)
    first.handle_stream(_stream(stream_id="42"))

    second, second_targets, _ = _make(state_path=path)  # daemon restarts
    second.handle_stream(_stream(stream_id="42"))

    assert len(first_targets["twitter"].posts) == 1
    assert second_targets["twitter"].posts == []


def test_restart_still_announces_a_different_stream(clock, tmp_path):
    path = str(tmp_path / "announce-state.json")

    first, _, _ = _make(state_path=path)
    first.handle_stream(_stream(stream_id="42"))

    second, targets, _ = _make(state_path=path)
    second.handle_stream(_stream(stream_id="43"))

    assert len(targets["twitter"].posts) == 1


def test_restart_resumes_a_pending_youtube_follow_up(clock, tmp_path):
    path = str(tmp_path / "announce-state.json")

    first, _, _ = _make(state_path=path, youtube_results=[None])
    first.handle_stream(_stream(stream_id="42"))

    second, targets, _ = _make(
        state_path=path, youtube_results=[LiveVideo("abc12345678")]
    )
    second.handle_stream(_stream(stream_id="42"))

    text, reply_to = targets["twitter"].posts[0]
    assert YOUTUBE_URL in text
    assert reply_to == {"id": "twitter-1"}  # the ref from before the restart


def test_corrupt_state_file_is_ignored(clock, tmp_path):
    path = tmp_path / "announce-state.json"
    path.write_text("{not json")

    announcer, targets, _ = _make(state_path=str(path))
    announcer.handle_stream(_stream())

    assert len(targets["twitter"].posts) == 1


def test_missing_state_file_is_fine(clock, tmp_path):
    announcer, targets, _ = _make(state_path=str(tmp_path / "nope" / "state.json"))
    announcer.handle_stream(_stream())
    assert len(targets["twitter"].posts) == 1


def test_unwritable_state_path_does_not_break_announcing(clock, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    announcer, targets, _ = _make(state_path=str(blocker / "state.json"))
    announcer.handle_stream(_stream())

    assert len(targets["twitter"].posts) == 1


def test_no_state_path_means_no_persistence(clock, tmp_path):
    announcer, targets, _ = _make(state_path="")
    announcer.handle_stream(_stream())
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# build_announcer
# ---------------------------------------------------------------------------


def test_build_announcer_returns_none_without_credentials():
    assert build_announcer("holden", AnnounceSettings()) is None


def test_build_announcer_returns_none_when_disabled():
    settings = AnnounceSettings(
        enabled=False, bluesky_handle="h", bluesky_app_password="pw"
    )
    assert build_announcer("holden", settings) is None


def test_build_announcer_builds_bluesky_only():
    settings = AnnounceSettings(bluesky_handle="h.bsky.social", bluesky_app_password="pw")
    announcer = build_announcer("holden", settings)

    assert sorted(announcer.targets) == ["bluesky"]
    assert announcer.youtube is None
    assert announcer.twitch_url == TWITCH_URL


def test_build_announcer_builds_both_platforms_and_youtube():
    settings = AnnounceSettings(
        twitter_api_key="k",
        twitter_api_secret="s",
        twitter_access_token="t",
        twitter_access_token_secret="ts",
        bluesky_handle="h.bsky.social",
        bluesky_app_password="pw",
        youtube_channel_handle="@holden",
    )
    announcer = build_announcer("holden", settings)

    assert sorted(announcer.targets) == ["bluesky", "twitter"]
    assert announcer.youtube.live_url == "https://www.youtube.com/@holden/live"


def test_build_announcer_skips_a_platform_that_fails_to_construct():
    """A broken X client must not take Bluesky announcements down with it."""
    settings = AnnounceSettings(
        twitter_api_key="k",
        twitter_api_secret="s",
        twitter_access_token="t",
        twitter_access_token_secret="ts",
        bluesky_handle="h.bsky.social",
        bluesky_app_password="pw",
    )
    with patch(
        "stream_ad_monitor.announcer.TwitterClient",
        side_effect=RuntimeError("requests-oauthlib missing"),
    ):
        announcer = build_announcer("holden", settings)

    assert sorted(announcer.targets) == ["bluesky"]


def test_build_announcer_returns_none_when_no_client_survives():
    settings = AnnounceSettings(bluesky_handle="h", bluesky_app_password="pw")
    with patch(
        "stream_ad_monitor.announcer.BlueskyClient", side_effect=RuntimeError("boom")
    ):
        assert build_announcer("holden", settings) is None
