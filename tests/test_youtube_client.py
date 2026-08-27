"""Tests for stream_ad_monitor.youtube_client.

The page fixtures below are trimmed-down copies of what youtube.com actually
serves for ``/@handle/live`` — in particular the canonical link really is the
string "undefined" on a live page, which is why the client reads ytInitialData
instead of the usual meta tags.
"""

import pytest
import responses as resp_lib

from stream_ad_monitor.youtube_client import LiveVideo, YouTubeClient

_LIVE_URL = "https://www.youtube.com/@holden/live"
_WATCH_URL = "https://www.youtube.com/watch?v=abc12345678"

_SIDEBAR = (
    '{"compactVideoRenderer":{"videoId":"zzz99999999","title":'
    '{"simpleText":"someone else\'s stream"}}}'
)


def _live_page(video_id="abc12345678", title="Spark stream", sidebar=_SIDEBAR):
    """A channel that is broadcasting: /live renders the watch page."""
    return (
        '<html><head><link rel="canonical" href="undefined">'
        "</head><body><script>var ytInitialData = "
        '{"contents":{"twoColumnWatchNextResults":{"results":{"results":'
        '{"contents":[{"videoPrimaryInfoRenderer":{"title":{"runs":[{"text":'
        f'"{title}"}}]}},"navigationEndpoint":{{"watchEndpoint":{{"videoId":'
        f'"{video_id}"}}}},"viewCount":{{"videoViewCountRenderer":{{"viewCount":'
        '{"runs":[{"text":"1,234"},{"text":" watching now"}]},"isLive":true}}}}]}}'
        f",{sidebar}}}}}}};</script></body></html>"
    )


def _offline_page():
    """A channel that is not broadcasting: /live falls back to the channel page."""
    return (
        '<html><head><link rel="canonical" href="https://www.youtube.com/@holden">'
        "</head><body><script>var ytInitialData = "
        '{"contents":{"twoColumnBrowseResultsRenderer":{"tabs":[{"tabRenderer":'
        '{"content":{"richGridRenderer":{"contents":[{"videoRenderer":'
        '{"videoId":"old12345678","title":{"simpleText":"last week\'s upload"}}}'
        "]}}}}]}}};</script></body></html>"
    )


def _upcoming_page(video_id="sch12345678"):
    """A scheduled broadcast: a real watch page, but nobody is streaming yet."""
    return (
        "<html><body><script>var ytInitialData = "
        '{"contents":{"twoColumnWatchNextResults":{"results":{"results":'
        '{"contents":[{"videoPrimaryInfoRenderer":{"title":{"runs":[{"text":'
        f'"Starting Sunday"}}]}},"navigationEndpoint":{{"watchEndpoint":'
        f'{{"videoId":"{video_id}"}}}}}}]}}}}}}}},'
        '"playabilityStatus":{"isUpcoming":true}};</script></body></html>'
    )


def _vod_page(video_id="vod12345678"):
    """A finished stream: watch page, plain view count, no live markers."""
    return (
        "<html><body><script>var ytInitialData = "
        '{"contents":{"twoColumnWatchNextResults":{"results":{"results":'
        '{"contents":[{"videoPrimaryInfoRenderer":{"title":{"runs":[{"text":'
        f'"Yesterday\'s stream"}}]}},"navigationEndpoint":{{"watchEndpoint":'
        f'{{"videoId":"{video_id}"}}}},"viewCount":{{"videoViewCountRenderer":'
        '{"viewCount":{"simpleText":"4,201 views"}}}}}]}}}}};</script></body></html>'
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_a_handle_id_or_url():
    with pytest.raises(ValueError, match="channel handle"):
        YouTubeClient()


@pytest.mark.parametrize("configured", ["holden", "@holden", "  @holden  "])
def test_handle_is_normalised_into_the_live_url(configured):
    assert YouTubeClient(configured).live_url == _LIVE_URL


def test_channel_id_builds_the_channel_live_url():
    assert YouTubeClient(channel_id="UC123").live_url == (
        "https://www.youtube.com/channel/UC123/live"
    )


def test_explicit_live_url_wins():
    client = YouTubeClient("holden", live_url="https://www.youtube.com/c/x/live")
    assert client.live_url == "https://www.youtube.com/c/x/live"


def test_consent_cookies_are_preset():
    """Without these an EU-resolved request gets the consent wall, not the page."""
    client = YouTubeClient("holden")
    assert client._session.cookies.get("CONSENT") == "YES+cb"
    assert client._session.cookies.get("SOCS") == "CAI"


# ---------------------------------------------------------------------------
# Live lookup
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_find_live_video_reads_the_primary_video_from_the_page():
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_live_page(), status=200)

    video = YouTubeClient("holden").find_live_video()

    assert video == LiveVideo(video_id="abc12345678", title="Spark stream")
    assert video.url == _WATCH_URL


