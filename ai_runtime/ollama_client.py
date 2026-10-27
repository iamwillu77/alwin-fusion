"""Minimal Ollama HTTP client built on the Python standard library.

Design rules:

* uses ``urllib.request`` only (no third-party dependency),
* **never** calls ``/api/pull`` — models are never downloaded,
* raises precise exception types so the CLI can show a useful message
  when the local runtime is unavailable, times out, has no matching
  model or returns garbage,
* never silently falls back to any cloud service.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Callable, List, Optional

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 30.0


class OllamaError(Exception):
    """Base class for all local-runtime failures."""


class OllamaUnavailableError(OllamaError):
    """The Ollama server cannot be reached at all."""


class OllamaTimeoutError(OllamaError):
    """The Ollama server did not answer in time."""


class OllamaModelError(OllamaError):
    """The requested model is missing or not configured."""


class OllamaResponseError(OllamaError):
    """The Ollama server answered with something unusable."""


class OllamaClient:
    """Synchronous client for a local Ollama server."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        opener: Optional[Callable] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        if model is None:
            model = os.environ.get("OLLAMA_MODEL", "").strip()
        self.model = (model or "").strip()
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        self._opener = opener or urllib.request.urlopen

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    @staticmethod
    def _error_text(body: bytes, fallback: str) -> str:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                return payload["error"]
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return fallback

    @classmethod
    def _classify_http_error(cls, url: str, code: int, body: bytes) -> OllamaError:
        text = cls._error_text(body, f"HTTP {code} from {url}")
        lowered = text.lower()
        if code == 404 and ("model" in lowered or "not found" in lowered or "no such" in lowered):
            return OllamaModelError(text)
        if "model" in lowered and ("not found" in lowered or "no such" in lowered or "pull" in lowered):
            return OllamaModelError(text)
        return OllamaResponseError(text)

    def _request(
        self, method: str, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            with self._opener(request, timeout=effective_timeout) as response:
                body = response.read()
                status = getattr(response, "status", None) or getattr(response, "code", None)
                if isinstance(status, int) and status >= 400:
                    raise self._classify_http_error(url, status, body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            raise self._classify_http_error(url, exc.code, body) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise OllamaTimeoutError(
                    f"Local Ollama at {self.base_url} timed out after "
                    f"{effective_timeout:g}s."
                ) from None
            raise OllamaUnavailableError(
                f"Local AI runtime is not reachable at {self.base_url}: {reason}. "
                f"Start it with 'ollama serve' (or install Ollama). "
                f"Alwin Fusion only uses the local runtime."
            ) from None
        except (socket.timeout, TimeoutError):
            raise OllamaTimeoutError(
                f"Local Ollama at {self.base_url} timed out after "
                f"{effective_timeout:g}s."
            ) from None
        except OSError as exc:
            raise OllamaUnavailableError(
                f"Local AI runtime is not reachable at {self.base_url}: {exc}. "
                f"Start it with 'ollama serve'."
            ) from None

        if not body:
            raise OllamaResponseError(f"Empty response from {url}.")
        try:
            parsed = json.loads(body.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OllamaResponseError(
                f"Invalid JSON from {url}: {exc}."
            ) from None
        if not isinstance(parsed, dict):
            raise OllamaResponseError(
                f"Unexpected response from {url}: expected a JSON object."
            )
        return parsed

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def health(self, timeout: float = 3.0) -> dict:
        """GET /api/tags — raises a precise error when unavailable."""
        return self._request("GET", "/api/tags", timeout=timeout)

    def is_available(self, timeout: float = 3.0) -> bool:
        try:
            self.health(timeout=timeout)
            return True
        except OllamaError:
            return False

    def list_models(self, timeout: float = 3.0) -> List[str]:
        tags = self.health(timeout=timeout)
        models = tags.get("models")
        if not isinstance(models, list):
            raise OllamaResponseError("Ollama /api/tags returned no 'models' list.")
        names = []
        for entry in models:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
        return sorted(names)

    def _require_model(self) -> None:
        if not self.model:
            raise OllamaModelError(
                "OLLAMA_MODEL is not set. Choose a model you already have "
                "(run 'ollama list' to see installed models). Alwin Fusion "
                "never downloads models automatically."
            )

    def model_installed(self) -> bool:
        """True when the configured model matches an installed model tag."""
        installed = self.list_models()
        for name in installed:
            if name == self.model or name.split(":")[0] == self.model:
                return True
        return False

    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        timeout: Optional[float] = None,
    ) -> str:
        """Run one non-streaming generation against the local model.

        The configured model is verified against the installed model
        list first so the user gets an actionable error instead of a
        silent surprise.  Nothing is ever downloaded.
        """
        self._require_model()
        installed = self.list_models()
        if not any(
            name == self.model or name.split(":")[0] == self.model for name in installed
        ):
            available = ", ".join(installed) if installed else "(none installed)"
            raise OllamaModelError(
                f"Model '{self.model}' is not installed in Ollama. "
                f"Installed models: {available}. Install it yourself with "
                f"'ollama pull {self.model}' if you want it — Alwin Fusion "
                f"will not download models."
            )

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        response = self._request("POST", "/api/generate", payload, timeout=timeout)
        text = response.get("response")
        if not isinstance(text, str):
            raise OllamaResponseError(
                "Ollama /api/generate returned no 'response' text."
            )
        return text
