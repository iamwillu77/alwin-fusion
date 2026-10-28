"""v0.5 capability-status tests: every probe is mocked, nothing touches
the network, the model, or the desktop."""

from __future__ import annotations

import unittest

from ai_runtime.ollama_client import (
    OllamaError,
    OllamaModelError,
    OllamaTimeoutError,
    OllamaUnavailableError,
)
from taskui.status import (
    OFFLINE_DETAIL,
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    collect_status,
    probe_ai,
    probe_automation,
)


class FakeClient:
    """Stand-in for OllamaClient: canned responses, zero network."""

    def __init__(self, *, models=None, error=None):
        self._models = models or []
        self._error = error
        self.calls = []

    def list_models(self, timeout=None):
        self.calls.append(timeout)
        if self._error is not None:
            raise self._error
        return list(self._models)


def factory_for(client):
    def factory(**_kwargs):
        return client
    return factory


class ProbeAITests(unittest.TestCase):
    def test_runtime_unreachable_is_reported_honestly(self):
        client = FakeClient(error=OllamaUnavailableError(
            "Cannot reach the Ollama server at http://localhost:11434."))
        status = probe_ai("http://localhost:11434", "llama3",
                          client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_UNAVAILABLE)
        self.assertFalse(status.can_use_llm)
        self.assertIn("Ollama", status.detail)
        # The honest fallback is stated: parsing still works.
        self.assertIn("deterministic parser", status.detail)

    def test_timeout_is_reported_as_unavailable(self):
        client = FakeClient(error=OllamaTimeoutError("Timed out after 2s."))
        status = probe_ai("http://x", "m", client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_UNAVAILABLE)
        self.assertFalse(status.can_use_llm)

    def test_probe_uses_a_short_timeout(self):
        client = FakeClient(models=["llama3:latest"])
        probe_ai("http://x", "llama3", timeout=1.5,
                 client_factory=factory_for(client))
        self.assertEqual(client.calls, [1.5])

    def test_no_model_configured_is_degraded_not_fake_ready(self):
        client = FakeClient(models=["llama3:latest"])
        status = probe_ai("http://x", "", client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_DEGRADED)
        self.assertTrue(status.runtime_reachable)
        self.assertFalse(status.can_use_llm)

    def test_model_missing_is_degraded_with_honest_detail(self):
        client = FakeClient(models=["tinyllama:latest"])
        status = probe_ai("http://x", "llama3", client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_DEGRADED)
        self.assertFalse(status.model_installed)
        self.assertIn("tinyllama", status.detail)
        # We never auto-download; the detail says so.
        self.assertIn("never downloads", status.detail)

    def test_ready_when_model_present(self):
        client = FakeClient(models=["llama3:latest"])
        status = probe_ai("http://x", "llama3", client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_OK)
        self.assertTrue(status.can_use_llm)
        self.assertIn("llama3:latest", status.models)

    def test_tagless_name_match(self):
        client = FakeClient(models=["qwen2.5:7b"])
        status = probe_ai("http://x", "qwen2.5", client_factory=factory_for(client))
        self.assertTrue(status.can_use_llm)

    def test_generic_ollama_error_is_caught(self):
        client = FakeClient(error=OllamaError("garbage response"))
        status = probe_ai("http://x", "m", client_factory=factory_for(client))
        self.assertEqual(status.status, STATUS_UNAVAILABLE)

    def test_unexpected_bugs_surface_instead_of_being_masked(self):
        class Exploding:
            def list_models(self, timeout=None):
                raise RuntimeError("unexpected bug")

        # Only *expected* Ollama failures map to an honest 'unavailable'
        # status; an unexpected bug must surface (fail loudly) rather
        # than be masked as a friendly 'AI is just offline' lie.
        with self.assertRaises(RuntimeError):
            probe_ai("http://x", "m", client_factory=lambda **k: Exploding())


class ProbeAutomationTests(unittest.TestCase):
    def test_bindings_present(self):
        status = probe_automation(spec_finder=lambda name: object())
        self.assertTrue(status.bindings_importable)
        self.assertEqual(status.status, STATUS_OK)

    def test_bindings_missing(self):
        status = probe_automation(spec_finder=lambda name: None)
        self.assertFalse(status.bindings_importable)
        self.assertEqual(status.status, STATUS_UNAVAILABLE)
        # Honest about never auto-installing.
        self.assertIn("never installs packages", status.detail)

    def test_finder_error_is_unavailable_not_crash(self):
        def boom(name):
            raise ValueError("bad spec")

        status = probe_automation(spec_finder=boom)
        self.assertEqual(status.status, STATUS_UNAVAILABLE)


class CollectStatusTests(unittest.TestCase):
    def test_collects_both_probes(self):
        client = FakeClient(models=["llama3:latest"])
        status = collect_status("http://x", "llama3",
                                client_factory=factory_for(client),
                                spec_finder=lambda name: None)
        self.assertEqual(status.ai.status, STATUS_OK)
        self.assertEqual(status.automation.status, STATUS_UNAVAILABLE)
        self.assertEqual(status.offline_detail, OFFLINE_DETAIL)
        as_dict = status.as_dict()
        self.assertIn("ai", as_dict)
        self.assertIn("automation", as_dict)
        self.assertIn("offline", as_dict["offline_detail"])

    def test_offline_statement_is_a_design_fact(self):
        # The offline detail must not pretend to be a network probe.
        self.assertIn("no external network request", OFFLINE_DETAIL)


if __name__ == "__main__":
    unittest.main()