@resp_lib.activate
def test_sidebar_videos_are_not_mistaken_for_the_broadcast():
    """The watch page lists other channels' videos alongside this one."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_live_page(), status=200)
    video = YouTubeClient("holden").find_live_video()
    assert video.video_id == "abc12345678"


@resp_lib.activate
def test_find_live_video_returns_none_when_channel_is_offline():
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_offline_page(), status=200)
    assert YouTubeClient("holden").find_live_video() is None


@resp_lib.activate
def test_scheduled_stream_is_not_treated_as_live():
    """An upcoming broadcast's link would send viewers to a countdown."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_upcoming_page(), status=200)
    assert YouTubeClient("holden").find_live_video() is None


@resp_lib.activate
def test_finished_stream_is_not_treated_as_live():
    """`/live` serves the previous broadcast's VOD on some channels."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_vod_page(), status=200)
    assert YouTubeClient("holden").find_live_video() is None


@resp_lib.activate
def test_live_broadcast_details_alone_are_enough():
    """Not every live page carries a live view counter."""
    page = (
        '<html><body>{"contents":{"twoColumnWatchNextResults":{"contents":'
        '[{"videoPrimaryInfoRenderer":{}},{"videoId":"abc12345678"}]}},'
        '"liveBroadcastDetails":{"isLiveNow":true,"startTimestamp":"2026-08-27"}}'
        "</body></html>"
    )
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=page, status=200)

    video = YouTubeClient("holden").find_live_video()

    assert video is not None and video.video_id == "abc12345678"


@resp_lib.activate
def test_find_live_video_follows_a_redirect_to_the_watch_page():
    """Some responses redirect straight to the broadcast instead of inlining it."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, status=302, headers={"Location": _WATCH_URL})
    resp_lib.add(resp_lib.GET, _WATCH_URL, body=_live_page(), status=200)

    video = YouTubeClient("holden").find_live_video()

    assert video is not None and video.video_id == "abc12345678"


@resp_lib.activate
def test_undefined_canonical_does_not_break_extraction():
    """The live page's canonical link is the literal string "undefined"."""
    page = _live_page()
    assert 'href="undefined"' in page
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=page, status=200)

    assert YouTubeClient("holden").find_live_video().video_id == "abc12345678"


@resp_lib.activate
def test_a_real_canonical_link_is_preferred_when_present():
    page = (
        f'<link rel="canonical" href="{_WATCH_URL}">'
        '{"twoColumnWatchNextResults":{"videoId":"zzz99999999"},'
        '"videoViewCountRenderer":{"viewCount":{},"isLive":true}}'
    )
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=page, status=200)

    assert YouTubeClient("holden").find_live_video().video_id == "abc12345678"


@resp_lib.activate
def test_title_json_escapes_are_decoded():
    resp_lib.add(
        resp_lib.GET,
        _LIVE_URL,
        body=_live_page(title=r"Spark & Scala 📚"),
        status=200,
    )
    assert YouTubeClient("holden").find_live_video().title == "Spark & Scala 📚"


@resp_lib.activate
def test_missing_title_is_not_fatal():
    """The title is only used for logging; the link is what matters."""
    page = (
        '{"contents":{"twoColumnWatchNextResults":{"videoId":"abc12345678"}},'
        '"videoViewCountRenderer":{"viewCount":{},"isLive":true}}'
    )
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=page, status=200)

    video = YouTubeClient("holden").find_live_video()
    assert video.video_id == "abc12345678"
    assert video.title == ""


@resp_lib.activate
def test_find_live_video_sends_a_browser_user_agent():
    """YouTube serves a stripped body without ytInitialData to obvious bots."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, body=_offline_page(), status=200)
    YouTubeClient("holden").find_live_video()
    assert "Mozilla/5.0" in resp_lib.calls[0].request.headers["User-Agent"]


@resp_lib.activate
def test_find_live_video_raises_on_http_error():
    resp_lib.add(resp_lib.GET, _LIVE_URL, body="nope", status=500)
    with pytest.raises(Exception, match="500"):
        YouTubeClient("holden").find_live_video()


@resp_lib.activate
def test_unrecognised_page_shape_returns_none_rather_than_a_wrong_link():
    """If YouTube reshapes the page, a missing follow-up beats a bad link."""
    resp_lib.add(resp_lib.GET, _LIVE_URL, body="<html>nothing useful</html>", status=200)
    assert YouTubeClient("holden").find_live_video() is None
