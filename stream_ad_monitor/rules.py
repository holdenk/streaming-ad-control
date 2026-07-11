"""Rule dataclass and YAML loader for multi-rule, multi-network ad control."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Rule:
    """One monitoring rule: match any keyword → enable all listed campaigns.

    ``campaign_ids`` are Reddit Ads campaigns (kept under the historical
    name for backward compatibility — ``reddit_campaign_ids`` in YAML maps
    here too). ``trafficstars_campaign_ids`` are TrafficStars campaigns.
    A rule may target either network or both.
    """

    name: str
    keywords: List[str]
    campaign_ids: List[str] = field(default_factory=list)
    trafficstars_campaign_ids: List[str] = field(default_factory=list)

    @property
    def reddit_campaign_ids(self) -> List[str]:
        """Clearer alias for :attr:`campaign_ids` now that there are two networks."""
        return self.campaign_ids

    def matches_title(self, title: str) -> bool:
        """Return True when any keyword appears in *title* (case-insensitive)."""
        title_lower = title.lower()
        return any(kw.lower() in title_lower for kw in self.keywords)


def _as_str_list(value) -> List[str]:
    """Normalise a YAML scalar-or-list into a list of stripped strings.

    YAML parses unquoted numeric IDs as ints; downstream code formats IDs
    into URLs and JSON bodies, so canonicalise to strings here.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(v).strip() for v in value]


def load_rules_from_yaml(path: str) -> List[Rule]:
    """Parse *path* as YAML and return a list of :class:`Rule` objects.

    Expected YAML structure::

        rules:
          - name: "Spark streams"
            keywords:
              - Spark
            campaign_ids:              # Reddit Ads campaigns
              - 2470329120103230906
            trafficstars_campaign_ids: # TrafficStars campaigns
              - 123456
          - name: "Home Assistant"
            keywords:
              - "home assistant"
              - homeassistant
            campaign_ids:
              - 2470329120103231000
              - 2470329120103231001

    Accepted aliases:

    * ``reddit_campaign_ids`` — same as ``campaign_ids``.
    * ``ad_group_ids`` — legacy name, treated as ``campaign_ids`` (with a
      deprecation warning).

    Each rule must list at least one campaign on at least one network.

    Raises:
        ValueError: If the file contains no rules or a rule is malformed.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    raw_rules = (data or {}).get("rules", [])
    if not raw_rules:
        raise ValueError(f"No rules found in '{path}'.")

    rules: List[Rule] = []
    for idx, raw in enumerate(raw_rules):
        name = raw.get("name", f"rule_{idx}")
        keywords = raw.get("keywords", [])

        campaign_ids = raw.get("campaign_ids")
        if campaign_ids is None:
            campaign_ids = raw.get("reddit_campaign_ids")
        if campaign_ids is None and "ad_group_ids" in raw:
            logger.warning(
                "Rule '%s' uses deprecated 'ad_group_ids'; rename to "
                "'campaign_ids' (the value is treated as a campaign ID list).",
                name,
            )
            campaign_ids = raw["ad_group_ids"]
        campaign_ids = _as_str_list(campaign_ids)
        trafficstars_ids = _as_str_list(raw.get("trafficstars_campaign_ids"))

        if not keywords:
            raise ValueError(f"Rule '{name}' has no keywords.")
        if not campaign_ids and not trafficstars_ids:
            raise ValueError(
                f"Rule '{name}' has no campaign_ids (Reddit) and no "
                "trafficstars_campaign_ids — at least one is required."
            )
        for ts_id in trafficstars_ids:
            if not ts_id.isdigit():
                raise ValueError(
                    f"Rule '{name}': TrafficStars campaign ID {ts_id!r} is not "
                    "numeric. Find the ID in the admin.trafficstars.com "
                    "campaign list."
                )

        rules.append(
            Rule(
                name=name,
                keywords=keywords,
                campaign_ids=campaign_ids,
                trafficstars_campaign_ids=trafficstars_ids,
            )
        )

    return rules
