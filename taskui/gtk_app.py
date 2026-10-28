"""GTK4 task window (v0.5) — a *thin view* over the presenter.

Responsibilities (and nothing more):

* render presenter dictionaries,
* forward user intents (submit / approve / reject / cancel, history
  selection) to the :class:`~taskui.manager.TaskManager`,
* marshal worker-thread updates onto the GTK main loop with
  ``GLib.idle_add``.

Security-relevant properties of this layer:

* it never executes anything itself — no subprocess, no code
  evaluation, no file writes; every intent goes through the manager,
  which goes through the trusted pipeline,
* **all** dynamic text (requests, model proposals, application text,
  window titles, history records, error messages) is inserted with
  ``set_text``/``set_label`` — never ``set_markup`` — so untrusted
  content can never inject Pango markup or look like UI chrome,
* approval buttons appear only when the presenter says a task is
  awaiting approval, and they approve the exact displayed digest; the
  pipeline independently re-checks everything anyway.

The window is intentionally plain GTK4 (no libadwaita dependency, no
animations): startup and idle cost stay low on a small machine.
"""

from __future__ import annotations

import sys
import traceback
from typing import Callable, Optional

try:  # pragma: no cover - exercised only in the opt-in manual smoke
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError):  # pragma: no cover - headless fallback
    Gtk = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]

from taskui import presenter
from taskui.manager import ManagerError, TaskManager
from taskui.status import SystemStatus

APP_TITLE = "ALWIN FUSION"

#: Light, friendly palette (avoiding a dark-only design).
CSS = b"""
window { background: #f6f7f9; }
.app-title { font-size: 16px; font-weight: 700; color: #1c2733; }
.section-title { font-size: 13px; font-weight: 700; color: #44515f;
                 margin-top: 10px; }
.muted { color: #6b7684; font-size: 12px; }
.request { font-size: 13px; color: #1c2733; }
.error-text { color: #a4272d; font-size: 12px; }
.plan-summary { font-size: 12px; color: #33404e; }
.tone-positive { color: #17663a; }
.tone-warning { color: #8a5a00; }
.tone-negative { color: #a4272d; }
.tone-neutral { color: #44515f; }
.badge { font-size: 11px; font-weight: 700; padding: 1px 6px;
         border-radius: 8px; background: #e3e8ee; color: #33404e; }
.badge-approval { background: #ffe8cc; color: #7a4a00; }
.badge-read_only { background: #dff0e4; color: #17663a; }
.badge-blocked, .badge-unsupported { background: #fadfe0; color: #a4272d; }
.badge-unverifiable { background: #e6e2f5; color: #4a3a8a; }
.pill { font-size: 12px; padding: 2px 10px; border-radius: 12px;
        background: #e3e8ee; color: #33404e; margin-left: 6px; }
.entry { background: #ffffff; }
"""

TONES = {
    presenter.TONE_POSITIVE: "tone-positive",
    presenter.TONE_WARNING: "tone-warning",
    presenter.TONE_NEGATIVE: "tone-negative",
    presenter.TONE_NEUTRAL: "tone-neutral",
}


def _label(text: str, css: str = "") -> "Gtk.Label":
    """A plain-text label.  Dynamic content is NEVER markup."""
    label = Gtk.Label(label=text or "")
    label.set_xalign(0.0)
    label.set_wrap(True)
    label.set_selectable(False)
    if css:
        for name in css.split():
            label.add_css_class(name)
    return label


