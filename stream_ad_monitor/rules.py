"""Rule dataclass and YAML loader for multi-rule ad control."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Rule:
    """One monitoring rule: match any keyword → enable all listed campaigns."""

    name: str
    keywords: List[str]
    campaign_ids: List[str]

    def matches_title(self, title: str) -> bool:
        """Return True when any keyword appears in *title* (case-insensitive)."""
        title_lower = title.lower()
        return any(kw.lower() in title_lower for kw in self.keywords)


def load_rules_from_yaml(path: str) -> List[Rule]:
    """Parse *path* as YAML and return a list of :class:`Rule` objects.

    Expected YAML structure::

        rules:
          - name: "Spark streams"
            keywords:
              - Spark
            campaign_ids:
              - 2470329120103230906
          - name: "Home Assistant"
            keywords:
              - "home assistant"
              - homeassistant
            campaign_ids:
              - 2470329120103231000
              - 2470329120103231001

    The legacy field name ``ad_group_ids`` is still accepted (with a
    deprecation warning) — the value is treated as a list of campaign IDs.

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
        if campaign_ids is None and "ad_group_ids" in raw:
            logger.warning(
                "Rule '%s' uses deprecated 'ad_group_ids'; rename to "
                "'campaign_ids' (the value is treated as a campaign ID list).",
                name,
            )
            campaign_ids = raw["ad_group_ids"]
        if campaign_ids is None:
            campaign_ids = []

        if not keywords:
            raise ValueError(f"Rule '{name}' has no keywords.")
        if not campaign_ids:
            raise ValueError(f"Rule '{name}' has no campaign_ids.")

        rules.append(Rule(name=name, keywords=keywords, campaign_ids=campaign_ids))

    return rules
