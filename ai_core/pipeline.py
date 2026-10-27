"""Task pipeline: parse -> plan -> permission -> approval -> execute
-> verify -> audit.

Each stage is a separate object with a single responsibility so the
system can evolve (an MCP integration or a GUI can reuse the same
pipeline).  Nothing in this module runs shell commands or trusts model
output: model proposals re-enter through ``planner.validate_action``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ai_core.audit import AuditError, AuditLogger
from ai_core.command_parser import EXIT, UNSUPPORTED, parse_command
from ai_core.config import Config
from ai_core.executor import ExecutionResult, Executor
from ai_core.permissions import PermissionChecker
from ai_core.planner import (
    MUTATING_ACTIONS,
    PlanValidationError,
    describe_action,
    plan_from_intent,
    plan_via_llm,
)
from ai_core.verifier import Verifier
from integrations import build_default_dispatcher

STATUS_SUCCESS = "success"
STATUS_DRY_RUN = "dry_run"
STATUS_REJECTED = "rejected"
STATUS_DENIED = "denied"
STATUS_UNSUPPORTED = "unsupported"
STATUS_FAILED = "failed"
STATUS_EXIT = "exit"

#: statuses that count as "the user got what they asked for"
OK_STATUSES = (STATUS_SUCCESS, STATUS_DRY_RUN, STATUS_EXIT)


@dataclass
class TaskReport:
    """Full result of one pipeline run."""

    task_id: str
    status: str
    command: str
    parser: str = "deterministic"
    dry_run: bool = False
    action: Optional[dict] = None
    proposal: Optional[str] = None
    permission: Optional[dict] = None
    approval: str = "not_required"
    execution: Optional[ExecutionResult] = None
    verification: Optional[dict] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


class TaskPipeline:
    """Runs one natural-language command through all trusted stages."""

    def __init__(
        self,
        config: Config,
        *,
        checker: Optional[PermissionChecker] = None,
        dispatcher=None,
        executor: Optional[Executor] = None,
        verifier: Optional[Verifier] = None,
        audit: Optional[AuditLogger] = None,
        approver: Optional[Callable[[str], bool]] = None,
        on_proposal: Optional[Callable[[str], None]] = None,
        use_llm: bool = False,
        llm_client=None,
    ) -> None:
        self.config = config
        self.checker = checker or PermissionChecker(config)
        try:
            self.default_location = config.permitted_roots[0].relative_to(
                config.home
            ).as_posix()
        except ValueError:  # pragma: no cover - config validation prevents this
            self.default_location = ""
        self.dispatcher = dispatcher or build_default_dispatcher(self.checker)
        self.executor = executor or Executor(self.dispatcher, self.checker)
        self.verifier = verifier or Verifier(self.checker)
        self.audit = audit or AuditLogger(config.audit_path)
        self.approver = approver or (lambda _description: False)
        self.on_proposal = on_proposal or (lambda _description: None)
        self.use_llm = use_llm
        self.llm_client = llm_client

    # ------------------------------------------------------------------ #

    @staticmethod
    def _new_task_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _mask_command(command: str, action: Optional[dict]) -> str:
        """Remove user-provided file content from the logged command."""
        content = (action or {}).get("content")
        if not isinstance(content, str) or not content:
            return command
        if content in command:
            return command.replace(content, f"<content redacted: {len(content)} chars>")
        # The parser collapses whitespace, so match a collapsed copy too.
        collapsed = " ".join(command.split())
        if content in collapsed:
            return collapsed.replace(
                content, f"<content redacted: {len(content)} chars>"
            )
        return "<command redacted: content could not be located safely>"

    def _record_audit(self, report: TaskReport, command: str) -> None:
        execution = report.execution.as_dict() if report.execution else None
        extra = {"status": report.status}
        if report.notes:
            extra["notes"] = list(report.notes)
        logged_command = self._mask_command(command, report.action)
        try:
            self.audit.record(
                task_id=report.task_id,
                command=logged_command,
                parser=report.parser,
                dry_run=report.dry_run,
                action=report.action,
                permission=report.permission,
                approval=report.approval,
                execution=execution,
                verification=report.verification,
                error=report.error,
                extra=extra,
            )
        except AuditError as exc:
            # Never hide an audit failure, but do not corrupt the real task
            # result either: the failure is surfaced as a report note.
            report.notes.append(f"Audit log write failed: {exc}")

    # ------------------------------------------------------------------ #

    def _plan(self, command: str, report: TaskReport):
        """Return a validated action or (status, error) for the report."""
        intent = parse_command(command)

        if intent.intent == EXIT:
            return None, STATUS_EXIT, None

        # Optional local-model proposal.  It is validated exactly like the
        # deterministic plan; on any problem we fall back to the
        # deterministic parser (never to a cloud service).
        if self.use_llm and self.llm_client is not None:
            try:
                llm_action = plan_via_llm(command, self.llm_client)
            except Exception as exc:  # noqa: BLE001 - any LLM issue => fallback
                report.parser = "deterministic"
                report.notes.append(
                    f"Local AI runtime unavailable or unusable "
                    f"({type(exc).__name__}: {exc}); used the built-in parser instead."
                )
            else:
                report.parser = f"llm:{getattr(self.llm_client, 'model', 'unknown')}"
                return llm_action, None, None

        # Deterministic plan: always available offline.
        if intent.intent == UNSUPPORTED:
            return (
                None,
                STATUS_UNSUPPORTED,
                intent.slots.get("reason", "Unsupported command."),
            )
        try:
            action = plan_from_intent(intent, default_location=self.default_location)
        except PlanValidationError as exc:
            return None, STATUS_UNSUPPORTED, str(exc)
        return action, None, None

    def run(self, command: str, *, dry_run: bool = False) -> TaskReport:
        """Execute the full pipeline for *command*."""
        task_id = self._new_task_id()
        clean_command = (command or "").strip()
        report = TaskReport(
            task_id=task_id, status=STATUS_FAILED, command=command or "", dry_run=dry_run
        )

        action, early_status, early_error = self._plan(clean_command, report)
        report.action = action

        if early_status == STATUS_EXIT:
            report.status = STATUS_EXIT
            return report

        if early_status is not None:
            report.status = early_status
            report.error = early_error
            self._record_audit(report, clean_command)
            return report

        # ---- Stage 3: permission check -------------------------------- #
        decision = self.checker.check_action(action)
        report.permission = decision.as_dict()

        is_explain = action["action"] == "explain"
        is_mutating = action["action"] in MUTATING_ACTIONS

        if not decision.allowed and not is_explain:
            report.status = STATUS_DENIED
            report.error = decision.reason
            self._record_audit(report, clean_command)
            return report

        if is_explain:
            report.proposal = describe_action(action, home=self.checker.home)
        elif decision.resolved is not None:
            report.proposal = describe_action(
                action, resolved=decision.resolved, home=self.checker.home
            )
        else:
            report.proposal = describe_action(action, home=self.checker.home)

        # ---- Stage 4: approval (only for mutating actions) ------------- #
        if is_mutating:
            self.on_proposal(report.proposal)
            if dry_run:
                report.approval = "dry_run"
            else:
                try:
                    approved = bool(self.approver(report.proposal))
                except Exception:  # noqa: BLE001 - interactive input failure = no
                    approved = False
                report.approval = "approved" if approved else "rejected"
                if not approved:
                    report.status = STATUS_REJECTED
                    report.error = "User rejected the proposed action."
                    self._record_audit(report, clean_command)
                    return report

        # ---- Stage 5: execution ---------------------------------------- #
        execution = self.executor.execute(action, dry_run=dry_run)
        report.execution = execution

        # ---- Stage 6: verification ------------------------------------- #
        verification = self.verifier.verify(action, execution)
        report.verification = verification.as_dict()

        if not execution.ok:
            report.status = STATUS_FAILED
            report.error = execution.error or execution.message or "Execution failed."
        elif verification.status == "failed":
            report.status = STATUS_FAILED
            report.error = verification.message
        elif dry_run:
            report.status = STATUS_DRY_RUN
        else:
            report.status = STATUS_SUCCESS

        # ---- Stage 7: audit -------------------------------------------- #
        self._record_audit(report, clean_command)
        return report
