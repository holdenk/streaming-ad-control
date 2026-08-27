"""Tests for stream_ad_monitor.mastodon_client."""

import json

import pytest
import requests
import responses as resp_lib

from stream_ad_monitor.mastodon_client import DEFAULT_INSTANCE_URL, MastodonClient

_STATUSES_URL = "https://tech.lgbt/api/v1/statuses"


def _client(**kwargs):
    kwargs.setdefault("session", requests.Session())
    return MastodonClient("token123", **kwargs)


def _status(status_id="109999", url="https://tech.lgbt/@holden/109999"):
    return {"id": status_id, "url": url, "content": "<p>posted</p>"}


def _post_requests():
    """Only the status POSTs — an instance-limit lookup may precede them."""
    return [c.request for c in resp_lib.calls if c.request.method == "POST"]


def _last_body():
    return json.loads(_post_requests()[-1].body)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_an_access_token():
    with pytest.raises(ValueError, match="MASTODON_ACCESS_TOKEN"):
        MastodonClient("")


def test_instance_defaults_to_tech_lgbt():
    assert _client().statuses_url == _STATUSES_URL
    assert DEFAULT_INSTANCE_URL == "https://tech.lgbt"


def test_any_instance_can_be_configured():
    client = _client(instance_url="https://hachyderm.io/")
    assert client.statuses_url == "https://hachyderm.io/api/v1/statuses"


def test_empty_instance_url_falls_back_to_the_default():
    assert _client(instance_url="").statuses_url == _STATUSES_URL


@pytest.mark.parametrize("visibility", ["public", "unlisted", "private", "direct"])
def test_valid_visibilities_are_kept(visibility):
    assert _client(visibility=visibility).visibility == visibility


def test_unknown_visibility_falls_back_to_public():
    assert _client(visibility="secret").visibility == "public"


def test_blank_visibility_defaults_to_public():
    assert _client(visibility="").visibility == "public"


def test_max_chars_is_auto_unless_configured():
    assert _client().max_chars == 0  # 0 = ask the instance
    assert _client(max_chars=5000).max_chars == 5000
    assert _client(max_chars=0).max_chars == 0  # unset in the environment


def test_construction_makes_no_network_calls():
    """Detection is lazy so a blip at daemon startup can't break announcing."""
    with resp_lib.RequestsMock() as mock:
        _client()
        assert len(mock.calls) == 0


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_post_sends_the_status_and_returns_a_ref():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    ref = _client().post("🔴 Live now: Spark\n\nhttps://twitch.tv/holden")

    assert _last_body() == {
        "status": "🔴 Live now: Spark\n\nhttps://twitch.tv/holden",
        "visibility": "public",
    }
    assert ref == {"id": "109999", "url": "https://tech.lgbt/@holden/109999"}


@resp_lib.activate
def test_post_authenticates_with_the_access_token():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)
    _client(max_chars=500).post("hello")
    assert _post_requests()[0].headers["Authorization"] == "Bearer token123"


@resp_lib.activate
def test_post_threads_under_reply_to():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status("110000"), status=200)

    _client().post("Also on YouTube: https://youtu.be/x", reply_to={"id": "109999"})

    assert _last_body()["in_reply_to_id"] == "109999"


@resp_lib.activate
def test_post_ignores_a_reply_ref_without_an_id():
    """A ref from a failed post must not produce a malformed reply."""
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)
    _client().post("hello", reply_to={"id": ""})
    assert "in_reply_to_id" not in _last_body()


@resp_lib.activate
def test_configured_visibility_is_sent():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)
    _client(visibility="unlisted").post("hello")
    assert _last_body()["visibility"] == "unlisted"


@resp_lib.activate
def test_identical_posts_share_an_idempotency_key():
    """So a retry after a timed-out-but-delivered post doesn't double-post."""
    for _ in range(2):
        resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    client = _client(max_chars=500)
    client.post("same text")
    client.post("same text")

    keys = [r.headers["Idempotency-Key"] for r in _post_requests()]
    assert keys[0] == keys[1]


