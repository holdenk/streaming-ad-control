"""Reddit Ads API client for enabling and disabling a specific ad group."""

from __future__ import annotations

import logging
from typing import Optional

import requests

from . import mask_credential

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_ADS_BASE = "https://ads-api.reddit.com/api/v3"

_STATUS_ACTIVE = "ACTIVE"
_STATUS_PAUSED = "PAUSED"


class RedditAdClient:
    """Wraps the Reddit Ads API to activate or pause a specific ad group."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_id: str,
        reddit_username: str = "",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self._access_token: Optional[str] = None
        self._session = requests.Session()
        # Reddit requires: <platform>:<app_id>:<version> (by /u/<username>)
        # See https://github.com/reddit-archive/reddit/wiki/API
        ua = "linux:stream-ad-monitor:1.0"
        if reddit_username:
            ua += f" (by /u/{reddit_username})"
        self._session.headers["User-Agent"] = ua

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Fetch an OAuth2 access token using client-credentials flow."""
        logger.debug(
            "Attempting Reddit auth with client_id=%s",
            mask_credential(self.client_id),
        )
        response = self._session.post(
            _TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        if not response.ok:
            logger.error(
                "Reddit auth failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        self._access_token = response.json()["access_token"]
        logger.debug("Reddit Ads authentication successful.")

    # ------------------------------------------------------------------
    # Ad group control
    # ------------------------------------------------------------------

    def _patch_ad_group(self, ad_group_id: str, status: str) -> dict:
        """PATCH the ad group to the given *status* and return the response body."""
        if self._access_token is None:
            self.authenticate()

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        url = f"{_ADS_BASE}/accounts/{self.account_id}/ad_groups/{ad_group_id}"
        response = self._session.patch(
            url,
            json={"status": status},
            headers=headers,
        )

        if response.status_code in (401, 403):
            logger.warning(
                "Reddit API returned %d for ad group %s; re-authenticating and retrying once.",
                response.status_code,
                ad_group_id,
            )
            self._access_token = None
            self.authenticate()
            headers["Authorization"] = f"Bearer {self._access_token}"
            response = self._session.patch(
                url,
                json={"status": status},
                headers=headers,
            )

        if not response.ok:
            logger.error(
                "Reddit API error: status=%d, url=%s, body=%s",
                response.status_code,
                url,
                response.text[:500],
            )
        response.raise_for_status()
        return response.json()

    def enable_ad_group(self, ad_group_id: str) -> dict:
        """Set the ad group status to ACTIVE (start serving the ad)."""
        logger.info("Enabling Reddit ad group %s.", ad_group_id)
        return self._patch_ad_group(ad_group_id, _STATUS_ACTIVE)

    def disable_ad_group(self, ad_group_id: str) -> dict:
        """Set the ad group status to PAUSED (stop serving the ad)."""
        logger.info("Disabling Reddit ad group %s.", ad_group_id)
        return self._patch_ad_group(ad_group_id, _STATUS_PAUSED)
