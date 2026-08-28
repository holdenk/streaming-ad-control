"""stream_ad_monitor – watches a Twitch stream, toggles ad campaigns on
Reddit Ads and TrafficStars, and announces the stream on X, Bluesky, and
Mastodon."""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def mask_credential(value: str) -> str:
    """Return a masked version of a credential for safe logging."""
    if len(value) <= 8:
        return value[:1] + "***" + value[-1:] if len(value) >= 2 else "***"
    return value[:3] + "***" + value[-3:]


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def require_secure_url(url: str, setting_name: str) -> str:
    """Return *url* if credentials can safely be sent to it, else raise.

    The Bluesky PDS and Mastodon instance URLs are operator-supplied and
    carry app passwords and bearer tokens, so a typo'd ``http://`` would put
    them on the wire in the clear. Plain HTTP is allowed only for loopback,
    where there is no network to sniff — that is how you would point this at
    a PDS running on the same box.
    """
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        logger.warning(
            "%s uses plain http (%s). Allowed because it is loopback, but "
            "credentials would be sent in the clear to any other host.",
            setting_name,
            url,
        )
        return url
    raise ValueError(
        f"{setting_name} must be an https:// URL (got {url!r}). It carries "
        "account credentials, so plain http:// is refused except for localhost."
    )
