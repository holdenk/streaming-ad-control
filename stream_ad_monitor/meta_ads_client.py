"""Meta (Facebook / Instagram) Marketing API client for pausing/resuming campaigns.

One Graph API call per toggle:

  * Auth: a long-lived **system user** access token with the
    ``ads_management`` permission (Business Settings → Users → System users →
    Generate new token), with the ad account assigned to that system user.
    System user tokens don't expire, so there is no refresh dance.
  * Toggle: ``POST https://graph.facebook.com/<version>/<campaign_id>`` with
    form field ``status=ACTIVE`` or ``status=PAUSED``. Success is
    ``{"success": true}``. Pausing a campaign also stops its ad sets and ads.

Docs: https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group/
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .exoclick_client import coerce_numeric_id

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v25.0"
_GRAPH_BASE = "https://graph.facebook.com"
_REQUEST_TIMEOUT_SEC = 30


class MetaAdsClient:
    """Toggles Meta ad campaigns via the Graph Marketing API."""

    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not access_token:
            raise ValueError("MetaAdsClient requires an access token.")
        self.access_token = access_token
        self.api_version = api_version or DEFAULT_API_VERSION
        self._session = session or requests.Session()

    def enable_campaign(self, campaign_id) -> dict:
        logger.info("Enabling Meta campaign %s.", campaign_id)
        return self._set_status(campaign_id, "ACTIVE")

    def disable_campaign(self, campaign_id) -> dict:
        logger.info("Disabling Meta campaign %s.", campaign_id)
        return self._set_status(campaign_id, "PAUSED")

    def _set_status(self, campaign_id, status: str) -> dict:
        cid = coerce_numeric_id(
            campaign_id, "Meta", "the Campaign ID column in Ads Manager"
        )
        # The token goes in the form body, not the URL, so it never lands in
        # an access log or a requests exception message.
        response = self._session.post(
            f"{_GRAPH_BASE}/{self.api_version}/{cid}",
            data={"status": status, "access_token": self.access_token},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        error = payload.get("error") if isinstance(payload, dict) else None
        if not response.ok or error:
            message = (error or {}).get("message") or response.text[:500]
            logger.error(
                "Meta set status=%s failed for campaign %s: status=%d, error=%s",
                status,
                cid,
                response.status_code,
                message,
            )
            raise RuntimeError(
                f"Meta refused to set campaign {cid} to {status}: {message}"
            )
        if payload.get("success") is not True:
            raise RuntimeError(
                f"Meta returned an unexpected response setting campaign {cid} "
                f"to {status}: {payload!r}"
            )
        return payload

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