@resp_lib.activate
def test_different_posts_get_different_idempotency_keys():
    for _ in range(3):
        resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    client = _client(max_chars=500)
    client.post("announcement")
    client.post("follow-up")
    client.post("follow-up", reply_to={"id": "109999"})

    keys = [r.headers["Idempotency-Key"] for r in _post_requests()]
    assert len(set(keys)) == 3


@resp_lib.activate
def test_post_truncates_at_the_instance_limit():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client(max_chars=500).post("x" * 800)

    status = _last_body()["status"]
    assert len(status) == 500
    assert status.endswith("…")


@resp_lib.activate
def test_a_raised_instance_limit_is_respected():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)
    _client(max_chars=5000).post("x" * 800)
    assert len(_last_body()["status"]) == 800


@resp_lib.activate
def test_post_raises_on_a_bad_token():
    resp_lib.add(
        resp_lib.POST, _STATUSES_URL, json={"error": "The access token is invalid"}, status=401
    )
    with pytest.raises(requests.HTTPError):
        _client().post("hello")


@resp_lib.activate
def test_post_raises_when_the_instance_rejects_the_status():
    resp_lib.add(
        resp_lib.POST,
        _STATUSES_URL,
        json={"error": "Validation failed: Text character limit exceeded"},
        status=422,
    )
    with pytest.raises(requests.HTTPError):
        _client().post("hello")


# ---------------------------------------------------------------------------
# Instance post-length limit
# ---------------------------------------------------------------------------

_INSTANCE_V2_URL = "https://tech.lgbt/api/v2/instance"
_INSTANCE_V1_URL = "https://tech.lgbt/api/v1/instance"


def _add_instance(url=_INSTANCE_V2_URL, max_characters=1024, status=200):
    resp_lib.add(
        resp_lib.GET,
        url,
        json={"configuration": {"statuses": {"max_characters": max_characters}}},
        status=status,
    )


@resp_lib.activate
def test_limit_is_read_from_the_instance():
    """tech.lgbt runs glitch-soc at 1024, so 500 would truncate needlessly."""
    _add_instance(max_characters=1024)
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client().post("x" * 800)

    assert len(_last_body()["status"]) == 800


@resp_lib.activate
def test_detected_limit_still_truncates_beyond_it():
    _add_instance(max_characters=1024)
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client().post("x" * 2000)

    assert len(_last_body()["status"]) == 1024


@resp_lib.activate
def test_limit_is_detected_once_and_cached():
    _add_instance()
    for _ in range(3):
        resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    client = _client()
    for _ in range(3):
        client.post("hello")

    instance_calls = [c for c in resp_lib.calls if c.request.method == "GET"]
    assert len(instance_calls) == 1


@resp_lib.activate
def test_detection_falls_back_to_v1_on_older_instances():
    """The v2 endpoint only exists from Mastodon 4.0."""
    _add_instance(url=_INSTANCE_V2_URL, status=404)
    _add_instance(url=_INSTANCE_V1_URL, max_characters=750)
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client().post("x" * 900)

    assert len(_last_body()["status"]) == 750


@resp_lib.activate
def test_undetectable_limit_falls_back_to_500():
    resp_lib.add(resp_lib.GET, _INSTANCE_V2_URL, body="boom", status=500)
    resp_lib.add(resp_lib.GET, _INSTANCE_V1_URL, body="boom", status=500)
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client().post("x" * 800)

    assert len(_last_body()["status"]) == 500


@resp_lib.activate
def test_a_malformed_instance_response_does_not_break_posting():
    resp_lib.add(resp_lib.GET, _INSTANCE_V2_URL, json={"configuration": {}}, status=200)
    resp_lib.add(resp_lib.GET, _INSTANCE_V1_URL, json={"nope": True}, status=200)
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    ref = _client().post("hello")

    assert ref["id"] == "109999"


@resp_lib.activate
def test_explicit_limit_skips_detection_entirely():
    resp_lib.add(resp_lib.POST, _STATUSES_URL, json=_status(), status=200)

    _client(max_chars=2000).post("hello")

    assert all(c.request.method == "POST" for c in resp_lib.calls)
