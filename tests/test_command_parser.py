"""Tests for the deterministic parser, planning and action validation.

Covers: supported commands, unsupported commands, path construction,
the action allowlist and validation of model-proposed JSON actions.
"""

from __future__ import annotations

import unittest

from ai_core.command_parser import (
    CREATE_DIRECTORY,
    CREATE_TEXT_FILE,
    EXIT,
    EXPLAIN,
    LIST_DIRECTORY,
    READ_FILE,
    UNSUPPORTED,
    parse_command,
)
from ai_core.planner import (
    ACTION_SCHEMA,
    PlanValidationError,
    describe_action,
    parse_model_action,
    plan_from_intent,
)


class FakeLLMClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.model = "fake:model"
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class TestSupportedCommands(unittest.TestCase):
    def test_spec_example_parses(self):
        intent = parse_command(
            "Create a folder named Business Reports in my Documents directory."
        )
        self.assertEqual(intent.intent, CREATE_DIRECTORY)
        self.assertEqual(intent.slots["name"], "Business Reports")
        self.assertEqual(intent.slots["location"], "Documents")

    def test_spec_example_plans_expected_schema(self):
        intent = parse_command(
            "Create a folder named Business Reports in my Documents directory."
        )
        action = plan_from_intent(intent)
        self.assertEqual(
            action,
            {"action": "create_directory", "path": "Documents/Business Reports"},
        )

    def test_folder_without_named_keyword(self):
        intent = parse_command("make directory Project Alpha in Documents")
        self.assertEqual(intent.intent, CREATE_DIRECTORY)
        self.assertEqual(intent.slots["name"], "Project Alpha")

    def test_folder_without_location_uses_default(self):
        intent = parse_command("create a folder named Inbox")
        self.assertEqual(intent.intent, CREATE_DIRECTORY)
        action = plan_from_intent(intent, default_location="Documents")
        self.assertEqual(action["path"], "Documents/Inbox")

    def test_list_with_location(self):
        intent = parse_command("Show files and folders in my Documents.")
        self.assertEqual(intent.intent, LIST_DIRECTORY)
        self.assertEqual(intent.slots["location"], "Documents")

    def test_list_without_location_defaults_to_root(self):
        intent = parse_command("list files")
        self.assertEqual(intent.intent, LIST_DIRECTORY)
        action = plan_from_intent(intent, default_location="Documents")
        self.assertEqual(action["path"], "Documents")

    def test_list_whats_in_form(self):
        intent = parse_command("what's in my Documents")
        self.assertEqual(intent.intent, LIST_DIRECTORY)
        self.assertEqual(intent.slots["location"], "Documents")

    def test_read_file_with_location(self):
        intent = parse_command("Read notes.txt in Documents")
        self.assertEqual(intent.intent, READ_FILE)
        action = plan_from_intent(intent)
        self.assertEqual(action["path"], "Documents/notes.txt")

    def test_read_file_from_location(self):
        intent = parse_command('read "report final.txt" from Documents')
        self.assertEqual(intent.intent, READ_FILE)
        action = plan_from_intent(intent)
        self.assertEqual(action["path"], "Documents/report final.txt")

    def test_create_text_file_with_content(self):
        intent = parse_command(
            "create a file named todo.txt in Documents containing buy milk"
        )
        self.assertEqual(intent.intent, CREATE_TEXT_FILE)
        action = plan_from_intent(intent)
        self.assertEqual(action["path"], "Documents/todo.txt")
        self.assertEqual(action["content"], "buy milk")

    def test_create_text_file_with_the_content_form(self):
        intent = parse_command(
            'create a file named note.txt with the content "hello world"'
        )
        action = plan_from_intent(intent)
        self.assertEqual(action["path"], "note.txt")
        self.assertEqual(action["content"], "hello world")

    def test_explain_command(self):
        intent = parse_command("Explain create a folder named Archive in Documents.")
        self.assertEqual(intent.intent, EXPLAIN)
        action = plan_from_intent(intent, default_location="Documents")
        self.assertEqual(action["action"], "explain")
        self.assertEqual(
            action["target"],
            {"action": "create_directory", "path": "Documents/Archive"},
        )

    def test_explain_what_would_you_do_form(self):
        intent = parse_command(
            "what would you do if I created a folder named X in Documents"
        )
        self.assertEqual(intent.intent, EXPLAIN)
        action = plan_from_intent(intent)
        self.assertEqual(action["target"]["action"], "create_directory")

    def test_exit_commands(self):
        for text in ("exit", "Quit", "bye", "q", "exit program"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).intent, EXIT)


class TestUnsupportedCommands(unittest.TestCase):
    def test_unsupported_returns_reason(self):
        intent = parse_command("format my hard drive and install windows")
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertTrue(intent.slots["reason"])

    def test_unsupported_plan_raises_with_reason(self):
        intent = parse_command("email my boss the secret plans")
        with self.assertRaises(PlanValidationError) as ctx:
            plan_from_intent(intent)
        self.assertIn("Supported commands", str(ctx.exception))

    def test_file_without_content_is_unsupported(self):
        intent = parse_command("create a file named empty.txt in Documents")
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("content", intent.slots["reason"])

    def test_folder_without_name_is_unsupported(self):
        intent = parse_command("create a folder in Documents")
        self.assertEqual(intent.intent, UNSUPPORTED)

    def test_empty_command_is_unsupported(self):
        self.assertEqual(parse_command("   ").intent, UNSUPPORTED)

    def test_explain_without_inner_command(self):
        self.assertEqual(parse_command("explain").intent, UNSUPPORTED)

    def test_explain_of_unsupported_inner_command(self):
        intent = parse_command("explain reboot the server")
        self.assertEqual(intent.intent, EXPLAIN)
        with self.assertRaises(PlanValidationError) as ctx:
            plan_from_intent(intent)
        self.assertIn("Nothing to explain", str(ctx.exception))


