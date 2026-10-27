"""Tests for path security and permissions.

Covers: permitted-root containment, path traversal, absolute paths,
protected locations, symbolic-link escapes, configuration validation
and the fact that no destructive capability exists in the allowlist.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from ai_core.config import ConfigError, load_config
from ai_core.permissions import (
    InvalidPathError,
    PermissionChecker,
    PermissionDenied,
    PathSecurityError,
)

from helpers import Sandbox


class PermissionTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.checker = PermissionChecker(self.sandbox.config)

    def tearDown(self):
        self.sandbox.cleanup()


class TestAllowedPaths(PermissionTestCase):
    def test_relative_path_inside_root(self):
        resolved = self.checker.resolve("Documents", operation="read")
        self.assertEqual(resolved, pathlib.Path(os.path.realpath(self.sandbox.documents)))

    def test_nested_path_inside_root(self):
        resolved = self.checker.resolve(
            "Documents/Business Reports", operation="create"
        )
        self.assertTrue(resolved.is_relative_to(pathlib.Path(os.path.realpath(self.sandbox.documents))))

    def test_dot_means_primary_root(self):
        resolved = self.checker.resolve(".", operation="read")
        self.assertEqual(resolved, pathlib.Path(os.path.realpath(self.sandbox.documents)))

    def test_check_returns_allowed_decision(self):
        decision = self.checker.check("Documents", operation="read")
        self.assertTrue(decision.allowed)
        self.assertIn("inside permitted roots", decision.reason)

    def test_symlink_staying_inside_root_is_allowed(self):
        target = self.sandbox.doc_path("real")
        target.mkdir()
        link = self.sandbox.doc_path("link")
        link.symlink_to(target, target_is_directory=True)
        resolved = self.checker.resolve("Documents/link", operation="read")
        self.assertEqual(resolved, pathlib.Path(os.path.realpath(target)))


class TestTraversalAndEscape(PermissionTestCase):
    def test_dotdot_traversal_rejected(self):
        for bad in ("../../etc/passwd", "Documents/../../..", "a/../b"):
            with self.subTest(path=bad), self.assertRaises(InvalidPathError):
                self.checker.resolve(bad, operation="create")

    def test_absolute_etc_rejected(self):
        with self.assertRaises(PermissionDenied) as ctx:
            self.checker.resolve("/etc/passwd", operation="read")
        self.assertIn("protected", str(ctx.exception).lower())

    def test_absolute_path_outside_roots_rejected(self):
        with self.assertRaises(PathSecurityError):
            self.checker.resolve("/usr/bin/python3", operation="read")

    def test_home_level_file_outside_roots_rejected(self):
        (self.sandbox.home / "secret.txt").write_text("s")
        with self.assertRaises(PermissionDenied) as ctx:
            self.checker.resolve("secret.txt", operation="read")
        self.assertIn("outside the permitted roots", str(ctx.exception))

    def test_tilde_home_is_rejected(self):
        with self.assertRaises(PathSecurityError):
            self.checker.resolve("~", operation="read")

    def test_null_byte_rejected(self):
        with self.assertRaises(InvalidPathError):
            self.checker.resolve("Documents/a\x00b", operation="read")

    def test_empty_path_rejected(self):
        with self.assertRaises(InvalidPathError):
            self.checker.resolve("   ", operation="read")

    def test_too_long_path_rejected(self):
        with self.assertRaises(InvalidPathError):
            self.checker.resolve("Documents/" + "x" * 2000, operation="read")

    def test_directory_symlink_escape_rejected(self):
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        (outside / "loot.txt").write_text("loot")
        link = self.sandbox.documents / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PermissionDenied):
            self.checker.resolve("Documents/escape/loot.txt", operation="read")
        with self.assertRaises(PermissionDenied):
            self.checker.resolve("Documents/escape/new_file.txt", operation="create")

    def test_file_symlink_escape_rejected(self):
        outside_file = self.sandbox.root / "outside.txt"
        outside_file.write_text("secret")
        link = self.sandbox.documents / "innocent.txt"
        link.symlink_to(outside_file)
        with self.assertRaises(PermissionDenied):
            self.checker.resolve("Documents/innocent.txt", operation="read")

    def test_dangling_symlink_escape_rejected(self):
        link = self.sandbox.documents / "dangling"
        link.symlink_to(self.sandbox.root / "nowhere")
        with self.assertRaises(PermissionDenied):
            self.checker.resolve("Documents/dangling", operation="create")

    def test_symlink_chain_escape_rejected(self):
        outside = self.sandbox.root / "outside2"
        outside.mkdir()
        hop1 = self.sandbox.documents / "hop1"
        hop1.symlink_to(outside, target_is_directory=True)
        hop2 = self.sandbox.documents / "hop2"
        hop2.symlink_to(hop1, target_is_directory=True)
        with self.assertRaises(PermissionDenied):
            self.checker.resolve("Documents/hop2/deep", operation="create")


class TestConfiguration(PermissionTestCase):
    def test_root_outside_home_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ConfigError):
                load_config(
                    home=pathlib.Path(td) / "home",
                    roots=["/etc"],
                    state_dir=pathlib.Path(td) / "state",
                )

    def test_root_equal_to_home_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            home.mkdir()
            with self.assertRaises(ConfigError):
                load_config(home=home, roots=[str(home)], state_dir=home / "state")

    def test_relative_root_resolves_under_home(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            home.mkdir()
            cfg = load_config(home=home, roots=["Documents"], state_dir=home / "s")
            self.assertTrue(cfg.permitted_roots[0].is_relative_to(home))

    def test_symlinked_root_pointing_outside_home_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            home.mkdir()
            target = pathlib.Path(td) / "elsewhere"
            target.mkdir()
            link = home / "Documents"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ConfigError):
                load_config(home=home, roots=["Documents"], state_dir=home / "s")

    def test_env_roots_are_honored(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            (home / "Documents").mkdir(parents=True)
            (home / "Downloads").mkdir(parents=True)
            cfg = load_config(
                env={"ALWIN_PERMITTED_ROOTS": "Documents:Downloads"},
                home=home,
                state_dir=home / "s",
            )
            self.assertEqual(len(cfg.permitted_roots), 2)

    def test_bad_timeout_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(env={"OLLAMA_TIMEOUT": "abc"}, home=self.sandbox.home)


class TestCheckAction(PermissionTestCase):
    def test_unknown_action_denied(self):
        decision = self.checker.check_action({"action": "rm_rf", "path": "Documents"})
        self.assertFalse(decision.allowed)
        self.assertIn("allowlist", decision.reason)

    def test_create_action_allowed(self):
        decision = self.checker.check_action(
            {"action": "create_directory", "path": "Documents/X"}
        )
        self.assertTrue(decision.allowed)

    def test_explain_action_always_has_decision(self):
        decision = self.checker.check_action(
            {
                "action": "explain",
                "target": {"action": "create_directory", "path": "Documents/X"},
            }
        )
        self.assertEqual(decision.operation, "describe")


class TestNoDestructiveCapability(unittest.TestCase):
    """Meta-test: the milestone simply has no delete/overwrite/exec."""

    def test_allowlist_has_no_destructive_actions(self):
        from ai_core.planner import ACTION_SCHEMA, MUTATING_ACTIONS

        # v0.2 extension: three document actions joined the allowlist
        # (each create-only, approval-gated, no overwrite).  The set is
        # still pinned exactly so nothing new can slip in unnoticed.
        self.assertEqual(
            set(ACTION_SCHEMA),
            {
                "create_directory",
                "list_directory",
                "read_file",
                "create_text_file",
                "create_report",
                "create_pdf",
                "create_spreadsheet",
                "explain",
            },
        )
        # Every filesystem-changing action requires approval.
        self.assertEqual(
            MUTATING_ACTIONS,
            {
                "create_directory",
                "create_text_file",
                "create_report",
                "create_pdf",
                "create_spreadsheet",
            },
        )

    def test_filesystem_tools_have_no_destructive_calls(self):
        import integrations.filesystem.tools as tools_mod

        source = pathlib.Path(tools_mod.__file__).read_text(encoding="utf-8")
        for banned in (
            "os.remove(",
            "os.unlink(",
            ".unlink(",
            "rmtree",
            "shutil.",
            "os.rename(",
            "os.replace(",
            "os.system(",
            "subprocess",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_core_never_evals_or_shells_out(self):
        import ai_core.executor as executor_mod
        import ai_core.pipeline as pipeline_mod
        import ai_core.planner as planner_mod

        for module in (executor_mod, pipeline_mod, planner_mod):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("eval(", "exec(", "os.system(", "subprocess", "shell=True"):
                with self.subTest(module=module.__name__, banned=banned):
                    self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
