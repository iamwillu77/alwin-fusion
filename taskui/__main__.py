"""Entry point for the ALWIN FUSION task experience (v0.5).

Launch the desktop interface::

    python3 -m taskui

Useful flags::

    python3 -m taskui --llm            # use the local Ollama model for
                                       # proposals (still re-validated)
    python3 -m taskui --dry-run        # every task runs as a dry run
    python3 -m taskui --check          # headless self-check: no window,
                                       # prints status + history location
    python3 -m taskui --smoke          # scripted headless walkthrough of
                                       # the full lifecycle (no display)

Environment: the same variables as the CLI (``ALWIN_HOME``,
``ALWIN_PERMITTED_ROOTS``, ``ALWIN_STATE_DIR``, ``OLLAMA_MODEL``,
``OLLAMA_BASE_URL``, ``OLLAMA_TIMEOUT``).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from ai_core import __version__
from ai_core.config import Config, ConfigError, load_config
from taskui import presenter
from taskui.history import TaskHistory
from taskui.manager import ManagerError, TaskManager
from taskui.status import collect_status

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m taskui",
        description=(
            "ALWIN FUSION task experience: submit a request, inspect the "
            "real plan, approve it, watch verified execution."
        ),
    )
    parser.add_argument("--llm", action="store_true",
                        help="use the local Ollama model for proposals "
                             "(every action is still re-validated)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and verify without changing anything")
    parser.add_argument("--check", action="store_true",
                        help="headless self-check (no window); prints "
                             "capability status and exits")
    parser.add_argument("--smoke", action="store_true",
                        help="scripted headless walkthrough of the task "
                             "lifecycle (no display; used by "
                             "scripts/manual_smoke_ui.sh)")
    return parser


def _status_for(config: Config, use_llm: bool):
    return collect_status(config.ollama_base_url, config.ollama_model)


def _run_check(config: Config) -> int:  # pragma: no cover - trivial output
    status = _status_for(config, False)
    view = presenter.status_view(status)
    print(f"ALWIN FUSION task experience (v{__version__})")
    print(f"  Local AI:     {view['ai_label']}")
    print(f"                {view['ai_detail']}")
    print(f"  Automation:   {view['automation_label']}")
    print(f"  Offline:      {view['offline_note']}")
    history = TaskHistory(config.state_dir / "task_history.jsonl")
    records, skipped = history.load(limit=5)
    print(f"  History:      {history.path}")
    print(f"                {len(records)} record(s), {skipped} skipped")
    return EXIT_OK


def _run_smoke(config: Config, use_llm: bool, dry_run: bool) -> int:
    """Headless walkthrough: read-only task, then approval flow.

    Runs the *real* manager/pipeline against the (sandboxed) config —
    no display, no desktop manipulation.  Used by the opt-in manual
    smoke test to prove the whole task experience works end to end.
    """
    from taskui.lifecycle import (
        STATE_AWAITING_APPROVAL,
        STATE_COMPLETED,
    )

    manager = TaskManager(config, use_llm=use_llm)
    failures: List[str] = []

    def expect(condition: bool, what: str) -> None:
        print(f"  [{'ok' if condition else 'FAIL'}] {what}")
        if not condition:
            failures.append(what)

    print("Smoke 1/3: read-only task (list Documents)")
    record = manager.submit("Show files and folders in my Documents.")
    manager.wait_terminal(timeout=60)
    view = presenter.task_view(manager.current)
    expect(view["state"] == STATE_COMPLETED,
           f"read-only task completed (state={view['state']})")
    expect(view["tone"] == presenter.TONE_POSITIVE, "positive tone shown")
    if view["error"]:
        print(f"         note: {view['error']}")

    print("Smoke 2/3: mutating task — plan shown, then rejected")
    folder = f"SmokeTestFolder{int(time.time())}"
    record = manager.submit(f"Create a folder named {folder} in Documents.")
    expect(manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=60),
           "task reached awaiting_approval")
    view = presenter.task_view(manager.current)
    expect(view["awaiting_approval"], "approval controls offered")
    expect(bool(view["plan_rows"]), "real plan rows displayed")
    expect(bool(view["approval_digest"]), "plan digest available")
    expect(any(r["badge"] == presenter.BADGE_APPROVAL for r in view["plan_rows"]),
           "mutating plan is badged as needing approval")
    manager.reject()
    manager.wait_terminal(timeout=60)
    view = presenter.task_view(manager.current)
    expect(view["state"] == "cancelled", "rejection cancelled the task")
    expect(not (config.home / "Documents" / folder).exists(),
           "rejected task changed nothing on disk")

    print("Smoke 3/3: mutating task — approved, executed, verified")
    record = manager.submit(f"Create a folder named {folder} in Documents.")
    expect(manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=60),
           "task reached awaiting_approval again")
    view = presenter.task_view(manager.current)
    expect(manager.approve(view["approval_digest"]), "approval accepted")
    manager.wait_terminal(timeout=60)
    view = presenter.task_view(manager.current)
    expect(view["state"] == STATE_COMPLETED,
           f"approved task completed (state={view['state']})")
    expect(bool(view["verification"]), "verification evidence displayed")
    expect((config.home / "Documents" / folder).is_dir(),
           "approved task created the folder (verified on disk)")

    rows = presenter.history_rows(manager.history_rows(limit=10))
    expect(len(rows) >= 3, "history recorded the finished tasks")
    not_replayable = all(
        presenter.history_detail(
            manager.history_detail(row["task_id"])
        ).get("replayable") is False
        for row in rows
    )
    expect(not_replayable, "history records are not replayable")

    if failures:
        print(f"SMOKE FAILED ({len(failures)} check(s))")
        return EXIT_FAILURE
    print("SMOKE OK — task experience works end to end (headless).")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.check:
        return _run_check(config)
    if args.smoke:
        return _run_smoke(config, args.llm, args.dry_run)

    from taskui import gtk_app

    if gtk_app.Gtk is None:
        print(
            "GTK4 (PyGObject with Gtk 4.0) is not available on this "
            "system, so the desktop window cannot open.\n"
            "Install it with your package manager (Debian/Ubuntu: "
            "'sudo apt install python3-gi gir1.2-gtk-4.0') or run the "
            "headless check:  python3 -m taskui --check",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    status = _status_for(config, args.llm)
    manager = TaskManager(config, use_llm=args.llm)
    if args.dry_run:
        original_submit = manager.submit

        def _dry_submit(command, *, dry_run=True):
            return original_submit(command, dry_run=True)

        manager.submit = _dry_submit  # type: ignore[method-assign]
    return gtk_app.run_app(manager, status=status)


if __name__ == "__main__":
    raise SystemExit(main())
