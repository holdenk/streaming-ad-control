"""stream_ad_monitor – monitors a Twitch stream and controls a Reddit ad."""


def mask_credential(value: str) -> str:
    """Return a masked version of a credential for safe logging."""
    if len(value) <= 8:
        return value[:1] + "***" + value[-1:] if len(value) >= 2 else "***"
    return value[:3] + "***" + value[-3:]
