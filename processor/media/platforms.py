from __future__ import annotations

from urllib.parse import urlparse


PLATFORM_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com",),
}


def _matches(hostname: str, allowed: tuple[str, ...]) -> bool:
    return any(hostname == host or hostname.endswith(f".{host}") for host in allowed)


def classify_url(value: str) -> str:
    """Validate a supported public social URL and return its platform."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete public http(s) video URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not supported.")
    hostname = parsed.hostname.lower().rstrip(".")
    for platform, hosts in PLATFORM_HOSTS.items():
        if _matches(hostname, hosts):
            return platform
    raise ValueError("Spotted currently supports YouTube, TikTok, and Instagram URLs.")
