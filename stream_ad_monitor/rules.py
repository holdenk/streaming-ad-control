"""Rule dataclass and YAML loader for multi-rule, multi-network ad control."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

from .networks import NETWORKS

logger = logging.getLogger(__name__)

MATCH_MODES = ("word", "substring")


def keyword_in_title(title: str, keyword: str, match: str = "word") -> bool:
    """Return True when *keyword* appears in *title* (case-insensitive).

    ``word`` (the default) requires the keyword to stand on its own, so
    "Spark" matches "Apache Spark" and "#Spark" but not "sparkle" or
    "SparkFun". ``substring`` is the old behaviour: any occurrence counts.
    """
    if not keyword:
        return False
    if match == "substring":
        return keyword.lower() in title.lower()
    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
    return re.search(pattern, title, re.IGNORECASE) is not None


@dataclass
class Rule:
    """One monitoring rule: match any keyword → enable all listed campaigns.

    ``campaign_ids`` are Reddit Ads campaigns (kept under the historical
    name for backward compatibility — ``reddit_campaign_ids`` in YAML maps
    here too). The other ``*_campaign_ids`` fields hold campaigns on the
    REST networks listed in :mod:`stream_ad_monitor.networks`. A rule may
    target any combination of networks.

    ``exclude_keywords`` veto a match: a title containing any of them never
    enables this rule's campaigns. ``match`` is ``word`` (default) or
    ``substring`` and applies to both lists.
    """

    name: str
    keywords: List[str]
    campaign_ids: List[str] = field(default_factory=list)
    trafficstars_campaign_ids: List[str] = field(default_factory=list)
    exoclick_campaign_ids: List[str] = field(default_factory=list)
    propellerads_campaign_ids: List[str] = field(default_factory=list)
    meta_campaign_ids: List[str] = field(default_factory=list)
    exclude_keywords: List[str] = field(default_factory=list)
    match: str = "word"

    @property
    def reddit_campaign_ids(self) -> List[str]:
        """Clearer alias for :attr:`campaign_ids` now that there are several networks."""
        return self.campaign_ids

    def campaigns_by_network(self) -> Dict[str, List[str]]:
        """Non-empty campaign ID lists keyed by network key."""
        result = {}
        for network in NETWORKS:
            ids = getattr(self, network.rule_attr)
            if ids:
                result[network.key] = ids
        return result

    def campaign_count(self) -> int:
        return sum(len(ids) for ids in self.campaigns_by_network().values())

    def matches_title(self, title: str) -> bool:
        """True when any keyword matches and no exclude keyword does."""
        if not any(keyword_in_title(title, kw, self.match) for kw in self.keywords):
            return False
        return not any(
            keyword_in_title(title, kw, self.match) for kw in self.exclude_keywords
        )


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
          - name: "Spark dev"
            keywords: [Spark, PySpark]
            exclude_keywords: [FHI]     # optional veto
            match: word                 # optional: word (default) | substring
            campaign_ids:               # Reddit Ads campaigns
              - 2470329120103230906
            trafficstars_campaign_ids:  # TrafficStars
              - 123456
            exoclick_campaign_ids: [...]
            propellerads_campaign_ids: [...]
            meta_campaign_ids: [...]

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
        keywords = _as_str_list(raw.get("keywords"))
        if not keywords:
            raise ValueError(f"Rule '{name}' has no keywords.")

        match = str(raw.get("match", "word")).strip().lower()
        if match not in MATCH_MODES:
            raise ValueError(
                f"Rule '{name}': match must be one of {MATCH_MODES}, got {match!r}."
            )

        ids_by_attr: Dict[str, List[str]] = {}
        for network in NETWORKS:
            value = None
            for key in network.yaml_keys:
                if raw.get(key) is not None:
                    value = raw[key]
                    break
            if network.key == "reddit" and value is None and "ad_group_ids" in raw:
                logger.warning(
                    "Rule '%s' uses deprecated 'ad_group_ids'; rename to "
                    "'campaign_ids' (the value is treated as a campaign ID list).",
                    name,
                )
                value = raw["ad_group_ids"]
            ids = _as_str_list(value)
            if network.numeric_ids:
                for cid in ids:
                    if not cid.isdigit():
                        raise ValueError(
                            f"Rule '{name}': {network.label} campaign ID {cid!r} "
                            f"is not numeric. Find the ID in {network.id_hint}."
                        )
            ids_by_attr[network.rule_attr] = ids

        if not any(ids_by_attr.values()):
            others = ", ".join(n.yaml_keys[0] for n in NETWORKS[1:])
            raise ValueError(
                f"Rule '{name}' has no campaign_ids (Reddit) and none of "
                f"{others} — at least one is required."
            )

        rules.append(
            Rule(
                name=name,
                keywords=keywords,
                exclude_keywords=_as_str_list(raw.get("exclude_keywords")),
                match=match,
                **ids_by_attr,
            )
        )

    return rules
