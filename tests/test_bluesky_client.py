"""Tests for stream_ad_monitor.bluesky_client."""

import json

import pytest
import requests
import responses as resp_lib

from stream_ad_monitor.bluesky_client import BlueskyClient, link_facets

_PDS = "https://bsky.social"
_SESSION_URL = f"{_PDS}/xrpc/com.atproto.server.createSession"
_REFRESH_URL = f"{_PDS}/xrpc/com.atproto.server.refreshSession"
_CREATE_URL = f"{_PDS}/xrpc/com.atproto.repo.createRecord"

_POST_URI = "at://did:plc:abc/app.bsky.feed.post/3kabc"


def _client():
    return BlueskyClient("holden.bsky.social", "app-pass", session=requests.Session())


def _add_session(access="access-1", refresh="refresh-1"):
    resp_lib.add(
        resp_lib.POST,
        _SESSION_URL,
        json={
            "accessJwt": access,
            "refreshJwt": refresh,
            "did": "did:plc:abc",
            "handle": "holden.bsky.social",
        },
        status=200,
    )


def _add_created(uri=_POST_URI, cid="bafycid"):
    resp_lib.add(resp_lib.POST, _CREATE_URL, json={"uri": uri, "cid": cid}, status=200)


def _last_record():
    for call in reversed(resp_lib.calls):
        if call.request.url == _CREATE_URL:
            return json.loads(call.request.body)["record"]
    raise AssertionError("no createRecord call was made")


# ---------------------------------------------------------------------------
# link_facets
# ---------------------------------------------------------------------------


def test_link_facets_indexes_a_plain_url():
    text = "Live: https://twitch.tv/holden"
    facets = link_facets(text)

    assert len(facets) == 1
    index = facets[0]["index"]
    raw = text.encode("utf-8")
    assert raw[index["byteStart"] : index["byteEnd"]] == b"https://twitch.tv/holden"
    assert facets[0]["features"][0]["uri"] == "https://twitch.tv/holden"
    assert facets[0]["features"][0]["$type"] == "app.bsky.richtext.facet#link"


def test_link_facets_uses_byte_offsets_not_character_offsets():
    """The default template leads with an emoji, which is 4 bytes but 1 char."""
    text = "🔴 Live now: https://twitch.tv/holden"
    facet = link_facets(text)[0]
    raw = text.encode("utf-8")

    assert raw[facet["index"]["byteStart"] : facet["index"]["byteEnd"]] == (
        b"https://twitch.tv/holden"
    )
    # Character offset would have been 13; the byte offset is larger.
    assert facet["index"]["byteStart"] == text.index("https") + 3


def test_link_facets_finds_every_url():
    text = "https://twitch.tv/holden\nhttps://www.youtube.com/watch?v=abc"
    facets = link_facets(text)
    uris = [f["features"][0]["uri"] for f in facets]
    assert uris == [
        "https://twitch.tv/holden",
        "https://www.youtube.com/watch?v=abc",
    ]


def test_link_facets_excludes_trailing_sentence_punctuation():
    facet = link_facets("watch at https://twitch.tv/holden.")[0]
    assert facet["features"][0]["uri"] == "https://twitch.tv/holden"


def test_link_facets_returns_empty_without_urls():
    assert link_facets("no links here") == []


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_requires_handle_and_app_password():
    with pytest.raises(ValueError, match="BLUESKY_APP_PASSWORD"):
        BlueskyClient("holden.bsky.social", "")


def test_handle_at_prefix_is_stripped():
    assert BlueskyClient("@holden.bsky.social", "pw").handle == "holden.bsky.social"


def test_custom_pds_url_is_used():
    client = BlueskyClient("h", "pw", pds_url="https://pds.example.com/")
    assert client._xrpc("com.atproto.repo.createRecord") == (
        "https://pds.example.com/xrpc/com.atproto.repo.createRecord"
    )


@resp_lib.activate
def test_post_logs_in_on_first_use():
    _add_session()
    _add_created()

    _client().post("hello")

    login_body = json.loads(resp_lib.calls[0].request.body)
    assert login_body == {"identifier": "holden.bsky.social", "password": "app-pass"}
    assert resp_lib.calls[1].request.headers["Authorization"] == "Bearer access-1"


@resp_lib.activate
def test_second_post_reuses_the_session():
    _add_session()
    _add_created()
    _add_created()

    client = _client()
    client.post("one")
    client.post("two")

    logins = [c for c in resp_lib.calls if c.request.url == _SESSION_URL]
    assert len(logins) == 1


@resp_lib.activate
def test_expired_token_is_refreshed_and_the_post_retried():
    _add_session()
    resp_lib.add(
        resp_lib.POST, _CREATE_URL, json={"error": "ExpiredToken"}, status=400
    )
    resp_lib.add(
        resp_lib.POST,
        _REFRESH_URL,
        json={"accessJwt": "access-2", "refreshJwt": "refresh-2", "did": "did:plc:abc"},
        status=200,
    )
    _add_created()

    ref = _client().post("hello")

    assert ref["uri"] == _POST_URI
    creates = [c for c in resp_lib.calls if c.request.url == _CREATE_URL]
    assert len(creates) == 2
    assert creates[1].request.headers["Authorization"] == "Bearer access-2"


