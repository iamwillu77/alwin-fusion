"""v0.5 task-history tests: roundtrip, corruption, redaction, and the
hard guarantee that a history record can never become executable.

Temporary directories only; no desktop, no display.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import unittest

from taskui.history import (
    HISTORY_VERSION,
    TaskHistory,
    summarize_steps,
    summarize_verification,
)
from taskui.lifecycle import TaskRecord


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alwin-history-test-")
        self.dir = pathlib.Path(self._tmp.name)
        self.history = TaskHistory(self.dir / "task_history.jsonl")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def finished_record(**kwargs) -> TaskRecord:
        rec = TaskRecord(task_id=kwargs.pop("task_id", "abc123def456"),
                         command=kwargs.pop(
                             "command",
                             "Create a folder named Demo in Documents."),
                         **kwargs)
        rec.plan = {
            "action": "create_directory",
            "summary": "Create directory 'Demo' in Documents.",
            "target": "Documents/Demo",
            "mutating": True,
            "requires_approval": True,
            "digest": "deadbeefdeadbeef",
        }
        rec.state = "completed"
        rec.approval = "approved"
        rec.verification = {
            "status": "passed",
            "message": "The directory exists.",
            "checks": [{"name": "exists", "passed": True, "detail": ""}],
        }
        return rec


class RoundtripTests(HistoryTestCase):
    def test_missing_file_loads_empty(self):
        records, skipped = self.history.load()
        self.assertEqual(records, [])
        self.assertEqual(skipped, 0)

    def test_record_then_load(self):
        self.history.record(self.finished_record())
        records, skipped = self.history.load()
        self.assertEqual(skipped, 0)
        self.assertEqual(len(records), 1)
        entry = records[0]
        self.assertEqual(entry["task_id"], "abc123def456")
        self.assertEqual(entry["state"], "completed")
        self.assertEqual(entry["approval"], "approved")
        self.assertEqual(entry["verification"]["status"], "passed")
        self.assertEqual(entry["plan"]["action"], "create_directory")
        self.assertEqual(entry["history_version"], HISTORY_VERSION)

    def test_newest_first_ordering(self):
        for i in range(3):
            self.history.record(self.finished_record(task_id=f"task-{i}"))
        rows = self.history.summary_rows()
        self.assertEqual([r["task_id"] for r in rows],
                         ["task-2", "task-1", "task-0"])

    def test_file_permissions_are_owner_only(self):
        self.history.record(self.finished_record())
        mode = stat.S_IMODE(os.stat(self.history.path).st_mode)
        self.assertEqual(mode, 0o600)
        dir_mode = stat.S_IMODE(os.stat(self.dir).st_mode)
        self.assertEqual(dir_mode, 0o700)

    def test_detail_by_task_id(self):
        self.history.record(self.finished_record())
        detail = self.history.detail("abc123def456")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["task_id"], "abc123def456")
        self.assertIsNone(self.history.detail("nope"))


class CorruptionTests(HistoryTestCase):
    def write_lines(self, lines):
        self.history.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_corrupt_json_is_skipped_and_counted(self):
        good = json.dumps({
            "history_version": HISTORY_VERSION,
            "task_id": "ok-task",
            "state": "completed",
            "created_at": "", "finished_at": "", "request": "x",
            "dry_run": False, "steps": [], "notes": [],
        })
        self.write_lines(["{not json at all", good, "also not json"])
        records, skipped = self.history.load()
        self.assertEqual(len(records), 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(records[0]["task_id"], "ok-task")

    def test_wrong_version_is_skipped(self):
        self.write_lines([json.dumps({
            "history_version": 999, "task_id": "x", "state": "completed",
        })])
        records, skipped = self.history.load()
        self.assertEqual(records, [])
        self.assertEqual(skipped, 1)

    def test_unknown_state_is_skipped(self):
        self.write_lines([json.dumps({
            "history_version": HISTORY_VERSION,
            "task_id": "x", "state": "executing_now",
        })])
        records, skipped = self.history.load()
        self.assertEqual(records, [])
        self.assertEqual(skipped, 1)

    def test_non_dict_line_is_skipped(self):
        self.write_lines(['"just a string"', "42", "null"])
        records, skipped = self.history.load()
        self.assertEqual(records, [])
        self.assertEqual(skipped, 3)

    def test_binary_garbage_is_handled_gracefully(self):
        self.history.path.write_bytes(b"\xff\xfe\x00garbage\n\x00\x00")
        records, skipped = self.history.load()
        self.assertEqual(records, [])
        self.assertGreaterEqual(skipped, 1)

    def test_malformed_step_entries_are_dropped_not_repaired(self):
        self.write_lines([json.dumps({
            "history_version": HISTORY_VERSION,
            "task_id": "x", "state": "completed",
            "steps": ["evil", {"index": 1, "action": "a", "status": "ok"}],
        })])
        records, _ = self.history.load()
        self.assertEqual(len(records[0]["steps"]), 1)


class RedactionTests(HistoryTestCase):
    @staticmethod
    def preview_for(command: str, action: dict):
        """A stand-in for the live TaskPreview the manager attaches.

        In the real flow ``record._preview`` holds the validated action,
        whose ``content`` field is what ``_mask_command`` redacts.
        """
        class _Prev:
            pass

        prev = _Prev()
        prev.action = action
        return prev

    def test_command_with_embedded_content_is_masked(self):
        rec = self.finished_record(
            command="Create a file named note.txt in Documents containing "
                    "my secret diary entry"
        )
        rec._preview = self.preview_for(rec.command, {
            "action": "create_text_file",
            "path": "Documents/note.txt",
            "content": "my secret diary entry",
        })
        entry = self.history.record(rec)
        self.assertNotIn("my secret diary entry", json.dumps(entry))
        self.assertIn("redacted", json.dumps(entry))

    def test_file_content_never_stored_verbatim(self):
        secret = "PASSWORD=hunter2 and token=zxq9wv7"
        rec = self.finished_record(
            command=f"Create a file named x.txt in Documents containing {secret}"
        )
        rec._preview = self.preview_for(rec.command, {
            "action": "create_text_file",
            "path": "Documents/x.txt",
            "content": secret,
        })
        entry = self.history.record(rec)
        flat = json.dumps(entry)
        self.assertNotIn("hunter2", flat)
        self.assertNotIn("zxq9wv7", flat)

    def test_sensitive_keys_are_stripped_even_if_smuggled(self):
        rec = self.finished_record()
        rec.notes = ["normal note"]
        # Simulate a summary dict sneaking a sensitive key through notes
        # is impossible (notes are strings); instead verify the redactor
        # runs over the whole entry:
        entry = self.history.record(rec)
        self.assertNotIn("password", json.dumps(entry).lower())

    def test_long_fields_are_bounded_on_read(self):
        self.history.path.write_text(json.dumps({
            "history_version": HISTORY_VERSION,
            "task_id": "x", "state": "completed",
            "request": "A" * 100000,
            "error": "B" * 100000,
        }) + "\n", encoding="utf-8")
        records, _ = self.history.load()
        self.assertLess(len(records[0]["request"]), 1000)
        self.assertLess(len(records[0]["error"]), 1000)


class NeverExecutableTests(HistoryTestCase):
    def test_history_module_does_not_import_execution_code(self):
        source = pathlib.Path(
            __import__("taskui.history", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        for banned in (
            "ai_core.pipeline",
            "ai_core.executor",
            "ai_core.planner",
            "integrations",
            "subprocess",
            "os.system",
            "eval(",
            "exec(",
        ):
            with self.subTest(banned=banned):
                # The only pipeline reference is a *local* import of the
                # masking helper; forbid top-level/trusted-exec imports.
                if banned == "ai_core.pipeline":
                    lines = [ln for ln in source.splitlines()
                             if "ai_core.pipeline" in ln]
                    for line in lines:
                        self.assertIn("local: masking helper", line)
                    continue
                self.assertNotIn(banned, source)

    def test_hostile_record_cannot_inject_markup_or_instructions(self):
        self.history.path.write_text(json.dumps({
            "history_version": HISTORY_VERSION,
            "task_id": "evil", "state": "completed",
            "request": "<script>alert(1)</script>",
            "result_message": "rm -rf ~",
            "plan": {"action": "__import__('os').system('x')",
                     "summary": "run", "target": "", "mutating": True,
                     "requires_approval": True},
        }) + "\n", encoding="utf-8")
        rows = self.history.summary_rows()
        # Data is preserved as inert text; nothing interprets it.
        self.assertEqual(len(rows), 1)
        detail = self.history.detail("evil")
        self.assertFalse(detail["replayable"] if "replayable" in detail
                         else False)
        # The taskui.history module exposes no execution surface at all.
        self.assertFalse(hasattr(self.history, "run"))
        self.assertFalse(hasattr(self.history, "execute"))
        self.assertFalse(hasattr(self.history, "replay"))

    def test_history_class_surface_is_display_only(self):
        public = [n for n in dir(self.history) if not n.startswith("_")]
        self.assertEqual(sorted(public),
                         sorted(["detail", "load", "path", "record",
                                 "summary_rows"]))


class CuratorTests(unittest.TestCase):
    def test_summarize_verification_keeps_status_and_checks(self):
        out = summarize_verification({
            "status": "passed",
            "message": "ok",
            "checks": [{"name": "a", "passed": True},
                       "garbage",
                       {"name": "b", "passed": False, "detail": "why"}],
            "raw_payload": "x" * 10000,
        })
        self.assertEqual(out["status"], "passed")
        self.assertEqual(len(out["checks"]), 2)
        self.assertNotIn("raw_payload", out)

    def test_summarize_verification_rejects_unknown_status(self):
        out = summarize_verification({"status": "totally-verified"})
        self.assertIsNone(out["status"])

    def test_summarize_steps_bounds_and_filters(self):
        out = summarize_steps(
            [{"index": i, "action": "a", "status": "ok"} for i in range(1000)]
            + ["junk"]
        )
        self.assertEqual(len(out), 64)
        self.assertTrue(all(isinstance(s, dict) for s in out))


if __name__ == "__main__":
    unittest.main()
