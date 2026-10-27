"""Alwin Fusion runtime integrations (local AI runtime, later: model
management, desktop automation backends, ...)."""

from ai_runtime.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaModelError,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaUnavailableError,
)

__all__ = [
    "OllamaClient",
    "OllamaError",
    "OllamaModelError",
    "OllamaResponseError",
    "OllamaTimeoutError",
    "OllamaUnavailableError",
]
