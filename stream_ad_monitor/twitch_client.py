"""Twitch Helix API client for detecting stream start/end and reading title."""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_HELIX_BASE = "https://api.twitch.tv/helix"


class TwitchClient:
    """Wraps the Twitch Helix API to check stream status for a given channel."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Fetch an app-access token using client-credentials flow."""
        response = self._session.post(
            _TOKEN_URL,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        self._access_token = response.json()["access_token"]
        logger.debug("Twitch authentication successful.")

    # ------------------------------------------------------------------
    # Stream status
    # ------------------------------------------------------------------

    def get_stream(self, user_login: str) -> Optional[dict]:
        """Return the live stream object for *user_login*, or None if offline.

        Automatically re-authenticates if no token is cached yet.
        """
        if self._access_token is None:
            self.authenticate()

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._access_token}",
        }
        response = self._session.get(
            f"{_HELIX_BASE}/streams",
            params={"user_login": user_login},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        return data[0] if data else None

    def is_live(self, user_login: str) -> bool:
        """Return True when *user_login* is currently streaming."""
        stream = self.get_stream(user_login)
        return stream is not None

    def get_stream_title(self, user_login: str) -> Optional[str]:
        """Return the current stream title, or None if the channel is offline."""
        stream = self.get_stream(user_login)
        if stream is None:
            return None
        return stream.get("title", "")
