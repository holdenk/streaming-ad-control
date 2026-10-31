"""PropellerAds (SSP API v5) client for starting/stopping campaigns.

The simplest of the REST networks — no token exchange at all:

  * Auth: the API token from the Profile page of your PropellerAds advertiser
    account is sent as a static ``Authorization: Bearer`` header.
  * Toggle: ``PUT /v5/adv/campaigns/play`` and ``PUT /v5/adv/campaigns/stop``
    with ``{"campaign_ids": [<int>, ...]}``. A 200 can still carry per-campaign
    failures in an ``errors`` object keyed by campaign id.

Docs: https://ssp-api.propellerads.com/v5/docs/
"""

from __future__ import annotations

import logging
from typing import List, Optional

import requests

from .exoclick_client import coerce_numeric_id

logger = logging.getLogger(__name__)

_API_BASE = "https://ssp-api.propellerads.com/v5/adv"
_PLAY_URL = f"{_API_BASE}/campaigns/play"
_STOP_URL = f"{_API_BASE}/campaigns/stop"

_REQUEST_TIMEOUT_SEC = 30


class PropellerAdsClient:
    """Toggles PropellerAds campaigns via the SSP REST API."""

    def __init__(
        self,
        api_token: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_token:
            raise ValueError("PropellerAdsClient requires an API token.")
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_token}"

    def enable_campaign(self, campaign_id) -> dict:
        logger.info("Enabling PropellerAds campaign %s.", campaign_id)
        return self._set_campaigns_state(_PLAY_URL, "play", [campaign_id])

    def disable_campaign(self, campaign_id) -> dict:
        logger.info("Disabling PropellerAds campaign %s.", campaign_id)
        return self._set_campaigns_state(_STOP_URL, "stop", [campaign_id])

    def _set_campaigns_state(self, url: str, action: str, campaign_ids: List) -> dict:
        ids = [
            coerce_numeric_id(
                c, "PropellerAds", "the partners.propellerads.com campaign list"
            )
            for c in campaign_ids
        ]
        response = self._session.put(
            url, json={"campaign_ids": ids}, timeout=_REQUEST_TIMEOUT_SEC
        )
        if not response.ok:
            logger.error(
                "PropellerAds %s failed for campaigns %s: status=%d, body=%s",
                action,
                ids,
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        payload = response.json() if response.content else {}
        errors = payload.get("errors") or {}
        if errors:
            raise RuntimeError(
                f"PropellerAds refused to {action} campaign(s) {ids}: {errors!r}"
            )
        logger.debug("PropellerAds %s ok for campaigns %s: %r", action, ids, payload)
        return payload

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
