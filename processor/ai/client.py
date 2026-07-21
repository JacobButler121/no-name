from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from processor.models import AnalysisConfigurationError, AnalysisError


class OpenAIResponsesClient:
    """Small Responses API client with no dependency on the OpenAI SDK."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("SPOTTED_OPENAI_MODEL", "gpt-5.6-luna")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(
            0,
            max_retries
            if max_retries is not None
            else int(os.getenv("SPOTTED_OPENAI_MAX_RETRIES", "4")),
        )

    def create_response(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.api_key.strip():
            raise AnalysisConfigurationError(
                "OPENAI_API_KEY is not configured. Spotted cannot analyze frames or search for products without a live OpenAI API key."
            )
        body = dict(payload)
        body.setdefault("model", self.model)
        encoded_body = json.dumps(body).encode("utf-8")
        decoded: Any = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                f"{self.base_url}/responses",
                data=encoded_body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Spotted-Hackathon/1.0",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                retryable = exc.code in {429, 500, 502, 503, 504}
                if retryable and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else 0.75 * (2**attempt)
                    except ValueError:
                        delay = 0.75 * (2**attempt)
                    time.sleep(min(max(delay, 0.1), 12.0))
                    continue
                raise AnalysisError(
                    f"OpenAI Responses API returned HTTP {exc.code}: {detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    time.sleep(min(0.75 * (2**attempt), 12.0))
                    continue
                raise AnalysisError(f"OpenAI Responses API request failed: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise AnalysisError(f"OpenAI Responses API request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise AnalysisError("OpenAI Responses API returned an unexpected payload")
        if decoded.get("error"):
            raise AnalysisError(f"OpenAI Responses API error: {decoded['error']}")
        return decoded

    @staticmethod
    def output_text(response: Mapping[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        chunks: list[str] = []
        for item in response.get("output", []):
            if not isinstance(item, Mapping):
                continue
            for content in item.get("content", []):
                if not isinstance(content, Mapping):
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                elif isinstance(text, Mapping) and isinstance(text.get("value"), str):
                    chunks.append(text["value"])
        if not chunks:
            raise AnalysisError("OpenAI response did not contain structured output text")
        return "".join(chunks)

    def create_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self.create_response(payload)
        try:
            parsed = json.loads(self.output_text(response))
        except json.JSONDecodeError as exc:
            raise AnalysisError("OpenAI response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise AnalysisError("OpenAI structured output must be a JSON object")
        return parsed
