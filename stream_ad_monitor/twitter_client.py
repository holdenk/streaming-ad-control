"""X (Twitter) API v2 client for posting the go-live announcement.

Auth is OAuth 1.0a user context: four static credentials from the app's
"Keys and tokens" tab (API key/secret + access token/secret), with no refresh
step, which is what a long-running daemon wants. The access token must belong
to the account that should appear as the author, and the app needs *Read and
write* permission — tokens minted before that permission was granted keep the
old scope, so regenerate them after changing it.

Posting is ``POST /2/tweets``; a follow-up is the same call with a
``reply.in_reply_to_tweet_id`` so the YouTube link threads under the original
announcement instead of landing as an orphan post.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .text_limits import truncate_weighted, weighted_length

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised only by the import-failure path
    from requests_oauthlib import OAuth1Session
except ImportError:  # pragma: no cover
    OAuth1Session = None  # type: ignore[assignment]

_TWEETS_URL = "https://api.twitter.com/2/tweets"
_STATUS_URL = "https://x.com/i/web/status/{tweet_id}"

_REQUEST_TIMEOUT_SEC = 30
# Weighted, not counted: see text_limits. A plain len() would wave through a
# CJK or emoji-heavy post that X rejects.
_MAX_WEIGHTED_CHARS = 280


class TwitterClient:
    """Posts announcements to X on behalf of the configured account."""

    name = "twitter"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        missing = [
            var
            for var, value in (
                ("TWITTER_API_KEY", api_key),
                ("TWITTER_API_SECRET", api_secret),
                ("TWITTER_ACCESS_TOKEN", access_token),
                ("TWITTER_ACCESS_TOKEN_SECRET", access_token_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "TwitterClient is missing credentials: " + ", ".join(missing)
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self._session = session or self._build_oauth_session()

    def _build_oauth_session(self) -> requests.Session:
        if OAuth1Session is None:
            raise RuntimeError(
                "Posting to X needs the 'requests-oauthlib' package. Install "
                "it (it is a main dependency of this project: "
                "`poetry install --only main`) or unset the TWITTER_* env "
                "vars to disable X announcements."
            )
        return OAuth1Session(
            client_key=self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=self.access_token,
            resource_owner_secret=self.access_token_secret,
        )

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post(
        self,
        text: str,
        reply_to: Optional[dict] = None,
        dedupe_key: str = "",
    ) -> dict:
        """Publish *text*, optionally as a reply to a previous post.

        Args:
            text: Post body. Truncated at 280 characters.
            reply_to: A ref previously returned by this method; the new post
                threads under it.
            dedupe_key: Accepted for interface parity with the other
                platforms and ignored — X has no idempotency key, though it
                does reject a post duplicating a recent one with a 403,
                which covers the same ground for identical text.

        Returns:
            A ref dict: ``{"id": ..., "url": ...}``.

        Raises:
            requests.HTTPError: On any non-2xx response. Note that X rejects
                a post whose text duplicates a recent one with a 403.
            RuntimeError: If the response carries no tweet id. Without one
                there is nothing to thread the YouTube follow-up onto, so
                this counts as a failure rather than a post with an empty
                permalink.
        """
        body: dict = {"text": self._truncate(text)}
        if reply_to and reply_to.get("id"):
            body["reply"] = {"in_reply_to_tweet_id": str(reply_to["id"])}

        response = self._session.post(
            _TWEETS_URL, json=body, timeout=_REQUEST_TIMEOUT_SEC
        )
        if not response.ok:
            logger.error(
                "X post failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        try:
            data = (response.json() or {}).get("data") or {}
        except ValueError:
            data = {}
        tweet_id = str(data.get("id") or "")
        if not tweet_id:
            raise RuntimeError(
                "X returned success but no tweet id "
                f"(body: {response.text[:200]!r}); treating the post as "
                "failed so it isn't threaded onto or reported as posted."
            )
        ref = {"id": tweet_id, "url": _STATUS_URL.format(tweet_id=tweet_id)}
        logger.info("Posted to X: %s", ref["url"])
        return ref

    @staticmethod
    def _truncate(text: str) -> str:
        weighted = weighted_length(text)
        if weighted <= _MAX_WEIGHTED_CHARS:
            return text
        logger.warning(
            "Announcement weighs %d of X's %d characters; truncating.",
            weighted,
            _MAX_WEIGHTED_CHARS,
        )
        return truncate_weighted(text, _MAX_WEIGHTED_CHARS)

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