class TestActionValidation(unittest.TestCase):
    def test_allowlist_is_exactly_expected(self):
        # v0.3 extension: detect_applications (read-only) and open_file
        # (approval-gated launch) joined the pinned set.  This is an
        # explicit extension of the allowlist, not a weakening: all ten
        # actions are validated per-schema, mutating ones require
        # approval, and none can delete, move, overwrite, shell out or
        # execute code.  open_file selects an application only through
        # the reviewed registry — never an executable or flag.
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
                "detect_applications",
                "open_file",
                "explain",
            },
        )

    def test_no_destructive_or_exec_actions(self):
        for banned in ("delete", "remove", "overwrite", "exec", "shell", "run_command"):
            self.assertNotIn(banned, ACTION_SCHEMA)

    def test_rejects_non_dict(self):
        from ai_core.planner import validate_action

        for bad in ("create_directory", None, ["create_directory"], 42):
            with self.subTest(bad=bad), self.assertRaises(PlanValidationError):
                validate_action(bad)

    def test_rejects_unknown_action(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError) as ctx:
            validate_action({"action": "delete_path", "path": "Documents/x"})
        self.assertIn("not allowed", str(ctx.exception))

    def test_rejects_shell_style_action(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError):
            validate_action({"action": "create_directory; rm -rf ~", "path": "x"})

    def test_rejects_missing_or_bad_path(self):
        from ai_core.planner import validate_action

        cases = [
            {"action": "create_directory"},
            {"action": "create_directory", "path": ""},
            {"action": "create_directory", "path": 42},
            {"action": "create_directory", "path": "a\x00b"},
            {"action": "create_directory", "path": "../etc"},
            {"action": "create_directory", "path": "Documents/../../x"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(PlanValidationError):
                validate_action(case)

    def test_rejects_unexpected_fields(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "create_directory", "path": "Documents/x", "recursive": True}
            )

    def test_rejects_non_string_content(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "create_text_file", "path": "Documents/x.txt", "content": ["a"]}
            )

    def test_rejects_oversized_content(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError):
            validate_action(
                {
                    "action": "create_text_file",
                    "path": "Documents/x.txt",
                    "content": "a" * (64 * 1024 + 1),
                }
            )

    def test_rejects_nested_explain(self):
        from ai_core.planner import validate_action

        with self.assertRaises(PlanValidationError):
            validate_action(
                {
                    "action": "explain",
                    "target": {
                        "action": "explain",
                        "target": {"action": "list_directory", "path": "Documents"},
                    },
                }
            )


class TestModelProposals(unittest.TestCase):
    def test_valid_json_action_is_accepted(self):
        action = parse_model_action(
            '{"action": "create_directory", "path": "Documents/Reports"}'
        )
        self.assertEqual(action["path"], "Documents/Reports")

    def test_fenced_json_is_accepted(self):
        action = parse_model_action(
            '```json\n{"action": "list_directory", "path": "Documents"}\n```'
        )
        self.assertEqual(action["action"], "list_directory")

    def test_invalid_json_raises(self):
        with self.assertRaises(PlanValidationError) as ctx:
            parse_model_action("I think you should just do it.")
        self.assertIn("valid JSON", str(ctx.exception))

    def test_model_disallowed_action_raises(self):
        with self.assertRaises(PlanValidationError):
            parse_model_action('{"action": "delete_path", "path": "Documents"}')

    def test_model_traversal_path_raises(self):
        with self.assertRaises(PlanValidationError):
            parse_model_action('{"action": "create_directory", "path": "../../etc"}')

    def test_model_unsupported_proposal_raises_with_reason(self):
        with self.assertRaises(PlanValidationError) as ctx:
            parse_model_action('{"action": "unsupported", "reason": "no can do"}')
        self.assertIn("no can do", str(ctx.exception))

    def test_plan_via_llm_validates(self):
        client = FakeLLMClient(
            response='{"action": "create_directory", "path": "Documents/LLM"}'
        )
        from ai_core.planner import plan_via_llm

        action = plan_via_llm("make a folder called LLM", client)
        self.assertEqual(action["path"], "Documents/LLM")
        self.assertEqual(len(client.calls), 1)
        prompt, kwargs = client.calls[0]
        self.assertIn("make a folder called LLM", prompt)
        self.assertTrue(kwargs.get("json_mode"))

    def test_plan_via_llm_invalid_output_raises(self):
        from ai_core.planner import plan_via_llm

        client = FakeLLMClient(response="rm -rf /")
        with self.assertRaises(PlanValidationError):
            plan_via_llm("delete everything", client)


class TestDescriptions(unittest.TestCase):
    def test_describe_create_directory(self):
        text = describe_action(
            {"action": "create_directory", "path": "Documents/X"}
        )
        self.assertEqual(text, "Create directory Documents/X")

    def test_describe_create_text_file_mentions_size(self):
        text = describe_action(
            {"action": "create_text_file", "path": "Documents/x.txt", "content": "abc"}
        )
        self.assertIn("Create text file Documents/x.txt", text)
        self.assertIn("3 bytes", text)

    def test_describe_explain_wraps_target(self):
        text = describe_action(
            {
                "action": "explain",
                "target": {"action": "list_directory", "path": "Documents"},
            }
        )
        self.assertTrue(text.startswith("Explain: List contents of"))


if __name__ == "__main__":
    unittest.main()
