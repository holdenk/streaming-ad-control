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
    rule = Rule(name="test", keywords=keywords, ad_group_ids=["adg_1"])
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
                ad_group_ids:
                  - adg_spark_123
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert len(rules) == 1
    assert rules[0].name == "Spark"
    assert rules[0].keywords == ["Spark"]
    assert rules[0].ad_group_ids == ["adg_spark_123"]


def test_load_rules_parses_multiple_rules(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - name: "Spark"
                keywords:
                  - Spark
                ad_group_ids:
                  - adg_spark
              - name: "Home Assistant"
                keywords:
                  - home assistant
                  - homeassistant
                ad_group_ids:
                  - adg_ha
                  - adg_rpi
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert len(rules) == 2

    assert rules[1].name == "Home Assistant"
    assert "home assistant" in rules[1].keywords
    assert "homeassistant" in rules[1].keywords
    assert "adg_ha" in rules[1].ad_group_ids
    assert "adg_rpi" in rules[1].ad_group_ids


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
                ad_group_ids:
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
                ad_group_ids: []
        """)
    )
    with pytest.raises(ValueError, match="no ad_group_ids"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_uses_index_as_name_when_missing(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent("""
            rules:
              - keywords:
                  - Spark
                ad_group_ids:
                  - adg_1
        """)
    )
    rules = load_rules_from_yaml(str(rules_yaml))
    assert rules[0].name == "rule_0"


def test_load_rules_raises_on_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_rules_from_yaml("/nonexistent/path/rules.yaml")
