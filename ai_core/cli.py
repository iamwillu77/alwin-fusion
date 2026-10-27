"""Alwin Fusion command-line interface.

Run interactively::

    python3 -m ai_core.cli

Run a single command::

    python3 -m ai_core.cli --dry-run "Create a folder named Test in Documents"
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from ai_core import __version__
from ai_core.command_parser import SUPPORTED_COMMAND_EXAMPLES
from ai_core.config import Config, ConfigError, load_config
from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_EXIT,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    STATUS_DRY_RUN,
    TaskPipeline,
)
from ai_core.permissions import PermissionChecker
from ai_runtime.ollama_client import OllamaClient, OllamaError

BANNER = "ALWIN FUSION v0.2\nLocal AI Core + Documents"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

APPROVE_YES = {"y", "yes"}


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def alwin_print(lines, file=None) -> None:
    """Print with the ``ALWIN:`` prefix and hanging indent."""
    file = file or sys.stdout
    if isinstance(lines, str):
        lines = [lines]
    first = True
    for line in lines:
        if first:
            print(f"ALWIN: {line}", file=file)
            first = False
        else:
            print(f"       {line}", file=file)


def print_help_text(file=None) -> None:
    file = file or sys.stdout
    print("Supported commands:", file=file)
    for example in SUPPORTED_COMMAND_EXAMPLES:
        print(f"  - {example}", file=file)
    print("", file=file)
    print("Options: --dry-run (no changes), --llm (use local Ollama to", file=file)
    print("propose actions), --root DIR, --ai-status, --show-config,", file=file)
    print("--help.  Type 'quit' or 'exit' to leave.", file=file)


def _interactive_approver(description: str) -> bool:
    """Ask for approval on stdin; anything but yes means no."""
    try:
        answer = input("       Approve? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in APPROVE_YES


def _print_success(report) -> None:
    action = report.action or {}
    name = action.get("action")
    execution = report.execution
    verification = report.verification or {}

    if name == "explain":
        explanation = (execution.data.get("explanation") if execution else "") or ""
        alwin_print(explanation.splitlines() or ["Explanation unavailable."])
        return

    if name in (
        "create_directory",
        "create_text_file",
        "create_report",
        "create_pdf",
        "create_spreadsheet",
    ):
        alwin_print(verification.get("message", "Completed."))
        return

    if name == "read_file" and execution is not None:
        alwin_print([execution.message])
        content = execution.data.get("content", "")
        sys.stdout.write(content)
        if content and not content.endswith("\n"):
            sys.stdout.write("\n")
        return

    if name == "list_directory" and execution is not None:
        entries = execution.data.get("entries", [])
        lines = [execution.message]
        for entry in entries:
            entry_name = entry.get("name", "?")
            kind = entry.get("type", "file")
            if kind == "directory":
                lines.append(f"{entry_name}/")
            elif kind == "symlink":
                lines.append(f"{entry_name}@ (symlink)")
            else:
                lines.append(entry_name)
        alwin_print(lines)
        return

    alwin_print(execution.message if execution else "Done.")


def print_report(report) -> None:
    status = report.status
    if status == STATUS_UNSUPPORTED:
        lines = ["Sorry, I could not understand that command."]
        if report.error:
            lines.append(report.error)
        lines.append("")
        lines.append("Try one of these:")
        lines.extend(f"  - {example}" for example in SUPPORTED_COMMAND_EXAMPLES)
        alwin_print(lines)
    elif status == STATUS_DENIED:
        lines = ["Permission denied."]
        if report.error:
            lines.append(report.error)
        alwin_print(lines)
    elif status == STATUS_REJECTED:
        alwin_print("Operation cancelled. Nothing was changed.")
    elif status == STATUS_FAILED:
        lines = ["Operation failed."]
        if report.error:
            lines.append(report.error)
        alwin_print(lines)
    elif status == STATUS_DRY_RUN:
        message = report.execution.message if report.execution else "Dry run complete."
        alwin_print([message])
    elif status == STATUS_SUCCESS:
        _print_success(report)

    for note in report.notes:
        alwin_print([f"Note: {note}"])


# --------------------------------------------------------------------------- #
# Flag handlers
# --------------------------------------------------------------------------- #


def _ai_status(config: Config) -> int:
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_model,
        timeout=min(config.ollama_timeout, 5.0),
    )
    print(f"Probing local AI runtime at {client.base_url} ...")
    try:
        models = client.list_models()
    except OllamaError as exc:
        alwin_print(
            [
                f"Local AI runtime unavailable: {exc}",
                "The core keeps working with the built-in deterministic parser.",
            ]
        )
        return EXIT_FAILURE

    print(f"Installed models: {', '.join(models) if models else '(none)'}")
    if not client.model:
        print(
            "No model configured (set OLLAMA_MODEL). The core works without "
            "a model; --llm needs one."
        )
        return EXIT_OK
    if any(m == client.model or m.split(":")[0] == client.model for m in models):
        print(f"Configured model '{client.model}': available.")
        return EXIT_OK
    print(
        f"Configured model '{client.model}': NOT installed. Install it with "
        f"'ollama pull {client.model}' yourself if desired; Alwin Fusion never "
        f"downloads models."
    )
    return EXIT_FAILURE


# --------------------------------------------------------------------------- #
# Argument parsing / main
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_core.cli",
        description=(
            "Alwin Fusion local AI core: turn natural-language commands "
            "into validated, approved and verified filesystem actions. "
            "Runs fully offline."
        ),
        epilog=(
            "Examples:\n"
            '  python3 -m ai_core.cli "Show files in my Documents"\n'
            '  python3 -m ai_core.cli --dry-run "Create a folder named Test in Documents"\n'
            "  python3 -m ai_core.cli            # interactive mode\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="a single command to run (omit for interactive mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without modifying the filesystem",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help=(
            "let the local Ollama model propose actions (proposals are "
            "validated by the trusted allowlist; falls back to the built-in "
            "parser when the runtime is unavailable)"
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        metavar="DIR",
        help=(
            "permitted root, repeatable; must be inside the home directory "
            "(overrides ALWIN_PERMITTED_ROOTS for this run)"
        ),
    )
    parser.add_argument(
        "--ai-status",
        action="store_true",
        help="probe the local Ollama runtime and exit",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="print the effective configuration and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Alwin Fusion {__version__}",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(roots=args.root if args.root else None)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.show_config:
        print(config.describe())
        return EXIT_OK

    if args.ai_status:
        return _ai_status(config)

    llm_client = None
    if args.llm:
        llm_client = OllamaClient(
            base_url=config.ollama_base_url,
            model=config.ollama_model,
            timeout=config.ollama_timeout,
        )

    checker = PermissionChecker(config)

    def on_proposal(description: str) -> None:
        lines = ["Proposed action:", description]
        if args.dry_run:
            lines.append("(dry run: nothing will be changed)")
        alwin_print(lines)

    pipeline = TaskPipeline(
        config,
        checker=checker,
        approver=_interactive_approver,
        on_proposal=on_proposal,
        use_llm=args.llm,
        llm_client=llm_client,
    )

    single_command = " ".join(args.command).strip()
    if single_command:
        try:
            report = pipeline.run(single_command, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\nALWIN: Interrupted.")
            return EXIT_FAILURE
        print_report(report)
        return EXIT_OK if report.ok else EXIT_FAILURE

    # --------------------------- interactive --------------------------- #
    print(BANNER)
    while True:
        try:
            line = input("\nYou: ")
        except (EOFError, KeyboardInterrupt):
            print()
            print("ALWIN: Goodbye.")
            return EXIT_OK
        text = line.strip()
        if not text:
            continue
        if text.lower() in ("help", "h", "?"):
            print_help_text()
            continue
        try:
            report = pipeline.run(text, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\nALWIN: Interrupted.")
            continue
        if report.status == STATUS_EXIT:
            print("ALWIN: Goodbye.")
            return EXIT_OK
        print_report(report)


if __name__ == "__main__":
    sys.exit(main())
