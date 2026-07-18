from __future__ import annotations

import json
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

    SerpApi requires a publicly reachable image URL. Spotted therefore exposes
    only the selected temporary crop through the existing expiring job route.
    When either credential or public base URL is absent, the client is disabled
    and the OpenAI-only search path continues unchanged.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        public_base_url: str | None = None,
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
        self.country = country or os.getenv("SPOTTED_LENS_COUNTRY", "us")
        self.language = language or os.getenv("SPOTTED_LENS_LANGUAGE", "en")
        self.timeout = timeout
        self.opener = opener

    @property
    def enabled(self) -> bool:
        parsed = urllib.parse.urlparse(self.public_base_url)
        return bool(
            self.api_key.strip()
            and parsed.scheme == "https"
            and parsed.netloc
        )

    def public_crop_url(self, thumbnail_url: str | None, crop_name: str) -> str | None:
        if not self.enabled or not thumbnail_url:
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
        if not self.enabled or not thumbnail_url:
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
            "type": "visual_matches",
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
