"""Tests for the local Ollama runtime client.

Covers: server unavailable, timeouts, missing/unconfigured models,
invalid responses, HTTP error mapping — and the guarantee that the
client never requests a model download (no /api/pull, ever).
"""

from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error

from ai_runtime.ollama_client import (
    OllamaClient,
    OllamaModelError,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaUnavailableError,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._body = payload
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(routes):
    """Build a fake urlopen.  routes: {url_suffix: payload|exception}."""
    calls = []

    def opener(request, timeout=None):
        url = request.full_url
        calls.append(
            {
                "url": url,
                "method": request.get_method(),
                "timeout": timeout,
                "data": request.data,
            }
        )
        for suffix, handler in routes.items():
            if url.endswith(suffix):
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    return handler(request)
                return FakeResponse(handler)
        raise AssertionError(f"unexpected URL requested: {url}")

    opener.calls = calls
    return opener


def http_error(url, code, payload):
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(url, code, "Error", {}, io.BytesIO(body))


TAGS_OK = {"models": [{"name": "qwen3:0.6b"}, {"name": "other:1"}]}


class TestAvailability(unittest.TestCase):
    def test_connection_refused_is_unavailable_with_helpful_message(self):
        opener = make_opener(
            {"/api/tags": urllib.error.URLError(ConnectionRefusedError(111, "refused"))}
        )
        client = OllamaClient(
            base_url="http://localhost:11434", model="qwen3:0.6b", opener=opener
        )
        with self.assertRaises(OllamaUnavailableError) as ctx:
            client.list_models()
        message = str(ctx.exception)
        self.assertIn("http://localhost:11434", message)
        self.assertIn("ollama serve", message)

    def test_timeout_is_classified(self):
        opener = make_opener(
            {"/api/tags": urllib.error.URLError(socket.timeout("timed out"))}
        )
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaTimeoutError):
            client.list_models()

    def test_direct_socket_timeout_is_classified(self):
        opener = make_opener({"/api/tags": socket.timeout("slow")})
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaTimeoutError):
            client.list_models()

    def test_is_available_returns_bool_without_raising(self):
        down = make_opener(
            {"/api/tags": urllib.error.URLError(ConnectionRefusedError(111, "down"))}
        )
        up = make_opener({"/api/tags": TAGS_OK})
        self.assertFalse(OllamaClient(opener=down).is_available())
        self.assertTrue(OllamaClient(opener=up).is_available())

    def test_list_models_parses_names(self):
        opener = make_opener({"/api/tags": TAGS_OK})
        client = OllamaClient(opener=opener, model="qwen3:0.6b")
        self.assertEqual(client.list_models(), ["other:1", "qwen3:0.6b"])


class TestModels(unittest.TestCase):
    def test_no_model_configured_fails_before_any_request(self):
        opener = make_opener({})
        client = OllamaClient(opener=opener, model="")
        with self.assertRaises(OllamaModelError) as ctx:
            client.generate("hello")
        self.assertIn("OLLAMA_MODEL", str(ctx.exception))
        self.assertEqual(opener.calls, [])

    def test_model_not_installed_lists_installed_models(self):
        opener = make_opener({"/api/tags": {"models": [{"name": "tiny:1"}]}})
        client = OllamaClient(opener=opener, model="big:latest")
        with self.assertRaises(OllamaModelError) as ctx:
            client.generate("hello")
        message = str(ctx.exception)
        self.assertIn("big:latest", message)
        self.assertIn("tiny:1", message)
        self.assertIn("not download", message)

    def test_model_short_name_matches_installed_tag(self):
        opener = make_opener(
            {
                "/api/tags": TAGS_OK,
                "/api/generate": {"response": "ok from model"},
            }
        )
        client = OllamaClient(opener=opener, model="qwen3")
        text = client.generate("hi")
        self.assertEqual(text, "ok from model")

    def test_generate_posts_expected_payload(self):
        opener = make_opener(
            {"/api/tags": TAGS_OK, "/api/generate": {"response": "answer"}}
        )
        client = OllamaClient(opener=opener, model="qwen3:0.6b")
        client.generate("do it", system="be careful", json_mode=True)
        generate_calls = [c for c in opener.calls if c["url"].endswith("/api/generate")]
        self.assertEqual(len(generate_calls), 1)
        payload = json.loads(generate_calls[0]["data"].decode("utf-8"))
        self.assertEqual(payload["model"], "qwen3:0.6b")
        self.assertEqual(payload["prompt"], "do it")
        self.assertEqual(payload["system"], "be careful")
        self.assertEqual(payload["format"], "json")
        self.assertFalse(payload["stream"])

    def test_server_reported_missing_model_is_model_error(self):
        opener = make_opener(
            {
                "/api/tags": TAGS_OK,
                "/api/generate": http_error(
                    "http://localhost:11434/api/generate",
                    404,
                    {"error": "model 'qwen3:0.6b' not found, try pulling it first"},
                ),
            }
        )
        client = OllamaClient(opener=opener, model="qwen3:0.6b")
        with self.assertRaises(OllamaModelError):
            client.generate("hi")


class TestResponses(unittest.TestCase):
    def test_invalid_json_body_is_response_error(self):
        opener = make_opener({"/api/tags": "<html>not json</html>"})
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaResponseError):
            client.list_models()

    def test_non_object_json_is_response_error(self):
        opener = make_opener({"/api/tags": "[1, 2, 3]"})
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaResponseError):
            client.list_models()

    def test_tags_without_models_list_is_response_error(self):
        opener = make_opener({"/api/tags": {"unexpected": True}})
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaResponseError):
            client.list_models()

    def test_generate_without_response_text_is_response_error(self):
        opener = make_opener({"/api/tags": TAGS_OK, "/api/generate": {}})
        client = OllamaClient(opener=opener, model="qwen3:0.6b")
        with self.assertRaises(OllamaResponseError):
            client.generate("hi")

    def test_server_500_is_response_error_with_server_text(self):
        opener = make_opener(
            {
                "/api/tags": http_error(
                    "http://localhost:11434/api/tags", 500, {"error": "runtime crashed"}
                )
            }
        )
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaResponseError) as ctx:
            client.list_models()
        self.assertIn("runtime crashed", str(ctx.exception))

    def test_empty_body_is_response_error(self):
        opener = make_opener({"/api/tags": b""})
        client = OllamaClient(opener=opener, model="m")
        with self.assertRaises(OllamaResponseError):
            client.list_models()


class TestNeverDownloads(unittest.TestCase):
    def test_no_pull_request_is_ever_made(self):
        opener = make_opener(
            {"/api/tags": TAGS_OK, "/api/generate": {"response": "hi"}}
        )
        client = OllamaClient(opener=opener, model="qwen3:0.6b")
        client.generate("hello")
        for call in opener.calls:
            self.assertNotIn("/api/pull", call["url"])

    def test_missing_model_error_never_triggers_a_pull(self):
        opener = make_opener({"/api/tags": {"models": []}})
        client = OllamaClient(opener=opener, model="anything")
        with self.assertRaises(OllamaModelError):
            client.generate("hello")
        for call in opener.calls:
            self.assertNotIn("/api/pull", call["url"])


if __name__ == "__main__":
    unittest.main()
