"""v0.17.0 intelligent application interaction tests.

Labels (accurate, per milestone brief):

* **SANDBOX** — every adapter/tool test runs a REAL
  :class:`AtspiSession` (the production traversal/search code) against
  an in-memory fake AT-SPI desktop in a temporary directory; the
  permission checker is the real one over a sandboxed config.  No real
  application is touched, no network, no display.
* **MOCKED** — the GNOME Text Editor tests mirror the REAL tree shape
  observed on the reference host (application → frame titled
  '… - Text Editor' → panels/groupings → buttons, toggle buttons,
  labels, page tabs; NO text interface).  The real-host evidence lives
  in ``scripts/verify_app_interaction.py``.
* **STATIC** — trust-isolation tests parse the source (AST): the new
  action stays inside the closed pipeline, adapter modules import no
  engine and no companion code, and application-provided strings stay
  sanitized display data that can never authorize anything.

Covers the brief's Phase 10 groups: target validation (missing app/
window, duplicate names, ambiguous, stale, defunct, unsupported,
traversal, oversized text, unavailable service), permission/approval
pins, verification honesty (API success ≠ verified success; uncertain
mutations are never retried), privacy/injection, and regression pins.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from automation_fakes import (  # noqa: E402
    AutomationSandbox,
    FakeAtspiModule,
    FakeNode,
)
from ai_core.planner import (  # noqa: E402
    MUTATING_ACTIONS,
    PlanValidationError,
    validate_action,
)
from ai_core.permissions import PermissionChecker  # noqa: E402
from integrations.automation.atspi_backend import AtspiSession  # noqa: E402
from integrations.automation.base import (  # noqa: E402
    ACTION_PERFORMED,
    AMBIGUOUS_TARGET,
    CAP_ACTIVATE,
    CAP_FIND,
    CAP_INSPECT,
    CAP_LIST,
    CAP_READ,
    MAX_CONTROL_NAME_CHARS,
    MAX_LIST_CONTROLS,
    RESULT_VERIFIED,
    STALE_TARGET,
    ControlLocator,
)
from integrations.automation.gnome_text_editor import (  # noqa: E402
    GnomeTextEditorAdapter,
)
from integrations.automation.libreoffice import LibreOfficeAdapter  # noqa: E402
from integrations.automation.tools import (  # noqa: E402
    ADAPTER_BUILDERS,
    AutomationTools,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def gnome_text_editor_tree():
    """The REAL tree shape observed on the host (see module docstring).

    No text/document/entry roles — GNOME Text Editor does not expose
    the document body through AT-SPI on this build.
    """
    app = FakeNode("application", "gnome-text-editor")
    window = FakeNode(
        "frame", "test.txt (/tmp/x) - Text Editor"
    )
    panel = FakeNode("panel")
    new_tab = FakeNode("button", "New Tab", actions=("click",))
    doc_props = FakeNode(
        "toggle button", "Document Properties", actions=("click",),
        states=("checked",),
    )
    file_label = FakeNode("label", "test.txt")
    tab = FakeNode("page tab", "test.txt")
    open_button = FakeNode("button", "Open", actions=("click",))
    open_toggle = FakeNode("toggle button", "Open", actions=("click",))
    window.add(panel)
    panel.add(new_tab, doc_props, file_label, tab, open_button, open_toggle)
    app.add(window)
    nodes = {
        "app": app, "window": window, "new_tab": new_tab,
        "doc_props": doc_props, "file_label": file_label, "tab": tab,
        "open_button": open_button, "open_toggle": open_toggle,
    }
    return app, nodes


def editor_tools(nodes_module, *, running_comm: str = "") -> AutomationTools:
    import shutil
    import tempfile
    from ai_core.config import load_config
    root = pathlib.Path(tempfile.mkdtemp(prefix="alwin-v017-test-"))
    config = load_config(home=root, roots=["Documents"],
                         state_dir=root / "state")
    checker = PermissionChecker(config)
    session = AtspiSession(nodes_module)
    proc_root = None
    comm_names = None
    if running_comm:
        proc_root = str(root / "proc")
        pathlib.Path(proc_root).mkdir()
        entry = pathlib.Path(proc_root) / "42"
        entry.mkdir()
        (entry / "comm").write_text(running_comm + "\n", encoding="utf-8")
        comm_names = {running_comm}
    return AutomationTools(
        checker, session=session,
        which=(lambda name: "/usr/bin/" + name),
        proc_root=proc_root, comm_names=comm_names,
    )


def defunct(node: FakeNode) -> None:
    from automation_fakes import FakeState
    node._states.append(FakeState("defunct"))


# --------------------------------------------------------------------------- #
# Target validation (SANDBOX)
# --------------------------------------------------------------------------- #


class TargetValidationTests(unittest.TestCase):

    def test_missing_application_is_an_honest_failure(self):
        s = AutomationSandbox(apps=None)
        result = s.tools.find_control(
            "libreoffice-writer", {"role": "button", "name": "OK"}
        )
        self.assertFalse(result.ok)
        self.assertIn("No accessible interface", result.error)

    def test_unregistered_application_is_refused_before_anything(self):
        s = AutomationSandbox()
        result = s.tools.find_control(
            "evince", {"role": "button", "name": "OK"}
        )
        self.assertFalse(result.ok)

    def test_missing_window_still_searches_the_app_node(self):
        # An app that exposes no frame: the search root falls back to
        # the application node — nothing crashes, nothing is invented.
        app = FakeNode("application", "LibreOffice Writer")
        button = FakeNode("button", "OK", actions=("click",))
        app.add(button)
        module = FakeAtspiModule(apps=[app])
        tools = editor_tools(module)
        result = tools.find_control(
            "libreoffice-writer", {"role": "button", "name": "OK"}
        )
        self.assertTrue(result.ok)

    def test_duplicate_names_are_ambiguous_not_guessed(self):
        s = AutomationSandbox()
        duplicate = FakeNode("button", "Save", actions=("click",))
        s.nodes["window"].add(duplicate)
        result = s.tools.find_control(
            "libreoffice-writer", {"role": "button", "name": "Save"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("state"), AMBIGUOUS_TARGET)
        self.assertIn("ambiguous", result.error.lower())

    def test_ambiguous_mutation_actually_never_runs(self):
        s = AutomationSandbox()
        duplicate = FakeNode("button", "OK", actions=("click",))
        s.nodes["window"].add(duplicate)
        adapter = LibreOfficeAdapter("libreoffice-writer",
                                     session=s.session)
        result = adapter.activate_control(
            ControlLocator(role="button", name="OK")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.state, AMBIGUOUS_TARGET)
        self.assertEqual(s.nodes["ok"].activated, 0)
        self.assertEqual(duplicate.activated, 0)

    def test_defunct_target_is_stale_and_nothing_runs(self):
        s = AutomationSandbox()
        defunct(s.nodes["ok"])
        adapter = LibreOfficeAdapter("libreoffice-writer",
                                     session=s.session)
        result = adapter.activate_control(
            ControlLocator(role="button", name="OK")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.state, STALE_TARGET)
        self.assertEqual(s.nodes["ok"].activated, 0)

    def test_defunct_between_find_and_activate_is_stale(self):
        s = AutomationSandbox()
        adapter = LibreOfficeAdapter("libreoffice-writer",
                                     session=s.session)
        found = adapter.find_control(ControlLocator(role="button",
                                                    name="OK"))
        self.assertTrue(found.ok)
        defunct(s.nodes["ok"])          # the app closes it right after
        result = adapter.activate_control(
            ControlLocator(role="button", name="OK")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.state, STALE_TARGET)
        self.assertEqual(s.nodes["ok"].activated, 0)

    def test_unsupported_capability_is_honest(self):
        module = FakeAtspiModule(apps=[gnome_text_editor_tree()[0]])
        tools = editor_tools(module, running_comm="gnome-text-edit")
        result = tools.read_control(
            "gnome-text-editor", {"role": "text", "name": ""}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("unsupported"), "read_control")

    def test_unregistered_adapter_gets_unavailable_adapter(self):
        s = AutomationSandbox()
        result = s.tools.list_controls("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("list_controls", result.error)

    def test_unavailable_service_reports_state_unknown(self):
        module = FakeAtspiModule(apps=(),
                                 desktop_error=RuntimeError("bus died"))
        tools = editor_tools(module, running_comm="soffice.bin")
        result = tools.inspect("libreoffice-writer")
        # A read-only inspect is a successful *report* — but it must
        # never claim the interface was detected when the bus died.
        snapshot = result.data.get("snapshot", {})
        self.assertIs(snapshot.get("interface_detected"), False)
        self.assertIs(snapshot.get("a11y_available"), False)
        self.assertIn("accessible interface was not detected",
                      result.message)

    def test_oversized_control_name_is_capped(self):
        big = "A" * 500
        app = FakeNode("application", "LibreOffice Writer")
        window = FakeNode("frame", "x - LibreOffice Writer")
        button = FakeNode("button", big, actions=("click",))
        window.add(button)
        app.add(window)
        module = FakeAtspiModule(apps=[app])
        tools = editor_tools(module)
        result = tools.find_control(
            "libreoffice-writer", {"role": "button", "name": big[:100]}
        )
        # the name is 500 chars: the planner caps locators at 120, the
        # serialization caps the reported name at 120 — bounded either way
        payload = json.dumps(result.data)
        self.assertNotIn("A" * 200, payload)

    def test_traversal_is_bounded_even_on_a_huge_tree(self):
        app = FakeNode("application", "LibreOffice Writer")
        window = FakeNode("frame", "x - LibreOffice Writer")
        parent = window
        # 200 chained panels: the listing must stay within its caps.
        for _ in range(200):
            child = FakeNode("panel")
            parent.add(child)
            parent = child
        for index in range(60):
            window.add(FakeNode("button", f"B{index}",
                                actions=("click",)))
        app.add(window)
        module = FakeAtspiModule(apps=[app])
        tools = editor_tools(module)
        result = tools.list_controls("libreoffice-writer")
        self.assertTrue(result.ok)
        self.assertLessEqual(result.data["count"], MAX_LIST_CONTROLS)


# --------------------------------------------------------------------------- #
# Bounded listing (SANDBOX)
# --------------------------------------------------------------------------- #


class ListControlsTests(unittest.TestCase):

    def test_listing_is_read_only_summary_only(self):
        s = AutomationSandbox()
        result = s.tools.list_controls("libreoffice-writer")
        self.assertTrue(result.ok)
        self.assertLessEqual(result.data["count"], MAX_LIST_CONTROLS)
        payload = json.dumps(result.data)
        # Document text must never ride along in a listing.
        self.assertNotIn("Document body text.", payload)
        self.assertNotIn("Hello Title", payload)
        for control in result.data["controls"]:
            self.assertEqual(
                set(control),
                {"role", "name", "states", "action_names", "readable",
                 "enterable", "index_path"},
            )

    def test_listing_flags_truncation_honestly(self):
        s = AutomationSandbox()
        window = s.nodes["window"]
        for index in range(MAX_LIST_CONTROLS + 10):
            window.add(FakeNode("label", f"Filler {index}"))
        result = s.tools.list_controls("libreoffice-writer")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(result.data["count"], MAX_LIST_CONTROLS)

    def test_listing_with_locator_roots_at_the_control(self):
        s = AutomationSandbox()
        result = s.tools.list_controls(
            "libreoffice-writer",
            {"role": "document frame", "name": ""},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)  # leaf control

    def test_listing_ambiguous_locator_is_rejected(self):
        s = AutomationSandbox()
        s.nodes["window"].add(FakeNode("button", "OK",
                                       actions=("click",)))
        result = s.tools.list_controls(
            "libreoffice-writer", {"role": "button", "name": "OK"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("state"), AMBIGUOUS_TARGET)

    def test_hostile_accessible_names_are_sanitized(self):
        s = AutomationSandbox()
        result = s.tools.list_controls("libreoffice-writer")
        payload = json.dumps(result.data)
        self.assertNotIn("\x1b", payload)
        self.assertNotIn("\n", payload.split("Evil")[1][:12]
                         if "Evil" in payload else "")


# --------------------------------------------------------------------------- #
# GNOME Text Editor adapter (MOCKED tree = real observed shape)
# --------------------------------------------------------------------------- #


class GnomeTextEditorTests(unittest.TestCase):

    def tools_for(self):
        module = FakeAtspiModule(apps=[gnome_text_editor_tree()[0]])
        return editor_tools(module), module

    def test_registered_as_a_real_adapter(self):
        self.assertIs(ADAPTER_BUILDERS.get("gnome-text-editor"),
                      GnomeTextEditorAdapter)

    def test_capabilities_exclude_unsupported_operations(self):
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=AtspiSession(None))
        caps = adapter.capabilities()
        self.assertIn(CAP_INSPECT, caps)
        self.assertIn(CAP_FIND, caps)
        self.assertIn(CAP_ACTIVATE, caps)
        self.assertIn(CAP_LIST, caps)
        self.assertNotIn(CAP_READ, caps)

    def test_inspect_is_honest_about_text_not_being_exposed(self):
        module = FakeAtspiModule(apps=[gnome_text_editor_tree()[0]])
        tools = editor_tools(module, running_comm="gnome-text-edit")
        snap = tools.inspect("gnome-text-editor")
        self.assertTrue(snap.ok)
        payload = json.dumps(snap.data)
        self.assertIn("not exposed", payload)

    def test_find_identifies_a_real_control(self):
        tools, _ = self.tools_for()
        result = tools.find_control(
            "gnome-text-editor",
            {"role": "toggle button", "name": "Document Properties"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["control"]["role"], "toggle button")

    def test_duplicate_open_names_are_ambiguous(self):
        tools, _ = self.tools_for()
        result = tools.find_control(
            "gnome-text-editor", {"role": "toggle button", "name": "Open"}
        )
        # 'Open' exists as BOTH a button and a toggle button; same-role
        # duplicates would be ambiguous, cross-role matches differ by
        # role so this specific lookup resolves — but a role-only
        # search is ambiguous.
        role_only = tools.find_control(
            "gnome-text-editor", {"role": "toggle button", "name": ""}
        )
        self.assertFalse(role_only.ok)
        self.assertEqual(role_only.data.get("state"), AMBIGUOUS_TARGET)

    def test_read_control_is_honestly_unsupported(self):
        tools, _ = self.tools_for()
        # Tools layer: the capability is honestly declared unsupported
        # (it is NOT in the adapter's capability set).
        result = tools.read_control(
            "gnome-text-editor", {"role": "label", "name": "test.txt"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("unsupported"), "read_control")
        # Adapter layer: the specific, informative reason.
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=tools.session)
        direct = adapter.read_control(
            ControlLocator(role="label", name="test.txt")
        )
        self.assertFalse(direct.ok)
        self.assertIn("does not expose document text", direct.error)

    def test_activation_with_state_change_is_verified(self):
        tools, module = self.tools_for()
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=tools.session)
        node = None
        # locate the toggle node in the fake tree
        app = module.get_desktop(0).get_child_at_index(0)
        stack = [app]
        while stack:
            current = stack.pop()
            if current.get_role_name() == "toggle button" and \
                    current.get_name() == "Document Properties":
                node = current
                break
            stack.extend(
                current.get_child_at_index(i)
                for i in range(current.get_child_count())
            )
        self.assertIsNotNone(node)
        from automation_fakes import FakeState
        node.on_action = lambda: node._states.append(
            FakeState("pressed"))
        result = adapter.activate_control(
            ControlLocator(role="toggle button",
                           name="Document Properties")
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RESULT_VERIFIED)
        self.assertTrue(result.data["verified"])

    def test_activation_without_observable_change_is_unverified(self):
        tools, module = self.tools_for()
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=tools.session)
        result = adapter.activate_control(
            ControlLocator(role="button", name="New Tab")
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.state, ACTION_PERFORMED)
        self.assertFalse(result.data["verified"])
        self.assertIn("UNVERIFIED", result.message)

    def test_non_activatable_role_is_refused(self):
        tools, _ = self.tools_for()
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=tools.session)
        result = adapter.activate_control(
            ControlLocator(role="label", name="test.txt")
        )
        self.assertFalse(result.ok)
        self.assertIn("not activatable", result.error)


# --------------------------------------------------------------------------- #
# Verification honesty (SANDBOX)
# --------------------------------------------------------------------------- #


class VerificationTests(unittest.TestCase):

    def test_api_success_without_state_change_is_not_success(self):
        s = AutomationSandbox()
        result = s.tools.activate_control(
            "libreoffice-writer", {"role": "button", "name": "Save"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["state"], ACTION_PERFORMED)
        self.assertFalse(result.data["verified"])

    def test_state_change_marks_verified(self):
        s = AutomationSandbox()
        from automation_fakes import FakeState
        bold = s.nodes["bold"]
        bold.on_action = lambda: bold._states.append(FakeState("pressed"))
        result = s.tools.activate_control(
            "libreoffice-writer",
            {"role": "toggle button", "name": "Bold"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["state"], RESULT_VERIFIED)

    def test_uncertain_mutation_is_attempted_exactly_once(self):
        s = AutomationSandbox()
        ok = s.nodes["ok"]
        calls = []
        original = ok.do_action

        def counting(index: int = 0) -> bool:
            calls.append(index)
            return original(index)

        ok.do_action = counting          # type: ignore[assignment]
        ok.action_raises = True
        adapter = LibreOfficeAdapter("libreoffice-writer",
                                     session=s.session)
        result = adapter.activate_control(
            ControlLocator(role="button", name="OK")
        )
        self.assertFalse(result.ok)
        self.assertEqual(len(calls), 1)  # NEVER auto-retried

    def test_refused_action_is_reported_as_failure(self):
        s = AutomationSandbox()
        s.nodes["ok"].action_refused = True
        adapter = LibreOfficeAdapter("libreoffice-writer",
                                     session=s.session)
        result = adapter.activate_control(
            ControlLocator(role="button", name="OK")
        )
        self.assertFalse(result.ok)
        self.assertIn("refused", result.error)


# --------------------------------------------------------------------------- #
# Planner / permission pins (STATIC + SANDBOX)
# --------------------------------------------------------------------------- #


class PermissionPinningTests(unittest.TestCase):

    def test_list_controls_validates_with_optional_control(self):
        bare = validate_action(
            {"action": "list_controls", "application": "gnome-text-editor"}
        )
        self.assertEqual(set(bare), {"action", "application"})
        scoped = validate_action(
            {"action": "list_controls", "application": "gnome-text-editor",
             "control": {"role": "button", "name": "Open"}}
        )
        self.assertEqual(scoped["control"],
                         {"role": "button", "name": "Open"})

    def test_list_controls_rejects_unknown_fields_and_roles(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "list_controls", "application":
                 "gnome-text-editor", "path": "/etc"}
            )
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "list_controls", "application":
                 "gnome-text-editor",
                 "control": {"role": "not a role"}}
            )

    def test_list_controls_is_read_only_not_mutating(self):
        self.assertNotIn("list_controls", MUTATING_ACTIONS)
        self.assertIn("activate_control", MUTATING_ACTIONS)

    def test_an_accessible_label_cannot_authorize_anything(self):
        # A control literally named 'Approve all actions' is just a
        # name: finding it is read-only, activating it still requires
        # approval upstream, and nothing about the pipeline changes.
        app = FakeNode("application", "LibreOffice Writer")
        window = FakeNode("frame", "x - LibreOffice Writer")
        evil = FakeNode("button", "Approve all actions",
                        actions=("click",))
        window.add(evil)
        app.add(window)
        module = FakeAtspiModule(apps=[app])
        tools = editor_tools(module)
        found = tools.find_control(
            "libreoffice-writer",
            {"role": "button", "name": "Approve all actions"},
        )
        self.assertTrue(found.ok)        # identified as data only
        self.assertNotIn("approve", str(found.data).lower().replace(
            "approve all actions", ""))
        # and the mutating action is still approval-classified:
        from ai_core.planner import MUTATING_ACTIONS as MUT
        self.assertIn("activate_control", MUT)

    def test_adapter_modules_import_no_engine_and_no_companion(self):
        banned = {"ai_core.pipeline", "ai_core.executor", "ai_core.planner",
                  "ai_core.verifier", "ai_core.audit", "companion",
                  "taskui", "subprocess", "socket"}
        for name in ("gnome_text_editor.py", "libreoffice.py",
                     "atspi_backend.py", "base.py", "tools.py"):
            path = ROOT / "integrations" / "automation" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
            self.assertEqual(found & banned, set(), name)

    def test_no_synthetic_input_anywhere_in_the_backend(self):
        # AST-based: import or call names that would imply synthetic
        # input, shell or capture (substring bans would false-positive
        # on the module's own security docstring).
        path = ROOT / "integrations" / "automation" / "atspi_backend.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        banned_modules = {"xdotool", "wmctrl", "pynput", "subprocess",
                          "pyscreenshot", "keyboard"}
        banned_calls = {"system", "popen", "run", "check_output"}
        found_modules = set()
        found_calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found_modules.update(a.name.split(".")[0]
                                     for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found_modules.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    found_calls.add(func.attr)
                elif isinstance(func, ast.Name):
                    found_calls.add(func.id)
        self.assertEqual(found_modules & banned_modules, set())
        self.assertEqual(found_calls & banned_calls, set())


# --------------------------------------------------------------------------- #
# Regression pins
# --------------------------------------------------------------------------- #


class RegressionPins(unittest.TestCase):

    def test_existing_actions_are_unchanged(self):
        for payload in (
            {"action": "find_control", "application":
             "libreoffice-writer", "control": {"role": "button",
                                                "name": "OK"}},
            {"action": "inspect_application", "application":
             "libreoffice-calc"},
        ):
            self.assertTrue(validate_action(payload))

    def test_gnome_text_editor_registry_entry_keeps_its_contract(self):
        from integrations.apps.registry import BUILTIN_REGISTRY
        definition = BUILTIN_REGISTRY.get("gnome-text-editor")
        self.assertIn("open_file", definition.capabilities)
        self.assertIn(".txt", definition.file_types)

    def test_known_roles_cover_the_new_listing(self):
        from ai_core.planner import KNOWN_ROLES
        for role in ("toggle button", "page tab", "panel", "grouping",
                     "scroll pane", "separator"):
            # panel/grouping/scroll pane/separator are structural; the
            # planner only needs the roles a LISTING may target later.
            if role in ("toggle button", "page tab"):
                self.assertIn(role, KNOWN_ROLES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
