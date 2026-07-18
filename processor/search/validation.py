from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urlparse


@dataclass(frozen=True)
class ProductPageMetadata:
    url: str
    title: str
    image_url: str | None = None
    price: str | None = None


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_chunks: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_chunks.append(data)


def _is_public_https(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            return False
    return True


def _meaningful_tokens(values: Iterable[str]) -> set[str]:
    noise = {"the", "and", "for", "with", "product", "item", "official", "shop", "buy"}
    return {
        token
        for value in values
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in noise
    }


def validate_product_url(
    url: str,
    *,
    expected_terms: Iterable[str] = (),
    timeout: float = 8.0,
    max_bytes: int = 512_000,
) -> ProductPageMetadata | None:
    """Fetch minimal page metadata and reject unsafe/unrelated destinations.

    Search-result URLs never reach the UI unless they resolve to a public HTTPS
    page with a title related to the detected product. Failure is represented as
    ``None`` so a blocked retailer cannot break the entire analysis job.
    """
    if not _is_public_https(url):
        return None
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SpottedProductVerifier/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            content_type = response.headers.get_content_type()
            if not _is_public_https(final_url) or content_type not in {"text/html", "application/xhtml+xml"}:
                return None
            raw = response.read(max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    parser = _MetadataParser()
    try:
        parser.feed(raw.decode(charset, errors="replace"))
    except (LookupError, ValueError):
        parser.feed(raw.decode("utf-8", errors="replace"))
    title = (parser.meta.get("og:title") or " ".join(parser.title_chunks)).strip()
    if not title:
        return None
    expected = _meaningful_tokens(expected_terms)
    observed = _meaningful_tokens((title, urlparse(final_url).path))
    if expected and not expected.intersection(observed):
        return None
    image = parser.meta.get("og:image") or parser.meta.get("twitter:image")
    image_url = urljoin(final_url, image) if image else None
    if image_url and urlparse(image_url).scheme not in {"https", "http"}:
        image_url = None
    amount = parser.meta.get("product:price:amount")
    currency = parser.meta.get("product:price:currency")
    price = f"{amount} {currency}".strip() if amount else None
    return ProductPageMetadata(url=final_url, title=title, image_url=image_url, price=price)
