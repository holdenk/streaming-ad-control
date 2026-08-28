"""Bluesky (AT Protocol) client for posting the go-live announcement.

Auth is ``com.atproto.server.createSession`` with a handle plus an *app
password* (Settings → Privacy and security → App passwords) — never the
account password. That returns a short-lived ``accessJwt`` (~2h) and a
long-lived ``refreshJwt``; an expired access token is refreshed in place, and
a refresh failure falls back to a fresh login, so the daemon survives
arbitrarily long uptimes.

Posting is ``com.atproto.repo.createRecord`` into the ``app.bsky.feed.post``
collection. Two details the API leaves to the client:

  * **Links are not auto-detected.** A URL in ``text`` renders as plain text
    unless the record carries a matching ``app.bsky.richtext.facet#link``
    facet, indexed by *byte* offsets into the UTF-8 encoding of the text.
    :func:`link_facets` derives those from the text we compose.
  * **Threading is explicit.** A reply carries both ``root`` and ``parent``
    strong refs (uri + cid), so the YouTube follow-up hangs off the original
    announcement.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

import requests

from . import guard_loopback_session, raise_on_redirect, require_secure_url

logger = logging.getLogger(__name__)

_DEFAULT_PDS_URL = "https://bsky.social"
_POST_COLLECTION = "app.bsky.feed.post"
_POST_URL_TEMPLATE = "https://bsky.app/profile/{handle}/post/{rkey}"

_REQUEST_TIMEOUT_SEC = 30
# Bluesky's limit is 300 graphemes. Counting code points can only ever
# over-count (a grapheme is one or more code points), so a code-point check
# errs on the safe side.
_MAX_CHARS = 300

# URLs as they appear in composed announcements. Closing brackets and
# sentence punctuation are excluded so a trailing "." doesn't end up inside
# the link.
_URL_RE = re.compile(rb"https?://[^\s<>\[\]()]+")
_URL_TRAILING_PUNCT = b".,;:!?'\"-"


def link_facets(text: str) -> List[dict]:
    """Return ``app.bsky.richtext.facet`` entries for every URL in *text*.

    Offsets are byte offsets into ``text.encode("utf-8")``, which is what the
    lexicon specifies — using character offsets silently mis-highlights any
    post containing an emoji or other non-ASCII text.
    """
    facets: List[dict] = []
    raw = text.encode("utf-8")
    for match in _URL_RE.finditer(raw):
        url = match.group(0)
        end = match.end()
        while url and url[-1:] in _URL_TRAILING_PUNCT:
            url = url[:-1]
            end -= 1
        if not url:
            continue
        facets.append(
            {
                "index": {"byteStart": match.start(), "byteEnd": end},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": url.decode("utf-8"),
                    }
                ],
            }
        )
    return facets


class BlueskyClient:
    """Posts announcements to a Bluesky account via its PDS."""

    name = "bluesky"

    def __init__(
        self,
        handle: str,
        app_password: str,
        *,
        pds_url: str = _DEFAULT_PDS_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not handle or not app_password:
            raise ValueError(
                "BlueskyClient requires BLUESKY_HANDLE and BLUESKY_APP_PASSWORD."
            )
        self.handle = handle.lstrip("@")
        self.app_password = app_password
        self.pds_url = require_secure_url(
            (pds_url or _DEFAULT_PDS_URL).rstrip("/"), "BLUESKY_PDS_URL"
        )
        self._session = session or requests.Session()
        guard_loopback_session(self._session, self.pds_url)
        self._access_jwt: Optional[str] = None
        self._refresh_jwt: Optional[str] = None
        self._did: Optional[str] = None
        # The server echoes the canonical handle at login; post URLs use it.
        self._resolved_handle: str = self.handle

    def _xrpc(self, method: str) -> str:
        return f"{self.pds_url}/xrpc/{method}"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Create a fresh session from the handle + app password."""
        logger.debug("Bluesky: creating session for %s.", self.handle)
        response = self._session.post(
            self._xrpc("com.atproto.server.createSession"),
            json={"identifier": self.handle, "password": self.app_password},
            timeout=_REQUEST_TIMEOUT_SEC,
            allow_redirects=False,
        )
        raise_on_redirect(response, "Bluesky login")
        if not response.ok:
            logger.error(
                "Bluesky login failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        self._store_session(response.json())
        logger.info("Bluesky authenticated as %s (%s).", self._resolved_handle, self._did)

    def _store_session(self, payload: dict) -> None:
        self._access_jwt = payload.get("accessJwt")
        self._refresh_jwt = payload.get("refreshJwt")
        self._did = payload.get("did") or self._did
        self._resolved_handle = payload.get("handle") or self._resolved_handle

    def _refresh(self) -> bool:
        """Swap the refresh token for a new access token. False if it failed."""
        if not self._refresh_jwt:
            return False
        logger.debug("Bluesky: access token expired; refreshing session.")
        response = self._session.post(
            self._xrpc("com.atproto.server.refreshSession"),
            headers={"Authorization": f"Bearer {self._refresh_jwt}"},
            timeout=_REQUEST_TIMEOUT_SEC,
            allow_redirects=False,
        )
        raise_on_redirect(response, "Bluesky session refresh")
        if not response.ok:
            logger.info(
                "Bluesky refresh failed (status=%d); falling back to full login.",
                response.status_code,
            )
            return False
        self._store_session(response.json())
        return True

    def _ensure_session(self) -> None:
        if self._access_jwt is None:
            self.login()

    def _reauthenticate(self) -> None:
        if not self._refresh():
            self.login()

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post(
        self,
        text: str,
        reply_to: Optional[dict] = None,
        dedupe_key: str = "",
    ) -> dict:
        """Publish *text*, optionally as a reply threaded under *reply_to*.

        Args:
            text: Post body. Truncated at 300 characters. URLs in it are
                turned into clickable link facets automatically.
            reply_to: A ref previously returned by this method.
            dedupe_key: Accepted for interface parity with the other
                platforms and ignored — the AT Protocol has no idempotency
                key, so a retry after a request that timed out post-delivery
                can duplicate.

        Returns:
            A ref dict: ``{"uri": ..., "cid": ..., "url": ...}``. ``uri`` and
            ``cid`` are the strong ref a later reply needs; ``url`` is the
            human-facing bsky.app permalink.

        Raises:
            requests.HTTPError: On any non-2xx response.
            RuntimeError: If the response carries no uri/cid. That pair is
                the strong ref a reply needs, so without it the YouTube
                follow-up could not be threaded — better to fail here than
                to report a post with an empty permalink.
        """
        self._ensure_session()
        text = self._truncate(text)
        record = {
            "$type": _POST_COLLECTION,
            "text": text,
            "createdAt": _now_iso8601(),
        }
        facets = link_facets(text)
        if facets:
            record["facets"] = facets
        if reply_to and reply_to.get("uri") and reply_to.get("cid"):
            parent = {"uri": reply_to["uri"], "cid": reply_to["cid"]}
            root = reply_to.get("root") or parent
            record["reply"] = {"root": root, "parent": parent}

        response = self._create_record(record)
        # An expired accessJwt shows up as 400 InvalidToken / 401; either way
        # the fix is the same — re-auth once and retry.
        if response.status_code in (400, 401) and _is_expired_token(response):
            self._reauthenticate()
            response = self._create_record(record)

        if not response.ok:
            logger.error(
                "Bluesky post failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        uri, cid = payload.get("uri", ""), payload.get("cid", "")
        if not uri or not cid:
            raise RuntimeError(
                "Bluesky returned success but no uri/cid "
                f"(body: {response.text[:200]!r}); treating the post as "
                "failed so it isn't threaded onto or reported as posted."
            )
        ref = {"uri": uri, "cid": cid, "url": self._permalink(uri)}
        # Replies keep pointing at the thread root, not at themselves.
        if record.get("reply"):
            ref["root"] = record["reply"]["root"]
        logger.info("Posted to Bluesky: %s", ref["url"])
        return ref

    def _create_record(self, record: dict) -> requests.Response:
        response = self._session.post(
            self._xrpc("com.atproto.repo.createRecord"),
            json={
                "repo": self._did,
                "collection": _POST_COLLECTION,
                "record": record,
            },
            headers={"Authorization": f"Bearer {self._access_jwt}"},
            timeout=_REQUEST_TIMEOUT_SEC,
            allow_redirects=False,
        )
        raise_on_redirect(response, "Bluesky post")
        return response

    def _permalink(self, uri: str) -> str:
        """Turn an ``at://did/app.bsky.feed.post/<rkey>`` URI into a web link."""
        if not uri:
            return ""
        rkey = uri.rsplit("/", 1)[-1]
        return _POST_URL_TEMPLATE.format(handle=self._resolved_handle, rkey=rkey)

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= _MAX_CHARS:
            return text
        logger.warning(
            "Announcement is %d characters; truncating to Bluesky's "
            "%d-character limit.",
            len(text),
            _MAX_CHARS,
        )
        return text[: _MAX_CHARS - 1].rstrip() + "…"

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)


def _now_iso8601() -> str:
    """UTC timestamp in the RFC 3339 form the lexicon expects."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _is_expired_token(response: requests.Response) -> bool:
    try:
        error = (response.json() or {}).get("error", "")
    except ValueError:
        return response.status_code == 401
    return error in ("ExpiredToken", "InvalidToken", "AuthMissing")
