"""ExoClick API client for pausing/resuming campaigns.

Plain REST, same shape as TrafficStars:

  * Auth: the permanent API token (My Profile → API Tokens on
    https://admin.exoclick.com/) is exchanged for a short-lived bearer via
    ``POST /v2/login`` with ``{"api_token": ...}``. The response carries
    ``token`` and ``expires_in``; the bearer is cached until shortly before
    expiry.
  * Toggle: ``PUT /v2/campaigns/play`` and ``PUT /v2/campaigns/pause`` with a
    JSON body of ``{"campaign_ids": [<int>, ...]}``. The response reports
    problems in an ``errors`` list.

Docs: https://api.exoclick.com/v2/docs/
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

from . import mask_credential

logger = logging.getLogger(__name__)

_API_BASE = "https://api.exoclick.com/v2"
_LOGIN_URL = f"{_API_BASE}/login"
_PLAY_URL = f"{_API_BASE}/campaigns/play"
_PAUSE_URL = f"{_API_BASE}/campaigns/pause"

_REQUEST_TIMEOUT_SEC = 30
_TOKEN_EXPIRY_MARGIN_SEC = 60


def coerce_numeric_id(campaign_id, network: str, where: str) -> int:
    """Campaign IDs on the bulk REST networks are integers; reject others early."""
    try:
        return int(str(campaign_id).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{network} campaign IDs must be numeric, got {campaign_id!r}. "
            f"Find the ID in {where}."
        ) from None


class ExoClickClient:
    """Toggles ExoClick campaigns via the public REST API.

    The bearer is renewed automatically before it expires; a 401 on a
    campaign call forces one re-login and retry.
    """

    def __init__(
        self,
        api_token: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_token:
            raise ValueError("ExoClickClient requires an API token.")
        self.api_token = api_token
        self._session = session or requests.Session()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def authenticate(self) -> None:
        """Exchange the permanent API token for a session bearer."""
        logger.debug(
            "ExoClick auth: exchanging API token %s for a session token.",
            mask_credential(self.api_token),
        )
        response = self._session.post(
            _LOGIN_URL,
            json={"api_token": self.api_token},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if not response.ok:
            logger.error(
                "ExoClick login failed: status=%d, body=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("type") == "2FA":
            raise RuntimeError(
                "ExoClick login asked for two-factor auth. Log in with an API "
                "token (My Profile → API Tokens), not a username/password."
            )
        self._access_token = payload["token"]
        expires_in = float(payload.get("expires_in", 0))
        self._token_expires_at = (
            time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN_SEC, 0)
        )
        logger.debug("ExoClick login ok (token valid ~%ds).", int(expires_in))

    def _ensure_token(self) -> None:
        if self._access_token is None or time.monotonic() >= self._token_expires_at:
            self.authenticate()

    def enable_campaign(self, campaign_id) -> dict:
        logger.info("Enabling ExoClick campaign %s.", campaign_id)
        return self._set_campaigns_state(_PLAY_URL, "play", [campaign_id])

    def disable_campaign(self, campaign_id) -> dict:
        logger.info("Disabling ExoClick campaign %s.", campaign_id)
        return self._set_campaigns_state(_PAUSE_URL, "pause", [campaign_id])

    def _set_campaigns_state(self, url: str, action: str, campaign_ids: List) -> dict:
        ids = [
            coerce_numeric_id(c, "ExoClick", "the admin.exoclick.com campaign list")
            for c in campaign_ids
        ]
        self._ensure_token()
        response = self._put(url, ids)
        if response.status_code == 401:
            logger.info("ExoClick %s: 401 despite unexpired token; re-authenticating.", action)
            self.authenticate()
            response = self._put(url, ids)

        if not response.ok:
            logger.error(
                "ExoClick %s failed for campaigns %s: status=%d, body=%s",
                action,
                ids,
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            raise RuntimeError(
                f"ExoClick refused to {action} campaign(s) {ids}: {errors!r}"
            )
        logger.debug("ExoClick %s ok for campaigns %s: %r", action, ids, payload)
        return payload

    def _put(self, url: str, ids: List[int]) -> requests.Response:
        return self._session.put(
            url,
            json={"campaign_ids": ids},
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=_REQUEST_TIMEOUT_SEC,
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            logger.debug("HTTP session close raised; ignoring.", exc_info=True)
