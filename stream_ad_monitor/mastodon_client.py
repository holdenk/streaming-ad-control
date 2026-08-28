"""Mastodon client for posting the go-live announcement.

The simplest of the three platforms. Auth is a single access token from
Preferences → Development → New application (the ``write:statuses`` scope is
all this needs); it doesn't expire, so there's no refresh path to maintain.
Posting is ``POST /api/v1/statuses``, and a follow-up is the same call with
``in_reply_to_id`` so the YouTube link threads under the announcement.

Two things Mastodon handles that the other clients have to do themselves:
URLs are auto-linked, so no rich-text markup is needed, and the server honours
an ``Idempotency-Key`` header — a retry after a request that timed out *after*
the status landed is collapsed server-side instead of double-posting.

The instance is configurable and defaults to tech.lgbt. Its post length limit
is read from the instance rather than assumed: the stock limit is 500, but
forks routinely raise it (tech.lgbt itself runs glitch-soc at 1024), so both
constants would be wrong somewhere. The lookup happens on the first post, not
at construction, so a network blip at daemon startup can't matter.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

import requests

from . import guard_loopback_session, raise_on_redirect, require_secure_url

logger = logging.getLogger(__name__)

DEFAULT_INSTANCE_URL = "https://tech.lgbt"

_REQUEST_TIMEOUT_SEC = 30
# Assumed only when the instance won't tell us; see _detect_max_chars.
DEFAULT_MAX_CHARS = 500
# v1 is deprecated but still serves instances older than Mastodon 4.0.
_INSTANCE_PATHS = ("/api/v2/instance", "/api/v1/instance")

_VISIBILITIES = ("public", "unlisted", "private", "direct")
_DEFAULT_VISIBILITY = "public"


class MastodonClient:
    """Posts announcements to a Mastodon account."""

    name = "mastodon"

    def __init__(
        self,
        access_token: str,
        instance_url: str = DEFAULT_INSTANCE_URL,
        *,
        visibility: str = _DEFAULT_VISIBILITY,
        max_chars: int = 0,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not access_token:
            raise ValueError("MastodonClient requires MASTODON_ACCESS_TOKEN.")
        self.access_token = access_token
        self.instance_url = require_secure_url(
            (instance_url or DEFAULT_INSTANCE_URL).rstrip("/"),
            "MASTODON_INSTANCE_URL",
        )
        self.visibility = self._validate_visibility(visibility)
        # 0 means "ask the instance on first use".
        self.max_chars = max_chars if max_chars > 0 else 0
        self._detected_max_chars: Optional[int] = None
        self._session = session or requests.Session()
        guard_loopback_session(self._session, self.instance_url)

    @staticmethod
    def _validate_visibility(visibility: str) -> str:
        visibility = (visibility or "").strip().lower() or _DEFAULT_VISIBILITY
        if visibility not in _VISIBILITIES:
            logger.warning(
                "MASTODON_VISIBILITY=%r is not one of %s; posting as %s.",
                visibility,
                ", ".join(_VISIBILITIES),
                _DEFAULT_VISIBILITY,
            )
            return _DEFAULT_VISIBILITY
        return visibility

    @property
    def statuses_url(self) -> str:
        """The instance's status-posting endpoint."""
        return f"{self.instance_url}/api/v1/statuses"

    # ------------------------------------------------------------------
    # Post length limit
    # ------------------------------------------------------------------

    @property
    def char_limit(self) -> int:
        """The instance's post limit: configured, else detected, else 500."""
        if self.max_chars:
            return self.max_chars
        if self._detected_max_chars is None:
            self._detected_max_chars = self._detect_max_chars()
        return self._detected_max_chars

    def _detect_max_chars(self) -> int:
        for path in _INSTANCE_PATHS:
            try:
                response = self._session.get(
                    f"{self.instance_url}{path}", timeout=_REQUEST_TIMEOUT_SEC
                )
                if not response.ok:
                    continue
                statuses = ((response.json() or {}).get("configuration") or {}).get(
                    "statuses"
                ) or {}
                limit = int(statuses.get("max_characters"))
            except Exception:
                logger.debug(
                    "Could not read %s%s.", self.instance_url, path, exc_info=True
                )
                continue
            if limit > 0:
                logger.info(
                    "%s allows %d characters per post.", self.instance_url, limit
                )
                return limit
        logger.info(
            "Could not read the post limit from %s; assuming %d. Set "
            "MASTODON_MAX_CHARS to override.",
            self.instance_url,
            DEFAULT_MAX_CHARS,
        )
        return DEFAULT_MAX_CHARS

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
            text: Post body. Truncated at the instance's character limit.
                URLs are linkified by the server.
            reply_to: A ref previously returned by this method.
            dedupe_key: Identifies the operation (broadcast + phase) behind
                this post. It becomes the idempotency key, so a retry of the
                same operation collapses server-side while two broadcasts
                that happen to render identical text stay distinct. Falls
                back to the text itself when the caller has nothing better.

        Returns:
            A ref dict: ``{"id": ..., "url": ...}``.

        Raises:
            requests.HTTPError: On any non-2xx response.
            RuntimeError: If the response carries no status id — without one
                the YouTube follow-up could not be threaded onto this post.
        """
        text = self._truncate(text)
        body = {"status": text, "visibility": self.visibility}
        reply_id = str((reply_to or {}).get("id") or "")
        if reply_id:
            body["in_reply_to_id"] = reply_id

        response = self._session.post(
            self.statuses_url,
            json=body,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                # Keyed on the operation so a retry of *this* post is
                # recognised, while an identically worded post from another
                # broadcast is not.
                "Idempotency-Key": _idempotency_key(dedupe_key or text, reply_id),
            },
            timeout=_REQUEST_TIMEOUT_SEC,
            allow_redirects=False,
        )
        raise_on_redirect(response, "Mastodon post")
        if not response.ok:
            logger.error(
                "Mastodon post failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        status_id = str(payload.get("id") or "")
        if not status_id:
            raise RuntimeError(
                "Mastodon returned success but no status id "
                f"(body: {response.text[:200]!r}); treating the post as "
                "failed so it isn't threaded onto or reported as posted."
            )
        ref = {"id": status_id, "url": payload.get("url", "")}
        logger.info("Posted to Mastodon: %s", ref["url"] or self.instance_url)
        return ref

    def _truncate(self, text: str) -> str:
        limit = self.char_limit
        if len(text) <= limit:
            return text
        logger.warning(
            "Announcement is %d characters; truncating to %s's %d-character "
            "limit.",
            len(text),
            self.instance_url,
            limit,
        )
        return text[: limit - 1].rstrip() + "…"

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)


def _idempotency_key(operation: str, reply_id: str) -> str:
    return hashlib.sha256(f"{reply_id}\x00{operation}".encode("utf-8")).hexdigest()
