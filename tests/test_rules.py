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


@pytest.mark.parametrize(
    "contents, message",
    [
        ("- not-a-mapping\n", "YAML mapping"),
        ("rules: not-a-list\n", "must be a list"),
        ("rules: [not-a-mapping]\n", "index 0"),
    ],
)
def test_load_rules_reports_malformed_structure(tmp_path, contents, message):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(contents)
    with pytest.raises(ValueError, match=message):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_rejects_blank_keywords_and_campaign_ids(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        "rules:\n  - name: blank\n    keywords: ['  ']\n    campaign_ids: ['  ']\n"
    )
    with pytest.raises(ValueError, match="no keywords"):
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


# ---------------------------------------------------------------------------
# Whole-word matching, exclude keywords, and new networks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Apache Spark internals", True),
        ("#Spark live coding", True),
        ("Spark's new shuffle", True),
        ("Sparkle unicorn stream", False),
        ("SparkFun soldering", False),
    ],
)
def test_word_matching_is_the_default(title, expected):
    rule = Rule(name="spark", keywords=["Spark"], campaign_ids=["c"])
    assert rule.matches_title(title) is expected


def test_substring_mode_keeps_the_old_behaviour():
    rule = Rule(name="spark", keywords=["Spark"], campaign_ids=["c"], match="substring")
    assert rule.matches_title("SparkSQL deep dive") is True


def test_multi_word_keyword_matches_as_a_phrase():
    rule = Rule(name="fhi", keywords=["Fight Health Insurance"], campaign_ids=["c"])
    assert rule.matches_title("Working on fight health insurance appeals")
    assert not rule.matches_title("fight the health insurance company")


def test_exclude_keywords_veto_a_match():
    rule = Rule(
        name="spark", keywords=["Spark"], exclude_keywords=["FHI"], campaign_ids=["c"]
    )
    assert rule.matches_title("Spark dev: new connector")
    assert not rule.matches_title("Fixing FHI's Spark pipeline")


def test_load_rules_reads_exclude_match_and_new_networks(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        textwrap.dedent(
            """\
            rules:
              - name: FHI
                keywords: [FHI, "Fight Health Insurance"]
                exclude_keywords: Spark
                match: substring
                meta_campaign_ids: [120200000000001]
                exoclick_campaign_ids: 11
                propellerads_campaign_ids: ["22"]
            """
        )
    )
    (rule,) = load_rules_from_yaml(str(rules_yaml))
    assert rule.exclude_keywords == ["Spark"]
    assert rule.match == "substring"
    assert rule.campaigns_by_network() == {
        "exoclick": ["11"],
        "propellerads": ["22"],
        "meta": ["120200000000001"],
    }
    assert rule.campaign_count() == 3


def test_load_rules_rejects_non_numeric_ids_on_rest_networks(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        "rules:\n  - name: x\n    keywords: [a]\n    meta_campaign_ids: [abc]\n"
    )
    with pytest.raises(ValueError, match="Meta"):
        load_rules_from_yaml(str(rules_yaml))


def test_load_rules_rejects_unknown_match_mode(tmp_path):
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(
        "rules:\n  - name: x\n    keywords: [a]\n    match: fuzzy\n    campaign_ids: [c]\n"
    )
    with pytest.raises(ValueError, match="match"):
        load_rules_from_yaml(str(rules_yaml))
