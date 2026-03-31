"""Rule dataclass and YAML loader for multi-rule ad control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import yaml


@dataclass
class Rule:
    """One monitoring rule: match any keyword → enable all listed ad groups."""

    name: str
    keywords: List[str]
    ad_group_ids: List[str]

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
            ad_group_ids:
              - adg_spark_123
          - name: "Home Assistant"
            keywords:
              - "home assistant"
              - homeassistant
            ad_group_ids:
              - adg_ha_456
              - adg_rpi_789

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
        ad_group_ids = raw.get("ad_group_ids", [])

        if not keywords:
            raise ValueError(f"Rule '{name}' has no keywords.")
        if not ad_group_ids:
            raise ValueError(f"Rule '{name}' has no ad_group_ids.")

        rules.append(Rule(name=name, keywords=keywords, ad_group_ids=ad_group_ids))

    return rules
