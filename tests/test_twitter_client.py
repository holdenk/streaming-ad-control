"""Tests for stream_ad_monitor.twitter_client."""

import json

import pytest
import requests
import responses as resp_lib

from stream_ad_monitor.text_limits import weighted_length
from stream_ad_monitor.twitter_client import TwitterClient

_TWEETS_URL = "https://api.twitter.com/2/tweets"

_CREDS = ("api_key", "api_secret", "access_token", "access_secret")


def _client():
    """A client with a plain session so `responses` can intercept it."""
    return TwitterClient(*_CREDS, session=requests.Session())


def _tweet_response(tweet_id="1750000000000000000"):
    return {"data": {"id": tweet_id, "text": "posted"}}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "index, missing_var",
    [
        (0, "TWITTER_API_KEY"),
        (1, "TWITTER_API_SECRET"),
        (2, "TWITTER_ACCESS_TOKEN"),
        (3, "TWITTER_ACCESS_TOKEN_SECRET"),
    ],
)
def test_missing_credentials_are_named(index, missing_var):
    creds = list(_CREDS)
    creds[index] = ""
    with pytest.raises(ValueError, match=missing_var):
        TwitterClient(*creds, session=requests.Session())


def test_builds_an_oauth1_session_when_none_is_injected():
    client = TwitterClient(*_CREDS)
    # OAuth1Session is a requests.Session subclass carrying the signing auth.
    assert isinstance(client._session, requests.Session)
    assert client._session.auth is not None


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_post_sends_text_and_returns_ref():
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response("123"), status=201)

    ref = _client().post("🔴 Live now: Spark\n\nhttps://twitch.tv/holden")

    body = json.loads(resp_lib.calls[0].request.body)
    assert body == {"text": "🔴 Live now: Spark\n\nhttps://twitch.tv/holden"}
    assert ref == {"id": "123", "url": "https://x.com/i/web/status/123"}


@resp_lib.activate
def test_post_threads_under_reply_to():
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response("456"), status=201)

    _client().post("Also on YouTube: https://youtu.be/x", reply_to={"id": "123"})

    body = json.loads(resp_lib.calls[0].request.body)
    assert body["reply"] == {"in_reply_to_tweet_id": "123"}


@resp_lib.activate
def test_post_ignores_reply_ref_without_an_id():
    """A ref from a failed post must not produce a malformed reply block."""
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response(), status=201)

    _client().post("hello", reply_to={"id": ""})

    assert "reply" not in json.loads(resp_lib.calls[0].request.body)


@resp_lib.activate
def test_post_truncates_over_the_character_limit():
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response(), status=201)

    _client().post("x" * 400)

    text = json.loads(resp_lib.calls[0].request.body)["text"]
    assert weighted_length(text) == 280
    assert text.endswith("…")


@resp_lib.activate
def test_wide_characters_are_weighted_like_x_weights_them():
    """A 200-character CJK title is already 400 by X's count, not 200."""
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response(), status=201)

    _client().post("観" * 200)

    text = json.loads(resp_lib.calls[0].request.body)["text"]
    assert len(text) < 200  # a plain len() check would have let this through
    assert weighted_length(text) <= 280


@resp_lib.activate
def test_urls_are_billed_at_their_t_co_length():
    """A long URL costs 23, so a post full of them still fits."""
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response(), status=201)
    links = "\n".join(f"https://example.com/{'a' * 80}/{n}" for n in range(6))

    _client().post(links)

    assert json.loads(resp_lib.calls[0].request.body)["text"] == links


@resp_lib.activate
def test_truncation_does_not_cut_into_a_url():
    """Half a link is worse than no link."""
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json=_tweet_response(), status=201)
    url = "https://twitch.tv/holden"

    _client().post("観" * 200 + " " + url)

    text = json.loads(resp_lib.calls[0].request.body)["text"]
    # A partial URL must not survive: either the whole link is there or none is.
    assert text.endswith(url) or "https://" not in text


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_success_without_a_tweet_id_is_an_error():
    """An empty id would silently break threading and log a bogus permalink."""
    resp_lib.add(resp_lib.POST, _TWEETS_URL, json={"data": {}}, status=201)

    with pytest.raises(RuntimeError, match="no tweet id"):
        _client().post("hello")


@resp_lib.activate
def test_success_with_a_non_json_body_is_an_error():
    resp_lib.add(resp_lib.POST, _TWEETS_URL, body="not json", status=201)

    with pytest.raises(RuntimeError, match="no tweet id"):
        _client().post("hello")


@resp_lib.activate
def test_post_raises_on_api_error():
    """A duplicate post or a read-only token both surface as a 403."""
    resp_lib.add(
        resp_lib.POST,
        _TWEETS_URL,
        json={"detail": "You are not allowed to create a Tweet with duplicate content."},
        status=403,
    )
    with pytest.raises(requests.HTTPError):
        _client().post("hello")
