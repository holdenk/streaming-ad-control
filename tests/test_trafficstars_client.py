"""Tests for stream_ad_monitor.trafficstars_client."""

import json

import pytest
import responses as resp_lib

from stream_ad_monitor.trafficstars_client import TrafficStarsClient

_TOKEN_URL = "https://api.trafficstars.com/v1/auth/token"
_RUN_URL = "https://api.trafficstars.com/v2/campaigns/run"
_PAUSE_URL = "https://api.trafficstars.com/v2/campaigns/pause"
_FAKE_TOKEN = "fake_ts_access_token"


def _add_token_response(token=_FAKE_TOKEN, expires_in=36000):
    resp_lib.add(
        resp_lib.POST,
        _TOKEN_URL,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        status=200,
    )


def _add_toggle_response(url, success, failed=(), status=200):
    resp_lib.add(
        resp_lib.PUT,
        url,
        json={
            "success": list(success),
            "failed": list(failed),
            "total": len(success) + len(failed),
        },
        status=status,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_api_key():
    with pytest.raises(ValueError, match="API key"):
        TrafficStarsClient("")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_authenticate_exchanges_api_key_for_token():
    _add_token_response()
    client = TrafficStarsClient("my_api_key")
    client.authenticate()

    assert client._access_token == _FAKE_TOKEN
    request = resp_lib.calls[0].request
    assert "grant_type=refresh_token" in request.body
    assert "refresh_token=my_api_key" in request.body


@resp_lib.activate
def test_authenticate_raises_on_failure():
    resp_lib.add(
        resp_lib.POST,
        _TOKEN_URL,
        json={"error": "invalid_grant"},
        status=400,
    )
    client = TrafficStarsClient("bad_key")
    with pytest.raises(Exception, match="400"):
        client.authenticate()


@resp_lib.activate
def test_token_is_cached_between_calls():
    """Only one token request should be issued for consecutive toggles."""
    _add_token_response()
    _add_toggle_response(_RUN_URL, success=[123])
    _add_toggle_response(_PAUSE_URL, success=[123])

    client = TrafficStarsClient("my_api_key")
    client.enable_campaign(123)
    client.disable_campaign(123)

    token_calls = [c for c in resp_lib.calls if c.request.url == _TOKEN_URL]
    assert len(token_calls) == 1


@resp_lib.activate
def test_expired_token_is_renewed():
    """expires_in=0 means the cached token is immediately stale."""
    _add_token_response(expires_in=0)
    _add_toggle_response(_RUN_URL, success=[123])
    _add_token_response(token="renewed_token")
    _add_toggle_response(_PAUSE_URL, success=[123])

    client = TrafficStarsClient("my_api_key")
    client.enable_campaign(123)
    client.disable_campaign(123)

    token_calls = [c for c in resp_lib.calls if c.request.url == _TOKEN_URL]
    assert len(token_calls) == 2
    assert client._access_token == "renewed_token"


# ---------------------------------------------------------------------------
# Campaign toggles
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_enable_campaign_puts_run_endpoint():
    _add_token_response()
    _add_toggle_response(_RUN_URL, success=[123])

    client = TrafficStarsClient("my_api_key")
    result = client.enable_campaign("123")

    assert result["success"] == [123]
    put = next(c.request for c in resp_lib.calls if c.request.url == _RUN_URL)
    assert json.loads(put.body) == {"campaign_ids": [123]}
    assert put.headers["Authorization"] == f"Bearer {_FAKE_TOKEN}"


@resp_lib.activate
def test_disable_campaign_puts_pause_endpoint():
    _add_token_response()
    _add_toggle_response(_PAUSE_URL, success=[456])

    client = TrafficStarsClient("my_api_key")
    result = client.disable_campaign(456)

    assert result["success"] == [456]
    put = next(c.request for c in resp_lib.calls if c.request.url == _PAUSE_URL)
    assert json.loads(put.body) == {"campaign_ids": [456]}


@resp_lib.activate
def test_campaign_in_failed_list_raises():
    """The API returns 200 with the campaign in 'failed' when it can't toggle."""
    _add_token_response()
    _add_toggle_response(_RUN_URL, success=[], failed=[789])

    client = TrafficStarsClient("my_api_key")
    with pytest.raises(RuntimeError, match="789"):
        client.enable_campaign(789)


@resp_lib.activate
def test_http_error_raises():
    _add_token_response()
    resp_lib.add(resp_lib.PUT, _PAUSE_URL, json={"error": "boom"}, status=500)

    client = TrafficStarsClient("my_api_key")
    with pytest.raises(Exception, match="500"):
        client.disable_campaign(123)


@resp_lib.activate
def test_401_triggers_reauth_and_retry():
    """A 401 on a toggle (token revoked early) re-authenticates and retries once."""
    _add_token_response(token="stale_token")
    resp_lib.add(resp_lib.PUT, _RUN_URL, json={"error": "unauthorized"}, status=401)
    _add_token_response(token="fresh_token")
    _add_toggle_response(_RUN_URL, success=[123])

    client = TrafficStarsClient("my_api_key")
    result = client.enable_campaign(123)

    assert result["success"] == [123]
    puts = [c.request for c in resp_lib.calls if c.request.url == _RUN_URL]
    assert len(puts) == 2
    assert puts[1].headers["Authorization"] == "Bearer fresh_token"


def test_non_numeric_campaign_id_raises():
    client = TrafficStarsClient("my_api_key")
    with pytest.raises(ValueError, match="numeric"):
        client.enable_campaign("camp_spark")


def test_close_is_idempotent():
    client = TrafficStarsClient("my_api_key")
    client.close()
    client.close()  # must not raise
