"""Tests for the ExoClick, PropellerAds, and Meta ad clients."""

import json
from urllib.parse import parse_qs

import pytest
import requests
import responses as resp_lib

from stream_ad_monitor.exoclick_client import ExoClickClient
from stream_ad_monitor.meta_ads_client import MetaAdsClient
from stream_ad_monitor.networks import NETWORKS
from stream_ad_monitor.propellerads_client import PropellerAdsClient

_EXO_LOGIN = "https://api.exoclick.com/v2/login"
_EXO_PLAY = "https://api.exoclick.com/v2/campaigns/play"
_EXO_PAUSE = "https://api.exoclick.com/v2/campaigns/pause"
_PROP_PLAY = "https://ssp-api.propellerads.com/v5/adv/campaigns/play"
_PROP_STOP = "https://ssp-api.propellerads.com/v5/adv/campaigns/stop"
_META_URL = "https://graph.facebook.com/v25.0/120200000000001"


@pytest.mark.parametrize("cls", [ExoClickClient, PropellerAdsClient, MetaAdsClient])
def test_clients_require_a_token(cls):
    with pytest.raises(ValueError, match="token"):
        cls("")


# ---------------------------------------------------------------------------
# ExoClick
# ---------------------------------------------------------------------------


def _exo_login(token="exo_bearer", expires_in=3600):
    resp_lib.add(
        resp_lib.POST,
        _EXO_LOGIN,
        json={
            "token": token,
            "type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": "r",
            "refresh_token_expires_in": 1,
        },
    )


@resp_lib.activate
def test_exoclick_logs_in_with_api_token_then_plays():
    _exo_login()
    resp_lib.add(resp_lib.PUT, _EXO_PLAY, json={"message": "ok", "campaigns": {}})

    ExoClickClient("perm_token").enable_campaign("42")

    assert json.loads(resp_lib.calls[0].request.body) == {"api_token": "perm_token"}
    put = resp_lib.calls[1].request
    assert json.loads(put.body) == {"campaign_ids": [42]}
    assert put.headers["Authorization"] == "Bearer exo_bearer"


@resp_lib.activate
def test_exoclick_pause_reuses_cached_token():
    _exo_login()
    resp_lib.add(resp_lib.PUT, _EXO_PAUSE, json={"message": "ok", "campaigns": {}})
    resp_lib.add(resp_lib.PUT, _EXO_PAUSE, json={"message": "ok", "campaigns": {}})

    client = ExoClickClient("perm_token")
    client.disable_campaign(1)
    client.disable_campaign(2)

    assert [c.request.url for c in resp_lib.calls].count(_EXO_LOGIN) == 1


@resp_lib.activate
def test_exoclick_relogs_in_once_on_401():
    _exo_login("first")
    resp_lib.add(resp_lib.PUT, _EXO_PLAY, status=401)
    _exo_login("second")
    resp_lib.add(resp_lib.PUT, _EXO_PLAY, json={"message": "ok", "campaigns": {}})

    ExoClickClient("perm_token").enable_campaign(7)

    assert resp_lib.calls[-1].request.headers["Authorization"] == "Bearer second"


@resp_lib.activate
def test_exoclick_errors_in_body_raise():
    _exo_login()
    resp_lib.add(
        resp_lib.PUT,
        _EXO_PLAY,
        json={"message": "partial", "errors": ["Campaign 7 has no budget"], "campaigns": {}},
    )
    with pytest.raises(RuntimeError, match="no budget"):
        ExoClickClient("perm_token").enable_campaign(7)


@resp_lib.activate
def test_exoclick_two_factor_login_is_rejected():
    resp_lib.add(resp_lib.POST, _EXO_LOGIN, json={"token": "", "type": "2FA", "expires_in": 0})
    with pytest.raises(RuntimeError, match="two-factor"):
        ExoClickClient("perm_token").enable_campaign(7)


def test_exoclick_rejects_non_numeric_ids():
    with pytest.raises(ValueError, match="numeric"):
        ExoClickClient("perm_token").enable_campaign("abc")


# ---------------------------------------------------------------------------
# PropellerAds
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_propellerads_play_sends_static_bearer():
    resp_lib.add(resp_lib.PUT, _PROP_PLAY, json={"message": "ok"})

    PropellerAdsClient("prop_token").enable_campaign("555")

    request = resp_lib.calls[0].request
    assert request.headers["Authorization"] == "Bearer prop_token"
    assert json.loads(request.body) == {"campaign_ids": [555]}


@resp_lib.activate
def test_propellerads_disable_uses_stop():
    resp_lib.add(resp_lib.PUT, _PROP_STOP, json={"message": "ok"})
    PropellerAdsClient("prop_token").disable_campaign(555)
    assert resp_lib.calls[0].request.url == _PROP_STOP


@resp_lib.activate
def test_propellerads_per_campaign_errors_raise():
    resp_lib.add(
        resp_lib.PUT, _PROP_PLAY, json={"message": "", "errors": {"555": "not approved"}}
    )
    with pytest.raises(RuntimeError, match="not approved"):
        PropellerAdsClient("prop_token").enable_campaign(555)


@resp_lib.activate
def test_propellerads_http_error_raises():
    resp_lib.add(resp_lib.PUT, _PROP_PLAY, status=401, json={"message": "bad token"})
    with pytest.raises(requests.HTTPError):
        PropellerAdsClient("prop_token").enable_campaign(555)


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_meta_enable_posts_active_status_with_token_in_body():
    resp_lib.add(resp_lib.POST, _META_URL, json={"success": True})

    MetaAdsClient("meta_token").enable_campaign("120200000000001")

    request = resp_lib.calls[0].request
    assert "access_token" not in request.url
    assert parse_qs(request.body) == {"status": ["ACTIVE"], "access_token": ["meta_token"]}


@resp_lib.activate
def test_meta_disable_posts_paused():
    resp_lib.add(resp_lib.POST, _META_URL, json={"success": True})
    MetaAdsClient("meta_token").disable_campaign(120200000000001)
    assert parse_qs(resp_lib.calls[0].request.body)["status"] == ["PAUSED"]


@resp_lib.activate
def test_meta_graph_error_raises_with_message():
    resp_lib.add(
        resp_lib.POST,
        _META_URL,
        status=400,
        json={"error": {"message": "(#200) Requires ads_management permission", "code": 200}},
    )
    with pytest.raises(RuntimeError, match="ads_management"):
        MetaAdsClient("meta_token").enable_campaign("120200000000001")


@resp_lib.activate
def test_meta_unexpected_body_raises():
    resp_lib.add(resp_lib.POST, _META_URL, json={"success": False})
    with pytest.raises(RuntimeError, match="unexpected"):
        MetaAdsClient("meta_token").enable_campaign("120200000000001")


def test_meta_api_version_is_configurable():
    assert MetaAdsClient("t", api_version="v26.0").api_version == "v26.0"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_every_rest_network_has_a_factory_and_credential():
    for network in NETWORKS:
        if network.key == "reddit":
            assert network.factory is None
            continue
        assert network.factory is not None
        assert network.credential_env and network.config_attr
        assert network.factory("token") is not None