@resp_lib.activate
def test_failed_refresh_falls_back_to_a_full_login():
    _add_session(access="access-1")
    resp_lib.add(
        resp_lib.POST, _CREATE_URL, json={"error": "ExpiredToken"}, status=400
    )
    resp_lib.add(resp_lib.POST, _REFRESH_URL, json={"error": "ExpiredToken"}, status=400)
    _add_session(access="access-3")
    _add_created()

    _client().post("hello")

    creates = [c for c in resp_lib.calls if c.request.url == _CREATE_URL]
    assert creates[1].request.headers["Authorization"] == "Bearer access-3"


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_post_builds_a_feed_post_record_with_facets():
    _add_session()
    _add_created()

    ref = _client().post("🔴 Live now\n\nhttps://twitch.tv/holden")

    record = _last_record()
    assert record["$type"] == "app.bsky.feed.post"
    assert record["text"] == "🔴 Live now\n\nhttps://twitch.tv/holden"
    assert record["createdAt"].endswith("Z")
    assert len(record["facets"]) == 1
    assert ref == {
        "uri": _POST_URI,
        "cid": "bafycid",
        "url": "https://bsky.app/profile/holden.bsky.social/post/3kabc",
    }


@resp_lib.activate
def test_post_without_links_omits_facets():
    _add_session()
    _add_created()

    _client().post("no links")

    assert "facets" not in _last_record()


@resp_lib.activate
def test_reply_carries_root_and_parent_strong_refs():
    _add_session()
    _add_created(uri="at://did:plc:abc/app.bsky.feed.post/3kreply", cid="bafyreply")

    root = {"uri": _POST_URI, "cid": "bafycid"}
    ref = _client().post("Also on YouTube", reply_to=root)

    reply = _last_record()["reply"]
    assert reply["root"] == root
    assert reply["parent"] == root
    # A reply's ref keeps pointing at the thread root for any further replies.
    assert ref["root"] == root


@resp_lib.activate
def test_reply_to_an_incomplete_ref_posts_standalone():
    _add_session()
    _add_created()

    _client().post("orphan", reply_to={"uri": _POST_URI})  # no cid

    assert "reply" not in _last_record()


@resp_lib.activate
def test_post_truncates_over_the_character_limit():
    _add_session()
    _add_created()

    _client().post("x" * 400)

    text = _last_record()["text"]
    assert len(text) == 300
    assert text.endswith("…")


@resp_lib.activate
def test_post_raises_on_api_error():
    _add_session()
    resp_lib.add(
        resp_lib.POST, _CREATE_URL, json={"error": "RateLimitExceeded"}, status=429
    )
    with pytest.raises(requests.HTTPError):
        _client().post("hello")


@resp_lib.activate
def test_bad_app_password_raises_on_login():
    resp_lib.add(
        resp_lib.POST,
        _SESSION_URL,
        json={"error": "AuthenticationRequired"},
        status=401,
    )
    with pytest.raises(requests.HTTPError):
        _client().post("hello")


@resp_lib.activate
def test_success_without_a_strong_ref_is_an_error():
    """Without uri+cid there is nothing to thread the follow-up onto."""
    _add_session()
    resp_lib.add(resp_lib.POST, _CREATE_URL, json={"uri": _POST_URI}, status=200)

    with pytest.raises(RuntimeError, match="no uri/cid"):
        _client().post("hello")


@resp_lib.activate
def test_success_with_a_non_json_body_is_an_error():
    _add_session()
    resp_lib.add(resp_lib.POST, _CREATE_URL, body="not json", status=200)

    with pytest.raises(RuntimeError, match="no uri/cid"):
        _client().post("hello")


def test_a_plain_http_pds_is_refused():
    """The app password would go over the wire in the clear."""
    with pytest.raises(ValueError, match="https"):
        BlueskyClient("h.bsky.social", "pw", pds_url="http://pds.example.com")


def test_plain_http_is_allowed_for_a_loopback_pds():
    client = BlueskyClient("h.bsky.social", "pw", pds_url="http://localhost:2583")
    assert client.pds_url == "http://localhost:2583"


# ---------------------------------------------------------------------------
# Redirects on credential-bearing requests
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_login_refuses_an_https_to_http_downgrade_redirect():
    """A 307 replays the body, and the login body *is* the app password.

    requests only strips Authorization across hosts, and the password is not
    in a header, so nothing else would stop it reaching a plaintext endpoint.
    """
    insecure = "http://bsky.social/xrpc/com.atproto.server.createSession"
    resp_lib.add(
        resp_lib.POST, _SESSION_URL, status=307, headers={"Location": insecure}
    )

    with pytest.raises(RuntimeError, match="redirected"):
        _client().post("hello")

    assert not [c for c in resp_lib.calls if c.request.url.startswith("http://")]


@resp_lib.activate
def test_the_app_password_never_reaches_a_plaintext_endpoint():
    insecure = "http://bsky.social/xrpc/com.atproto.server.createSession"
    resp_lib.add(
        resp_lib.POST, _SESSION_URL, status=308, headers={"Location": insecure}
    )

    with pytest.raises(RuntimeError):
        _client().post("hello")

    bodies = [c.request.body or b"" for c in resp_lib.calls]
    assert not any(b"app-pass" in (b if isinstance(b, bytes) else b.encode()) for b in bodies[1:])


@resp_lib.activate
def test_posting_refuses_a_redirect_too():
    _add_session()
    resp_lib.add(
        resp_lib.POST,
        _CREATE_URL,
        status=307,
        headers={"Location": "http://bsky.social/xrpc/com.atproto.repo.createRecord"},
    )

    with pytest.raises(RuntimeError, match="redirected"):
        _client().post("hello")
