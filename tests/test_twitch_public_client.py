"""Tests for stream_ad_monitor.twitch_public_client (credential-free title read)."""

from unittest.mock import MagicMock

import requests

from stream_ad_monitor.twitch_public_client import TwitchPublicTitleClient


def _resp(ok=True, status=200, payload=None):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.text = "" if payload is None else str(payload)
    r.json.return_value = payload
    return r


def _client_with(resp=None, exc=None):
    session = MagicMock()
    if exc is not None:
        session.post.side_effect = exc
    else:
        session.post.return_value = resp
    return TwitchPublicTitleClient(session=session), session


def test_get_stream_returns_title_when_live():
    payload = {
        "data": {
            "user": {
                "stream": {"id": "42", "type": "live"},
                "broadcastSettings": {"title": "Apache Spark deep dive"},
            }
        }
    }
    client, session = _client_with(_resp(payload=payload))
    stream = client.get_stream("streamer")

    assert stream is not None
    assert stream["title"] == "Apache Spark deep dive"
    assert stream["type"] == "live"
    # It uses the public web client-id and never sends credentials.
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Client-Id"] == "kimne78kx3ncx6brgo4mv6wki5h1ko"
    assert kwargs["json"]["variables"] == {"login": "streamer"}


def test_get_stream_returns_none_when_offline():
    payload = {"data": {"user": {"stream": None, "broadcastSettings": {"title": "old"}}}}
    client, _ = _client_with(_resp(payload=payload))
    assert client.get_stream("streamer") is None


def test_get_stream_returns_none_for_unknown_channel():
    payload = {"data": {"user": None}}
    client, _ = _client_with(_resp(payload=payload))
    assert client.get_stream("nope") is None


def test_get_stream_returns_none_on_http_error():
    client, _ = _client_with(_resp(ok=False, status=503, payload=None))
    assert client.get_stream("streamer") is None


def test_get_stream_returns_none_on_request_exception():
    client, _ = _client_with(exc=requests.RequestException("boom"))
    assert client.get_stream("streamer") is None


def test_get_stream_returns_none_on_non_json_body():
    resp = _resp(payload=None)
    resp.json.side_effect = ValueError("not json")
    client, _ = _client_with(resp)
    assert client.get_stream("streamer") is None


def test_get_stream_returns_none_for_empty_login():
    client, session = _client_with(_resp(payload={"data": {"user": None}}))
    assert client.get_stream("") is None
    session.post.assert_not_called()  # no request for an empty login


def test_get_stream_title_live_and_offline():
    live_payload = {
        "data": {
            "user": {
                "stream": {"id": "1", "type": "live"},
                "broadcastSettings": {"title": "Spark"},
            }
        }
    }
    client, _ = _client_with(_resp(payload=live_payload))
    assert client.get_stream_title("streamer") == "Spark"

    client_off, _ = _client_with(_resp(payload={"data": {"user": {"stream": None}}}))
    assert client_off.get_stream_title("streamer") is None