class TaskWindow:
    """Main window: task area, plan, execution panel, history."""

    def __init__(
        self,
        manager: TaskManager,
        *,
        status: Optional[SystemStatus] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        if Gtk is None:  # pragma: no cover - defensive
            raise RuntimeError(
                "GTK4 (PyGObject with Gtk 4.0) is required for the UI."
            )
        self.manager = manager
        self.on_quit = on_quit

        self.app = Gtk.Application(application_id="org.alwin.fusion.taskui")
        self.app.connect("activate", self._on_activate)
        self.window: Optional[Gtk.ApplicationWindow] = None

        self._status = status
        self._plan_digest = ""
        self._updating = False  # suppress history-activate recursion

        # The manager calls on_update from the *worker* thread; every
        # update is marshalled onto the GTK main loop.
        manager.on_update = self._queue_update

    # ------------------------------------------------------------------ #
    # Window construction
    # ------------------------------------------------------------------ #

    def _on_activate(self, app) -> None:  # pragma: no cover - needs display
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = app.get_default_for_display() if hasattr(
            app, "get_default_for_display"
        ) else None
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        window = Gtk.ApplicationWindow(application=app)
        window.set_title(APP_TITLE)
        window.set_default_size(880, 680)
        self.window = window

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(14)
        root.set_margin_end(14)
        window.set_child(root)

        # ---- Header: title + capability pills ----------------------- #
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(_label(APP_TITLE, "app-title"))
        self._ai_pill = _label("", "pill")
        self._automation_pill = _label("", "pill")
        header.append(self._ai_pill)
        header.append(self._automation_pill)
        offline = _label("Works fully offline", "pill")
        header.append(offline)
        root.append(header)

        status_detail = _label("", "muted")
        self._status_detail = status_detail
        root.append(status_detail)

        # ---- Task input --------------------------------------------- #
        root.append(_label("What should Alwin do?", "section-title"))
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._entry = Gtk.Entry()
        self._entry.set_hexpand(True)
        self._entry.set_placeholder_text(
            "e.g. Create a pdf named summary.pdf in Documents using report.json"
        )
        self._entry.add_css_class("entry")
        self._entry.connect("activate", self._on_submit)
        input_row.append(self._entry)
        self._submit_btn = Gtk.Button(label="Submit")
        self._submit_btn.add_css_class("suggested-action")
        self._submit_btn.connect("clicked", self._on_submit)
        input_row.append(self._submit_btn)
        self._cancel_btn = Gtk.Button(label="Cancel task")
        self._cancel_btn.connect("clicked", self._on_cancel)
        input_row.append(self._cancel_btn)
        root.append(input_row)

        self._intake_error = _label("", "error-text")
        root.append(self._intake_error)

        # ---- Plan panel --------------------------------------------- #
        root.append(_label("Task plan", "section-title"))
        self._state_label = _label("No task yet — submit a request above.",
                                   "muted")
        root.append(self._state_label)
        plan_scroll = Gtk.ScrolledWindow()
        plan_scroll.set_vexpand(True)
        plan_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._plan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=4)
        plan_scroll.set_child(self._plan_box)
        root.append(plan_scroll)

        # ---- Approval controls -------------------------------------- #
        approval_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=8)
        self._approve_btn = Gtk.Button(label="Approve this plan")
        self._approve_btn.add_css_class("suggested-action")
        self._approve_btn.connect("clicked", self._on_approve)
        self._reject_btn = Gtk.Button(label="Reject")
        self._reject_btn.connect("clicked", self._on_reject)
        approval_row.append(self._approve_btn)
        approval_row.append(self._reject_btn)
        self._approval_row = approval_row
        root.append(approval_row)

        # ---- Execution panel ---------------------------------------- #
        root.append(_label("Execution", "section-title"))
        exec_scroll = Gtk.ScrolledWindow()
        exec_scroll.set_vexpand(True)
        exec_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._exec_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=4)
        exec_scroll.set_child(self._exec_box)
        root.append(exec_scroll)

        # ---- History ------------------------------------------------- #
        root.append(_label("History (local, display-only)", "section-title"))
        history_scroll = Gtk.ScrolledWindow()
        history_scroll.set_min_content_height(120)
        history_scroll.set_vexpand(False)
        history_scroll.set_policy(Gtk.PolicyType.NEVER,
                                  Gtk.PolicyType.AUTOMATIC)
        self._history_list = Gtk.ListBox()
        self._history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._history_list.connect("row-selected", self._on_history_selected)
        history_scroll.set_child(self._history_list)
        root.append(history_scroll)

        self._history_detail = _label("Select a past task to inspect its "
                                      "recorded result.", "muted")
        self._history_detail.set_selectable(True)
        root.append(self._history_detail)

        self._render_status()
        self._refresh_history()
        window.present()

    # ------------------------------------------------------------------ #
    # Intents (UI thread -> manager)
    # ------------------------------------------------------------------ #

    def _on_submit(self, *_args) -> None:  # pragma: no cover - needs display
        text = self._entry.get_text()
        error = presenter.validate_submission(text, busy=self.manager.busy)
        if error:
            self._intake_error.set_text(error)
            return
        self._intake_error.set_text("")
        try:
            self.manager.submit(text)
        except ManagerError as exc:
            self._intake_error.set_text(str(exc))
        self._entry.set_text("")
        self._render_task()

    def _on_approve(self, *_args) -> None:  # pragma: no cover - display
        if not self.manager.approve(self._plan_digest or None):
            self._intake_error.set_text(
                "The plan could not be approved (it may have changed); "
                "nothing was executed."
            )

    def _on_reject(self, *_args) -> None:  # pragma: no cover - display
        self.manager.reject()

    def _on_cancel(self, *_args) -> None:  # pragma: no cover - display
        if not self.manager.cancel():
            record = self.manager.current
            reason = ""
            if record and record.notes:
                reason = record.notes[-1]
            self._intake_error.set_text(
                ("Cannot cancel now — " + reason)
                if reason
                else "Cannot cancel this task in its current state."
            )

    def _on_history_selected(self, _list, row) -> None:  # pragma: no cover
        if row is None or self._updating:
            return
        task_id = row.get_child().get_text().split(" ")[0].strip()
        detail = self.manager.history_detail(task_id)
        view = presenter.history_detail(detail)
        if not view.get("present"):
            self._history_detail.set_text(
                "That history record could not be read (it was skipped as "
                "invalid). Nothing is executed from history."
            )
            return
        lines = [
            f"{view['state_label']}  ·  {view['finished_at']}",
            f"Request: {view['request']}",
            f"Plan: {view['plan_action']} — {view['plan_summary']}",
            f"Approval: {view['approval']}",
            f"Verification: {view['verification_label']} "
            f"{view['verification_message']}".rstrip(),
        ]
        if view["error"]:
            lines.append(f"Error: {view['error']}")
        for step in view["steps"]:
            lines.append(
                f"  {step['mark']} Step {step['index']}: {step['action']} — "
                f"{step['label']}"
            )
        lines.append("(History is display-only; tasks are never replayed.)")
        self._history_detail.set_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Worker-thread marshalling
    # ------------------------------------------------------------------ #

    def _queue_update(self, record) -> None:  # pragma: no cover - display
        GLib.idle_add(self._render_task)

    def _render_task(self) -> bool:  # pragma: no cover - needs display
        view = presenter.task_view(self.manager.current, busy=self.manager.busy)
        if not view.get("present"):
            self._state_label.set_text("No task yet — submit a request above.")
            self._clear_box(self._plan_box)
            self._clear_box(self._exec_box)
            self._set_approval_visible(False)
            return False

        tone = TONES.get(view["tone"], "tone-neutral")
        self._state_label.set_text(f"{view['state_label']}   ·   "
                                   f"#{view['task_id']}")
        for css in list(TONES.values()):
            self._state_label.remove_css_class(css)
        self._state_label.add_css_class(tone)

        # ---- Plan rows ---- #
        self._clear_box(self._plan_box)
        if not view["plan_rows"]:
            self._plan_box.append(
                _label(view["error"] or "No plan yet.", "muted")
            )
        for row in view["plan_rows"]:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.append(_label(f"{row['index']}.", "muted"))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.append(_label(row["summary"] or row["action"], "plan-summary"))
            if row["target"]:
                text.append(_label(f"Target: {row['target']}", "muted"))
            box.append(text)
            badge = _label(row["badge_label"].split("—")[0].strip(),
                           f"badge badge-{row['badge']}")
            box.append(badge)
            self._plan_box.append(box)
        for note in view["risk_notes"]:
            self._plan_box.append(_label("• " + note, "muted"))

        # ---- Approval controls ---- #
        self._plan_digest = view["approval_digest"] if view["awaiting_approval"] else ""
        self._set_approval_visible(view["awaiting_approval"])

        # ---- Execution panel ---- #
        self._clear_box(self._exec_box)
        if view["steps"]:
            for step in view["steps"]:
                err = f"   ({step['error']})" if step["error"] else ""
                self._exec_box.append(
                    _label(
                        f"{step['mark']} Step {step['index']}: "
                        f"{step['action']} — {step['label']}{err}",
                        TONES.get(view["tone"], "tone-neutral")
                        if step["status"] in ("failed", "denied") else "",
                    )
                )
        if view["result_message"]:
            self._exec_box.append(_label(view["result_message"], "muted"))
        if view["verification"]:
            v = view["verification"]
            self._exec_box.append(
                _label(
                    f"{v['mark']} {v['label']}"
                    + (f": {v['message']}" if v["message"] else ""),
                    "tone-positive" if v["status"] == "passed"
                    else ("tone-negative" if v["status"] == "failed"
                          else "muted"),
                )
            )
            for check in v["checks"]:
                self._exec_box.append(
                    _label(f"    {check['mark']} {check['name']}"
                           + (f" — {check['detail']}" if check["detail"] else ""),
                           "muted")
                )
        if view["error"]:
            self._exec_box.append(_label("Error: " + view["error"],
                                         "error-text"))
        for note in view["notes"]:
            self._exec_box.append(_label("• " + note, "muted"))
        if not view["steps"] and not view["result_message"] \
                and not view["error"]:
            self._exec_box.append(_label("Nothing has run yet.", "muted"))
        return False

    def _set_approval_visible(self, visible: bool) -> None:  # pragma: no cover
        self._approve_btn.set_visible(visible)
        self._reject_btn.set_visible(visible)

    @staticmethod
    def _clear_box(box) -> None:  # pragma: no cover - needs display
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    # ------------------------------------------------------------------ #
    # Status + history rendering
    # ------------------------------------------------------------------ #

    def _render_status(self) -> None:  # pragma: no cover - needs display
        view = presenter.status_view(self._status)
        self._ai_pill.set_text(view["ai_label"])
        self._automation_pill.set_text(view["automation_label"])
        detail = view["ai_detail"] or view["offline_note"]
        self._status_detail.set_text(detail)

    def _refresh_history(self) -> None:  # pragma: no cover - needs display
        self._updating = True
        try:
            child = self._history_list.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self._history_list.remove(child)
                child = nxt
            rows = presenter.history_rows(self.manager.history_rows(limit=20))
            if not rows:
                self._history_list.append(
                    _label("No finished tasks yet.", "muted")
                )
            for row in rows:
                state = row["state"]
                text = f"{row['task_id']}   {row['finished_at']}   " \
                       f"{row['state_label']}   {row['request']}"
                label = _label(text, "request")
                rowbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                 spacing=4)
                rowbox.append(label)
                self._history_list.append(rowbox)
        finally:
            self._updating = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> int:  # pragma: no cover - needs display
        return self.app.run(None)

    def refresh_history(self) -> None:  # pragma: no cover - needs display
        self._refresh_history()


def run_app(manager: TaskManager, *, status: Optional[SystemStatus] = None
            ) -> int:  # pragma: no cover - needs display
    """Create the window and run the GTK main loop."""
    win = TaskWindow(manager, status=status)
    code = win.run()
    return int(code) if code is not None else 0
