from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class LensDiagnostic:
    """A secret-safe description of one Lens retrieval stage."""

    stage: str
    status: str
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class LensSearchOutcome:
    """Candidates plus diagnostics; never contains request URLs or credentials."""

    candidates: tuple[LensCandidate, ...] = ()
    diagnostics: tuple[LensDiagnostic, ...] = field(default_factory=tuple)

    @property
    def status(self) -> str:
        if any(item.status == "error" for item in self.diagnostics):
            return "error"
        if self.candidates:
            return "success"
        if any(item.status == "disabled" for item in self.diagnostics):
            return "disabled"
        return "empty"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidateCount": len(self.candidates),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _diagnostic(
    stage: str,
    status: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> LensDiagnostic:
    return LensDiagnostic(stage, status, code, message, retryable)


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
        """Compatibility wrapper returning candidates only."""
        return list(self.search_with_diagnostics(image_url, query=query, limit=limit).candidates)

    def search_with_diagnostics(
        self,
        image_url: str,
        *,
        query: str | None = None,
        limit: int = 8,
    ) -> LensSearchOutcome:
        if not self.api_key.strip():
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_configuration", "disabled", "missing_api_key",
                "Google Lens retrieval is not configured.",
            ),))
        if not (self.relay_enabled or self.public_base_enabled):
            code = "missing_relay_token" if self.relay_url and not self.relay_token.strip() else "missing_public_transport"
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_configuration", "disabled", code,
                "No secure public image transport is configured for Google Lens.",
            ),))
        if not image_url.startswith("https://"):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "invalid_image_url",
                "Google Lens requires a public HTTPS image URL.",
            ),))
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "invalid_json",
                "Google Lens returned an unreadable response.", retryable=True,
            ),))
        except urllib.error.HTTPError as exc:
            unauthorized = exc.code in {401, 403}
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error",
                "lens_unauthorized" if unauthorized else "lens_http_error",
                (
                    "Google Lens authorization was rejected."
                    if unauthorized
                    else "Google Lens rejected the HTTP request."
                ),
                retryable=exc.code >= 500,
            ),))
        except (OSError, TimeoutError, urllib.error.URLError):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "network_error",
                "Google Lens could not be reached.", retryable=True,
            ),))
        if not isinstance(payload, Mapping):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "invalid_response",
                "Google Lens returned an unexpected response.", retryable=True,
            ),))
        if payload.get("error"):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "api_error",
                "Google Lens rejected the search request.", retryable=True,
            ),))
        raw_matches = payload.get("visual_matches")
        if raw_matches is None:
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "empty", "no_visual_matches",
                "Google Lens returned no visual matches.",
            ),))
        if not isinstance(raw_matches, list):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_request", "error", "invalid_matches",
                "Google Lens returned malformed visual matches.", retryable=True,
            ),))

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
        status = "success" if candidates else "empty"
        code = "candidates_returned" if candidates else "no_usable_candidates"
        message = (
            "Google Lens returned usable visual candidates."
            if candidates
            else "Google Lens results contained no usable public product links."
        )
        return LensSearchOutcome(
            candidates=tuple(candidates),
            diagnostics=(_diagnostic("lens_request", status, code, message),),
        )

    def search_crop(
        self,
        image_path: str | os.PathLike[str],
        *,
        query: str | None = None,
        limit: int = 8,
    ) -> list[LensCandidate]:
        """Compatibility wrapper returning candidates only."""
        return list(self.search_crop_with_diagnostics(
            image_path, query=query, limit=limit
        ).candidates)

    def search_crop_with_diagnostics(
        self,
        image_path: str | os.PathLike[str],
        *,
        query: str | None = None,
        limit: int = 8,
    ) -> LensSearchOutcome:
        """Search one local crop through the relay and report every failure stage."""
        if not self.api_key.strip():
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "lens_configuration", "disabled", "missing_api_key",
                "Google Lens retrieval is not configured.",
            ),))
        if not self.relay_enabled:
            code = "missing_relay_token" if self.relay_url and not self.relay_token.strip() else "missing_relay"
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "disabled", code,
                "The public crop relay is not configured.",
            ),))
        try:
            with open(image_path, "rb") as image_file:
                image_bytes = image_file.read(5 * 1024 * 1024 + 1)
        except OSError:
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "crop_read_failed",
                "The selected crop could not be read.",
            ),))
        if not image_bytes:
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "empty_crop",
                "The selected crop is empty.",
            ),))
        if len(image_bytes) > 5 * 1024 * 1024:
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "crop_too_large",
                "The selected crop exceeds the relay size limit.",
            ),))

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
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "invalid_relay_json",
                "The crop relay returned an unreadable response.", retryable=True,
            ),))
        except urllib.error.HTTPError as exc:
            unauthorized = exc.code in {401, 403}
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error",
                "relay_unauthorized" if unauthorized else "relay_http_error",
                (
                    "The crop relay rejected its authorization token."
                    if unauthorized
                    else "The crop relay rejected the upload request."
                ),
                retryable=exc.code >= 500,
            ),))
        except (OSError, TimeoutError, urllib.error.URLError):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "relay_network_error",
                "The crop could not be uploaded to the public relay.", retryable=True,
            ),))
        if not isinstance(payload, Mapping):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "invalid_relay_response",
                "The crop relay returned an unexpected response.", retryable=True,
            ),))
        image_url = str(payload.get("url") or "").strip()
        delete_url = str(payload.get("deleteUrl") or image_url).strip()
        if not image_url.startswith("https://"):
            return LensSearchOutcome(diagnostics=(_diagnostic(
                "relay_upload", "error", "missing_relay_url",
                "The crop relay did not return a public HTTPS URL.", retryable=True,
            ),))
        diagnostics = [_diagnostic(
            "relay_upload", "success", "crop_uploaded",
            "The crop was uploaded to the public relay.",
        )]
        try:
            outcome = self.search_with_diagnostics(image_url, query=query, limit=limit)
            diagnostics.extend(outcome.diagnostics)
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
                    diagnostics.append(_diagnostic(
                        "relay_cleanup", "warning", "relay_cleanup_failed",
                        "The temporary relay crop could not be deleted immediately.",
                        retryable=True,
                    ))
        return LensSearchOutcome(
            candidates=outcome.candidates,
            diagnostics=tuple(diagnostics),
        )
