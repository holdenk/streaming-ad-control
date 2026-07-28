"""Credential-free reader for a Twitch channel's live state and title.

The Helix client (``twitch_client.py``) needs a registered developer app —
a client id **and** secret. That's more setup than this project needs just to
answer "is my channel live, and what's the title?". This client asks Twitch's
public GQL endpoint instead, using the same public web client-id the Twitch
website itself ships (the one tools like streamlink use). It requires **no**
account, login, or secret, and only ever reads public, unauthenticated data —
it never touches your account or your ads.

It's a drop-in for :meth:`TwitchClient.get_stream`: returns a stream-like dict
(carrying at least ``title``) when the channel is live, or ``None`` when it's
offline — so the monitor and the OBS gate can use either interchangeably.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TwitchLookupError(RuntimeError):
    """Raised when Twitch's live-state couldn't be determined at all.

    Deliberately distinct from "the channel is offline" (``get_stream`` returning
    ``None``). A network blip, a 5xx, or a malformed body means we *don't know*
    the stream state — callers that would otherwise treat it as "offline" must
    not tear a live campaign down over one failed request.
    """


_GQL_URL = "https://gql.twitch.tv/gql"
# Public web client-id embedded in the Twitch site itself. Not account-specific,
# not a secret — reading public stream metadata with it needs no login.
_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_REQUEST_TIMEOUT_SEC = 15

# Parameterised so the login can't break the query string. ``stream`` is null
# when offline; ``broadcastSettings.title`` is the current title.
_QUERY = (
    "query($login:String!){"
    "user(login:$login){stream{id type} broadcastSettings{title}}}"
)


class TwitchPublicTitleClient:
    """Reads is-live + current title via Twitch's public GQL API (no credentials)."""

    def __init__(self, *, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()

    def get_stream(self, user_login: str) -> Optional[dict]:
        """Return a stream dict (with ``title``) if *user_login* is live, else None.

        ``None`` means Twitch answered and the channel is **genuinely offline**
        (or doesn't exist). Anything that leaves the state *unknown* — network
        failure, non-200, malformed body — raises :class:`TwitchLookupError`
        instead, so callers can tell "the stream ended" apart from "I couldn't
        reach Twitch". Conflating the two would let one failed request tear down
        a campaign mid-stream.
        """
        if not user_login:
            return None
        try:
            resp = self._session.post(
                _GQL_URL,
                json={"query": _QUERY, "variables": {"login": user_login}},
                headers={"Client-Id": _PUBLIC_CLIENT_ID},
                timeout=_REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise TwitchLookupError(f"Twitch GQL request failed: {exc!r}") from exc
        if not resp.ok:
            raise TwitchLookupError(
                f"Twitch GQL returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json() or {}
        except ValueError as exc:
            raise TwitchLookupError("Twitch GQL returned a non-JSON body.") from exc
        if not isinstance(body, dict) or "data" not in body:
            raise TwitchLookupError(f"Unexpected Twitch GQL payload: {str(body)[:200]}")
        user = (body.get("data") or {}).get("user")
        if not user or user.get("stream") is None:
            return None  # channel is offline (or unknown login)
        title = ((user.get("broadcastSettings") or {}).get("title")) or ""
        stream = dict(user["stream"])
        stream["title"] = title
        return stream

    def get_stream_title(self, user_login: str) -> Optional[str]:
        """Return the current live title, or None if the channel is offline."""
        stream = self.get_stream(user_login)
        return None if stream is None else stream.get("title", "")

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
