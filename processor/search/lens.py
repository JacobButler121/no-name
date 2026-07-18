from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping


UrlOpener = Callable[..., Any]


@dataclass(frozen=True)
class LensCandidate:
    """A visual candidate returned by Google Lens through SerpApi."""

    title: str
    link: str
    source: str
    image_url: str | None = None
    price: str | None = None
    exact_hint: bool = False

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "imageUrl": self.image_url,
            "price": self.price,
            "lensExactHint": self.exact_hint,
        }


class GoogleLensSearchClient:
    """Optional Google Lens candidate lookup backed by SerpApi.

    SerpApi requires a publicly reachable image URL. The preferred path uploads
    one selected crop to Spotted's short-lived Sites relay, searches it, and
    deletes it in a finally block. A tunneled processor origin remains available
    as a legacy fallback. When neither transport is configured, the OpenAI-only
    search path continues unchanged.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        public_base_url: str | None = None,
        relay_url: str | None = None,
        relay_token: str | None = None,
        country: str | None = None,
        language: str | None = None,
        timeout: float = 25.0,
        opener: UrlOpener = urllib.request.urlopen,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("SERPAPI_API_KEY", "")
        self.public_base_url = (
            public_base_url
            if public_base_url is not None
            else os.getenv("SPOTTED_PUBLIC_BASE_URL", "")
        ).rstrip("/")
        self.relay_url = (
            relay_url
            if relay_url is not None
            else os.getenv("SPOTTED_IMAGE_RELAY_URL", "")
        ).rstrip("/")
        self.relay_token = (
            relay_token
            if relay_token is not None
            else os.getenv("SPOTTED_RELAY_TOKEN", "")
        )
        self.country = country or os.getenv("SPOTTED_LENS_COUNTRY", "us")
        self.language = language or os.getenv("SPOTTED_LENS_LANGUAGE", "en")
        self.timeout = timeout
        self.opener = opener

    @property
    def public_base_enabled(self) -> bool:
        parsed = urllib.parse.urlparse(self.public_base_url)
        return bool(
            parsed.scheme == "https"
            and parsed.netloc
        )

    @property
    def relay_enabled(self) -> bool:
        parsed = urllib.parse.urlparse(self.relay_url)
        return bool(
            parsed.scheme == "https"
            and parsed.netloc
            and self.relay_token.strip()
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_key.strip()
            and (self.relay_enabled or self.public_base_enabled)
        )

    def public_crop_url(self, thumbnail_url: str | None, crop_name: str) -> str | None:
        if not self.public_base_enabled or not thumbnail_url:
            return None
        match = urllib.parse.urlparse(thumbnail_url).path.split("/")
        try:
            jobs_index = match.index("jobs")
            job_id = match[jobs_index + 1]
        except (ValueError, IndexError):
            return None
        if not job_id or not crop_name:
            return None
        return (
            f"{self.public_base_url}/api/jobs/"
            f"{urllib.parse.quote(job_id, safe='')}/crops/"
            f"{urllib.parse.quote(crop_name, safe='')}"
        )

    def public_frame_url(self, thumbnail_url: str | None) -> str | None:
        if not self.public_base_enabled or not thumbnail_url:
            return None
        path = urllib.parse.urlparse(thumbnail_url).path
        if not path.startswith("/api/jobs/") or "/frames/" not in path:
            return None
        return f"{self.public_base_url}{path}"

    def search(
        self,
        image_url: str,
        *,
        query: str | None = None,
        limit: int = 8,
    ) -> list[LensCandidate]:
        if not self.enabled or not image_url.startswith("https://"):
            return []
        params = {
            "engine": "google_lens",
            # Product results prioritize purchasable listings while retaining
            # the same visual_matches response shape used by SerpApi.
            "type": "products",
            "url": image_url,
            "api_key": self.api_key,
            "country": self.country,
            "hl": self.language,
            "safe": "active",
            "auto_crop": "false",
            "no_cache": "false",
            "output": "json",
        }
        if query:
            params["q"] = query[:300]
        endpoint = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            endpoint,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "Spotted-Hackathon/1.0"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            return []
        if not isinstance(payload, Mapping) or payload.get("error"):
            return []
        raw_matches = payload.get("visual_matches")
        if not isinstance(raw_matches, list):
            return []

        candidates: list[LensCandidate] = []
        seen_links: set[str] = set()
        for raw in raw_matches:
            if not isinstance(raw, Mapping):
                continue
            link = str(raw.get("link") or "").strip()
            parsed = urllib.parse.urlparse(link)
            if parsed.scheme != "https" or not parsed.netloc or link in seen_links:
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            seen_links.add(link)
            raw_price = raw.get("price")
            price = (
                str(raw_price.get("value") or "").strip()
                if isinstance(raw_price, Mapping)
                else None
            )
            candidates.append(
                LensCandidate(
                    title=title,
                    link=link,
                    source=str(raw.get("source") or "Google Lens").strip(),
                    image_url=str(raw.get("image") or raw.get("thumbnail") or "").strip() or None,
                    price=price or None,
                    exact_hint=bool(raw.get("exact_matches")),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    def search_crop(
        self,
        image_path: str | os.PathLike[str],
        *,
        query: str | None = None,
        limit: int = 8,
    ) -> list[LensCandidate]:
        """Search one local crop through the relay and always request deletion."""
        if not self.enabled or not self.relay_enabled:
            return []
        try:
            with open(image_path, "rb") as image_file:
                image_bytes = image_file.read(5 * 1024 * 1024 + 1)
        except OSError:
            return []
        if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
            return []

        content_type = mimetypes.guess_type(os.fspath(image_path))[0] or "image/jpeg"
        upload = urllib.request.Request(
            self.relay_url,
            data=image_bytes,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.relay_token}",
                "Content-Type": content_type,
                "User-Agent": "Spotted-Hackathon/1.0",
            },
        )
        try:
            with self.opener(upload, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            return []
        if not isinstance(payload, Mapping):
            return []
        image_url = str(payload.get("url") or "").strip()
        delete_url = str(payload.get("deleteUrl") or image_url).strip()
        if not image_url.startswith("https://"):
            return []
        try:
            return self.search(image_url, query=query, limit=limit)
        finally:
            if delete_url.startswith("https://"):
                deletion = urllib.request.Request(
                    delete_url,
                    method="DELETE",
                    headers={
                        "Authorization": f"Bearer {self.relay_token}",
                        "User-Agent": "Spotted-Hackathon/1.0",
                    },
                )
                try:
                    with self.opener(deletion, timeout=min(self.timeout, 10.0)):
                        pass
                except (OSError, TimeoutError, urllib.error.URLError):
                    pass
