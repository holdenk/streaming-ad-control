"""Tests for stream_ad_monitor.rules."""

import textwrap
import pytest

from stream_ad_monitor.rules import Rule, load_rules_from_yaml


# ---------------------------------------------------------------------------
# Rule.matches_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, keywords, expected",
    [
        ("Spark deep dive", ["Spark"], True),
        ("spark deep dive", ["Spark"], True),  # case-insensitive
        ("SPARK all the things", ["Spark"], True),
        ("Home Assistant setup", ["home assistant"], True),
        ("Python and Pandas", ["Spark"], False),
        ("", ["Spark"], False),
        # Multiple keywords – any match is sufficient
        ("home assistant project", ["home assistant", "homeassistant"], True),
        ("homeassistant tips", ["home assistant", "homeassistant"], True),
        ("completely unrelated", ["home assistant", "homeassistant"], False),
    ],
)
def test_rule_matches_title(title, keywords, expected):
    rule = Rule(name="test", keywords=keywords, campaign_ids=["adg_1"])
    assert rule.matches_title(title) == expected


# ---------------------------------------------------------------------------
# load_rules_from_yaml
# ---------------------------------------------------------------------------


def test_load_rules_parses_minimal_yaml(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Spark"
                keywords:
                  - Spark
                campaign_ids:
                  - adg_spark_123
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert len(rules) == 1
    assert rules[0].name == "Spark"
    assert rules[0].keywords == ["Spark"]
    assert rules[0].campaign_ids == ["adg_spark_123"]


def test_load_rules_parses_multiple_rules(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Spark"
                keywords:
                  - Spark
                campaign_ids:
                  - adg_spark
              - name: "Home Assistant"
                keywords:
                  - home assistant
                  - homeassistant
                campaign_ids:
                  - adg_ha
                  - adg_rpi
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert len(rules) == 2

    assert rules[1].name == "Home Assistant"
    assert "home assistant" in rules[1].keywords
    assert "homeassistant" in rules[1].keywords
    assert "adg_ha" in rules[1].campaign_ids
    assert "adg_rpi" in rules[1].campaign_ids


def test_load_rules_raises_on_empty_rules_list(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text("rules: []\n")
    with pytest.raises(ValueError, match="No rules found"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_raises_on_missing_rules_key(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text("something_else: true\n")
    with pytest.raises(ValueError, match="No rules found"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_raises_when_rule_has_no_keywords(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad"
                keywords: []
                campaign_ids:
                  - adg_1
        """)
    )
    with pytest.raises(ValueError, match="no keywords"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_raises_when_rule_has_no_ad_groups(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad"
                keywords:
                  - Spark
                campaign_ids: []
        """)
    )
    with pytest.raises(ValueError, match="no campaign_ids"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_uses_index_as_name_when_missing(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - keywords:
                  - Spark
                campaign_ids:
                  - adg_1
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].name == "rule_0"


def test_load_rules_raises_on_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_rules_from_yaml("/nonexistent/path/rules.yaml")


# ---------------------------------------------------------------------------
# TrafficStars campaign support
# ---------------------------------------------------------------------------


def test_load_rules_parses_trafficstars_campaigns(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Both networks"
                keywords:
                  - Spark
                campaign_ids:
                  - reddit_camp_1
                trafficstars_campaign_ids:
                  - 123456
                  - "654321"
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].campaign_ids == ["reddit_camp_1"]
    assert rules[0].trafficstars_campaign_ids == ["123456", "654321"]


def test_load_rules_accepts_trafficstars_only_rule(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "TS only"
                keywords:
                  - Spark
                trafficstars_campaign_ids:
                  - 123456
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].campaign_ids == []
    assert rules[0].trafficstars_campaign_ids == ["123456"]


def test_load_rules_accepts_reddit_campaign_ids_alias(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Aliased"
                keywords:
                  - Spark
                reddit_campaign_ids:
                  - reddit_camp_1
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].campaign_ids == ["reddit_camp_1"]


def test_load_rules_coerces_numeric_ids_to_strings(tmp_path):
    """Unquoted numeric YAML IDs parse as ints; the loader must normalise."""
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Numeric"
                keywords:
                  - Spark
                campaign_ids:
                  - 2470329120103230906
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].campaign_ids == ["2470329120103230906"]


def test_load_rules_raises_on_non_numeric_trafficstars_id(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Bad TS"
                keywords:
                  - Spark
                trafficstars_campaign_ids:
                  - not_a_number
        """)
    )
    with pytest.raises(ValueError, match="not numeric"):
        load_rules_from_yaml(str(rules_yaml))


def test_rule_reddit_campaign_ids_property_aliases_campaign_ids():
    rule = Rule(name="r", keywords=["k"], campaign_ids=["c1"])
    assert rule.reddit_campaign_ids == ["c1"]
