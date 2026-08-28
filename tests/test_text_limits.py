"""Tests for stream_ad_monitor.text_limits."""

import pytest

from stream_ad_monitor.text_limits import (
    URL_WEIGHT,
    char_weight,
    truncate_weighted,
    weighted_length,
)


# ---------------------------------------------------------------------------
# weighted_length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", 0),
        ("hello", 5),
        ("Live now: Spark", 15),
        ("観", 2),  # CJK weighs 2
        ("🔴", 2),  # so does an emoji
        ("🔴 Live", 7),
    ],
)
def test_weighted_length(text, expected):
    assert weighted_length(text) == expected


def test_a_url_costs_its_t_co_length_however_long_it_is():
    short = "https://x.co/a"
    long = "https://example.com/" + "a" * 200
    assert weighted_length(short) == weighted_length(long) == URL_WEIGHT


def test_the_default_announcement_overhead():
    """Prefix plus both links, so the title budget can be reasoned about."""
    text = (
        "🔴 Live now: \n\nhttps://twitch.tv/holden\n"
        "https://www.youtube.com/watch?v=abc12345678"
    )
    assert weighted_length(text) == 62


@pytest.mark.parametrize("char, weight", [("a", 1), ("~", 1), ("観", 2), ("🔴", 2)])
def test_char_weight(char, weight):
    assert char_weight(char) == weight


# ---------------------------------------------------------------------------
# truncate_weighted
# ---------------------------------------------------------------------------


def test_text_within_the_limit_is_untouched():
    assert truncate_weighted("hello", 280) == "hello"


def test_truncation_respects_the_weighted_limit():
    out = truncate_weighted("観" * 200, 280)
    assert weighted_length(out) <= 280
    assert out.endswith("…")


def test_the_ellipsis_is_paid_for():
    """The ellipsis itself weighs 2, so it has to come out of the budget."""
    out = truncate_weighted("x" * 100, 20)
    assert weighted_length(out) == 20
    assert out == "x" * 18 + "…"


def test_truncation_stops_before_a_url_rather_than_cutting_it():
    url = "https://example.com/watch"
    out = truncate_weighted("x" * 260 + " " + url, 280)
    assert "https://" not in out or out.endswith(url)


def test_a_zero_limit_yields_nothing():
    """Callers that mean "don't truncate" say so themselves."""
    assert truncate_weighted("hello", 0) == ""


def test_a_negative_limit_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        truncate_weighted("hello", -1)


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 10])
def test_output_never_exceeds_the_limit(limit):
    """Including limits too small to fit the ellipsis itself."""
    assert weighted_length(truncate_weighted("hello world", limit)) <= limit


def test_a_limit_below_the_ellipsis_weight_yields_nothing():
    """The ellipsis weighs 2; emitting it at limit=1 would break the contract."""
    assert truncate_weighted("hello", 1) == ""
