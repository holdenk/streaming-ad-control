"""Tests for stream_ad_monitor.twitch_client."""

import pytest
import responses as resp_lib

from stream_ad_monitor.twitch_client import TwitchClient

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_STREAMS_URL = "https://api.twitch.tv/helix/streams"
_FAKE_TOKEN = "fake_access_token"


def _add_token_response():
    resp_lib.add(
        resp_lib.POST,
        _TOKEN_URL,
        json={"access_token": _FAKE_TOKEN, "token_type": "bearer"},
        status=200,
    )


@resp_lib.activate
def test_authenticate_sets_access_token():
    _add_token_response()
    client = TwitchClient("cid", "csecret")
    client.authenticate()
    assert client._access_token == _FAKE_TOKEN


@resp_lib.activate
def test_authenticate_raises_and_logs_on_failure():
    """Auth failure should log the response body before raising."""
    resp_lib.add(
        resp_lib.POST,
        _TOKEN_URL,
        json={"message": "invalid client"},
        status=403,
    )
    client = TwitchClient("cid", "csecret")
    with pytest.raises(Exception, match="403"):
        client.authenticate()


@resp_lib.activate
def test_get_stream_returns_stream_when_live():
    _add_token_response()
    stream_data = {
        "id": "111",
        "user_login": "testchannel",
        "title": "Learning Apache Spark today!",
        "type": "live",
    }
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": [stream_data]},
        status=200,
    )

    client = TwitchClient("cid", "csecret")
    result = client.get_stream("testchannel")
    assert result == stream_data


@resp_lib.activate
def test_get_stream_returns_none_when_offline():
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": []},
        status=200,
    )

    client = TwitchClient("cid", "csecret")
    result = client.get_stream("testchannel")
    assert result is None


@resp_lib.activate
def test_is_live_true_when_streaming():
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": [{"id": "1", "user_login": "ch", "title": "t", "type": "live"}]},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    assert client.is_live("ch") is True


@resp_lib.activate
def test_is_live_false_when_offline():
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": []},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    assert client.is_live("ch") is False


@resp_lib.activate
def test_get_stream_title_returns_title_when_live():
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": [{"id": "1", "user_login": "ch", "title": "Spark stream", "type": "live"}]},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    assert client.get_stream_title("ch") == "Spark stream"


@resp_lib.activate
def test_get_stream_title_returns_none_when_offline():
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": []},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    assert client.get_stream_title("ch") is None


@resp_lib.activate
def test_get_stream_auto_authenticates_if_no_token():
    """get_stream should call authenticate automatically when no token is cached."""
    _add_token_response()
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": []},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    assert client._access_token is None
    client.get_stream("ch")
    assert client._access_token == _FAKE_TOKEN


@resp_lib.activate
def test_get_stream_retries_on_401():
    """A 401 from the streams API should trigger re-auth and one retry."""
    _add_token_response()
    # First GET returns 401
    resp_lib.add(resp_lib.GET, _STREAMS_URL, status=401)
    # Re-auth token
    _add_token_response()
    # Retry GET succeeds
    resp_lib.add(
        resp_lib.GET,
        _STREAMS_URL,
        json={"data": [{"id": "1", "user_login": "ch", "title": "t", "type": "live"}]},
        status=200,
    )
    client = TwitchClient("cid", "csecret")
    result = client.get_stream("ch")
    assert result is not None
    # auth + get(401) + re-auth + get(200)
    assert len(resp_lib.calls) == 4


@resp_lib.activate
def test_get_stream_raises_on_non_401_http_error():
    _add_token_response()
    resp_lib.add(resp_lib.GET, _STREAMS_URL, status=500)
    client = TwitchClient("cid", "csecret")
    with pytest.raises(Exception):
        client.get_stream("ch")
