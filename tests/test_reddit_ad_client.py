"""Tests for stream_ad_monitor.reddit_ad_client."""

import pytest
import responses as resp_lib

from stream_ad_monitor.reddit_ad_client import RedditAdClient

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_ADS_BASE = "https://ads-api.reddit.com/api/v3"
_ACCOUNT_ID = "acct_123"
_AD_GROUP_ID = "adg_456"
_FAKE_TOKEN = "fake_reddit_token"


def _ad_group_url():
    return f"{_ADS_BASE}/accounts/{_ACCOUNT_ID}/ad_groups/{_AD_GROUP_ID}"


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
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    client.authenticate()
    assert client._access_token == _FAKE_TOKEN


@resp_lib.activate
def test_authenticate_raises_and_logs_on_failure():
    """Auth failure should log the response body before raising."""
    resp_lib.add(
        resp_lib.POST,
        _TOKEN_URL,
        json={"error": "invalid_client"},
        status=403,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    with pytest.raises(Exception, match="403"):
        client.authenticate()


@resp_lib.activate
def test_enable_ad_group_sends_active_status():
    _add_token_response()
    resp_lib.add(
        resp_lib.PATCH,
        _ad_group_url(),
        json={"id": _AD_GROUP_ID, "status": "ACTIVE"},
        status=200,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    result = client.enable_ad_group(_AD_GROUP_ID)
    assert result["status"] == "ACTIVE"

    # Verify the request body
    assert len(resp_lib.calls) == 2  # token + patch
    patch_call = resp_lib.calls[1]
    import json
    body = json.loads(patch_call.request.body)
    assert body == {"status": "ACTIVE"}


@resp_lib.activate
def test_disable_ad_group_sends_paused_status():
    _add_token_response()
    resp_lib.add(
        resp_lib.PATCH,
        _ad_group_url(),
        json={"id": _AD_GROUP_ID, "status": "PAUSED"},
        status=200,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    result = client.disable_ad_group(_AD_GROUP_ID)
    assert result["status"] == "PAUSED"

    import json
    patch_call = resp_lib.calls[1]
    body = json.loads(patch_call.request.body)
    assert body == {"status": "PAUSED"}


@resp_lib.activate
def test_patch_ad_group_auto_authenticates():
    _add_token_response()
    resp_lib.add(
        resp_lib.PATCH,
        _ad_group_url(),
        json={"id": _AD_GROUP_ID, "status": "ACTIVE"},
        status=200,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    assert client._access_token is None
    client.enable_ad_group(_AD_GROUP_ID)
    assert client._access_token == _FAKE_TOKEN


@resp_lib.activate
def test_patch_ad_group_retries_on_401():
    """A 401 from the ads API should trigger re-auth and one retry."""
    _add_token_response()
    # First PATCH returns 401
    resp_lib.add(resp_lib.PATCH, _ad_group_url(), status=401)
    # Re-auth token
    _add_token_response()
    # Retry PATCH succeeds
    resp_lib.add(
        resp_lib.PATCH,
        _ad_group_url(),
        json={"id": _AD_GROUP_ID, "status": "ACTIVE"},
        status=200,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    result = client.enable_ad_group(_AD_GROUP_ID)
    assert result["status"] == "ACTIVE"
    # auth + patch(401) + re-auth + patch(200)
    assert len(resp_lib.calls) == 4


@resp_lib.activate
def test_patch_ad_group_retries_on_403():
    """A 403 from the ads API should trigger re-auth and one retry."""
    _add_token_response()
    # First PATCH returns 403
    resp_lib.add(resp_lib.PATCH, _ad_group_url(), status=403)
    # Re-auth token
    _add_token_response()
    # Retry PATCH succeeds
    resp_lib.add(
        resp_lib.PATCH,
        _ad_group_url(),
        json={"id": _AD_GROUP_ID, "status": "PAUSED"},
        status=200,
    )
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    result = client.disable_ad_group(_AD_GROUP_ID)
    assert result["status"] == "PAUSED"
    assert len(resp_lib.calls) == 4


@resp_lib.activate
def test_patch_ad_group_raises_on_http_error():
    _add_token_response()
    resp_lib.add(resp_lib.PATCH, _ad_group_url(), status=500)
    client = RedditAdClient("cid", "csecret", _ACCOUNT_ID)
    with pytest.raises(Exception):
        client.enable_ad_group(_AD_GROUP_ID)
