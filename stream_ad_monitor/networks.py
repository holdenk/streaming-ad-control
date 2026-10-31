"""Registry of supported ad networks.

One entry per network says where its campaign IDs live on a :class:`Rule`,
which YAML keys and legacy env var feed them, and — for the plain-REST
networks — which credential env var / Config attribute holds the token and
how to build the client. Reddit is listed too (rules and YAML need it) but
has no ``factory``: its Selenium client needs far more than one token and is
built by the monitor itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


@dataclass(frozen=True)
class Network:
    key: str
    label: str
    # Rule attribute holding this network's campaign IDs.
    rule_attr: str
    # YAML keys accepted for this network's campaign IDs, in precedence order.
    yaml_keys: Tuple[str, ...]
    # Single-rule (no RULES_FILE) env var for one campaign ID.
    legacy_env: str
    # Where to find a campaign ID — used in validation errors.
    id_hint: str
    numeric_ids: bool = True
    # Credential env var and the Config attribute it's read into.
    credential_env: Optional[str] = None
    config_attr: Optional[str] = None
    # Builds a client from the credential; None for Reddit (built by the monitor).
    factory: Optional[Callable[[str], object]] = None


def _trafficstars(token: str):
    from .trafficstars_client import TrafficStarsClient

    return TrafficStarsClient(token)


def _exoclick(token: str):
    from .exoclick_client import ExoClickClient

    return ExoClickClient(token)


def _propellerads(token: str):
    from .propellerads_client import PropellerAdsClient

    return PropellerAdsClient(token)


def _meta(token: str):
    from .meta_ads_client import DEFAULT_API_VERSION, MetaAdsClient

    return MetaAdsClient(
        token, api_version=os.environ.get("META_API_VERSION", "").strip() or DEFAULT_API_VERSION
    )


NETWORKS: Tuple[Network, ...] = (
    Network(
        key="reddit",
        label="Reddit Ads",
        rule_attr="campaign_ids",
        yaml_keys=("campaign_ids", "reddit_campaign_ids"),
        legacy_env="REDDIT_CAMPAIGN_ID",
        id_hint="the ads.reddit.com campaign URL",
        numeric_ids=False,
    ),
    Network(
        key="trafficstars",
        label="TrafficStars",
        rule_attr="trafficstars_campaign_ids",
        yaml_keys=("trafficstars_campaign_ids",),
        legacy_env="TRAFFICSTARS_CAMPAIGN_ID",
        id_hint="the admin.trafficstars.com campaign list",
        credential_env="TRAFFICSTARS_API_KEY",
        config_attr="trafficstars_api_key",
        factory=_trafficstars,
    ),
    Network(
        key="exoclick",
        label="ExoClick",
        rule_attr="exoclick_campaign_ids",
        yaml_keys=("exoclick_campaign_ids",),
        legacy_env="EXOCLICK_CAMPAIGN_ID",
        id_hint="the admin.exoclick.com campaign list",
        credential_env="EXOCLICK_API_TOKEN",
        config_attr="exoclick_api_token",
        factory=_exoclick,
    ),
    Network(
        key="propellerads",
        label="PropellerAds",
        rule_attr="propellerads_campaign_ids",
        yaml_keys=("propellerads_campaign_ids",),
        legacy_env="PROPELLERADS_CAMPAIGN_ID",
        id_hint="the partners.propellerads.com campaign list",
        credential_env="PROPELLERADS_API_TOKEN",
        config_attr="propellerads_api_token",
        factory=_propellerads,
    ),
    Network(
        key="meta",
        label="Meta (Facebook/Instagram)",
        rule_attr="meta_campaign_ids",
        yaml_keys=("meta_campaign_ids",),
        legacy_env="META_CAMPAIGN_ID",
        id_hint="the Campaign ID column in Meta Ads Manager",
        credential_env="META_ACCESS_TOKEN",
        config_attr="meta_access_token",
        factory=_meta,
    ),
)

NETWORKS_BY_KEY = {n.key: n for n in NETWORKS}
