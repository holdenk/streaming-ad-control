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


def raise_on_redirect(response, what: str) -> None:
    """Refuse a redirect on a request that carried credentials.

    Redirects are disabled on those requests rather than followed, because a
    same-host ``https``→``http`` 307 or 308 replays the request *body* — and
    a Bluesky login carries its app password there, not in a header, so
    requests' cross-host ``Authorization`` stripping does not help. A 3xx is
    not an error status, so it has to be rejected explicitly or it would sail
    past ``raise_for_status``.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("Location", "<none>")
        raise RuntimeError(
            f"{what} was redirected to {location!r}; the redirect was not "
            "followed because this request carries credentials. Point the "
            "configured URL straight at the API host."
        )


def _is_loopback_http(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


def guard_loopback_session(session, url: str) -> None:
    """Keep proxy environment variables from intercepting loopback traffic.

    :func:`require_secure_url` permits plain http for loopback on the
    reasoning that there is no network to sniff. Proxy environment variables
    break that reasoning: with ``HTTP_PROXY`` or ``ALL_PROXY`` set and
    ``NO_PROXY`` not covering localhost, requests routes the call — and the
    credentials it carries — to the proxy instead. Turning off ``trust_env``
    for exactly those sessions restores it, while https endpoints keep normal
    proxy behaviour (which a deployment behind a corporate proxy needs).
    """
    if _is_loopback_http(url):
        logger.debug("Ignoring proxy environment for loopback endpoint %s.", url)
        session.trust_env = False
