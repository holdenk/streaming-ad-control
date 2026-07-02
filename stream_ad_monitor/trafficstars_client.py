"""TrafficStars API client for pausing/resuming campaigns.

Unlike the Reddit side (which needs a real browser), TrafficStars exposes a
plain REST API:

  * Auth: OAuth2 refresh-token grant. The API key generated on
    https://admin.trafficstars.com/profile/ is used as the refresh token
    against ``POST /v1/auth/token``; the returned bearer lives ~10h
    (``expires_in``) and is cached until shortly before expiry.
  * Toggle: ``PUT /v2/campaigns/run`` and ``PUT /v2/campaigns/pause`` with a
    JSON body of ``{"campaign_ids": [<int>, ...]}``. The response reports
    per-campaign success: ``{"success": [...], "failed": [...], "total": n}``.

Docs: https://docs.trafficstars.com/
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

from . import mask_credential

logger = logging.getLogger(__name__)

_API_BASE = "https://api.trafficstars.com"
_TOKEN_URL = f"{_API_BASE}/v1/auth/token"
_RUN_URL = f"{_API_BASE}/v2/campaigns/run"
_PAUSE_URL = f"{_API_BASE}/v2/campaigns/pause"

_REQUEST_TIMEOUT_SEC = 30
# Renew the bearer this many seconds before the server-reported expiry so a
# request never goes out with a token that dies in flight.
_TOKEN_EXPIRY_MARGIN_SEC = 60


class TrafficStarsClient:
    """Toggles TrafficStars campaigns via the public REST API.

    Auth: the account API key acts as an OAuth2 refresh token. The access
    token is cached and renewed automatically before it expires; a 401 on a
    campaign call forces one re-auth and retry (in case the token was
    revoked server-side ahead of schedule).
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_key:
            raise ValueError("TrafficStarsClient requires an API key.")
        self.api_key = api_key
        self._session = session or requests.Session()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Exchange the API key for a fresh access token."""
        logger.debug(
            "TrafficStars auth: exchanging API key %s for access token.",
            mask_credential(self.api_key),
        )
        response = self._session.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.api_key},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if not response.ok:
            logger.error(
                "TrafficStars auth failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 0))
        self._token_expires_at = (
            time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN_SEC, 0)
        )
        logger.debug(
            "TrafficStars authentication successful (token valid ~%ds).",
            int(expires_in),
        )

    def _ensure_token(self) -> None:
        if self._access_token is None or time.monotonic() >= self._token_expires_at:
            self.authenticate()

    # ------------------------------------------------------------------
    # Campaign control
    # ------------------------------------------------------------------

    def enable_campaign(self, campaign_id) -> dict:
        """Set the campaign running (status ``enabled``)."""
        logger.info("Enabling TrafficStars campaign %s.", campaign_id)
        return self._set_campaigns_state(_RUN_URL, "run", [campaign_id])

    def disable_campaign(self, campaign_id) -> dict:
        """Pause the campaign."""
        logger.info("Disabling TrafficStars campaign %s.", campaign_id)
        return self._set_campaigns_state(_PAUSE_URL, "pause", [campaign_id])

    def _set_campaigns_state(self, url: str, action: str, campaign_ids: List) -> dict:
        ids = [self._coerce_campaign_id(c) for c in campaign_ids]
        self._ensure_token()
        response = self._put_campaign_state(url, ids)

        # 401 means the token was invalidated before its advertised expiry
        # (password change, key rotation, server-side revocation). Re-auth
        # once and retry.
        if response.status_code == 401:
            logger.info(
                "TrafficStars %s: 401 despite unexpired token; re-authenticating.",
                action,
            )
            self.authenticate()
            response = self._put_campaign_state(url, ids)

        if not response.ok:
            logger.error(
                "TrafficStars %s failed for campaigns %s: status=%d, body=%s",
                action,
                ids,
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        payload = response.json()
        failed = payload.get("failed") or []
        if failed:
            raise RuntimeError(
                f"TrafficStars refused to {action} campaign(s) {failed} "
                f"(response: {payload!r}). Check the campaign exists, is "
                "approved, and the account has funds."
            )
        logger.debug("TrafficStars %s ok for campaigns %s: %r", action, ids, payload)
        return payload

    def _put_campaign_state(self, url: str, ids: List[int]) -> requests.Response:
        return self._session.put(
            url,
            json={"campaign_ids": ids},
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=_REQUEST_TIMEOUT_SEC,
        )

    @staticmethod
    def _coerce_campaign_id(campaign_id) -> int:
        """TrafficStars campaign IDs are integers; reject anything else early."""
        try:
            return int(str(campaign_id).strip())
        except (TypeError, ValueError):
            raise ValueError(
                f"TrafficStars campaign IDs must be numeric, got {campaign_id!r}. "
                "Find the ID in the admin.trafficstars.com campaign list."
            ) from None

    def close(self) -> None:
        """Tear down the HTTP session. Idempotent."""
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
