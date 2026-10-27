"""Execution stage.

The executor takes a *validated* action and routes it to a trusted
handler through the :class:`integrations.ActionDispatcher`.  It never
builds shell strings, never evaluates code and always reports the real
error when a handler fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from ai_core.permissions import PermissionChecker, pretty_path
from ai_core.planner import describe_action


@dataclass
class ExecutionResult:
    """Outcome of the execution stage."""

    ok: bool
    executed: bool
    dry_run: bool
    message: str
    error: Optional[str] = None
    data: Dict = field(default_factory=dict)

    def as_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "executed": self.executed,
            "dry_run": self.dry_run,
            "message": self.message,
            "error": self.error,
        }


class Executor:
    """Executes validated actions through the dispatcher."""

    def __init__(self, dispatcher, checker: PermissionChecker) -> None:
        self.dispatcher = dispatcher
        self.checker = checker

    # ------------------------------------------------------------------ #

    def _explain(self, action: dict, *, dry_run: bool) -> ExecutionResult:
        target = action.get("target") or {}
        decision = self.checker.check_action(target)
        description = describe_action(
            target,
            resolved=decision.resolved,
            home=self.checker.home,
        )
        if decision.allowed:
            status = "This operation is permitted by the current configuration."
        else:
            status = (
                "This operation would be denied by the current configuration: "
                f"{decision.reason}"
            )
        explanation = f"Proposed operation: {description}\n{status}"
        return ExecutionResult(
            ok=True,
            executed=True,
            dry_run=dry_run,
            message="Explanation produced.",
            data={
                "explanation": explanation,
                "target": target,
                "allowed": decision.allowed,
            },
        )

    def execute(self, action: dict, *, dry_run: bool = False) -> ExecutionResult:
        """Run *action*; never raises for handler failures."""
        name = action.get("action")
        if name == "explain":
            return self._explain(action, dry_run=dry_run)

        try:
            result = self.dispatcher.dispatch(action, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - real errors are reported, not hidden
            return ExecutionResult(
                ok=False,
                executed=False,
                dry_run=dry_run,
                message="",
                error=f"{type(exc).__name__}: {exc}",
            )

        return ExecutionResult(
            ok=result.ok,
            executed=result.ok and not dry_run,
            dry_run=dry_run,
            message=result.message,
            error=result.error,
            data=dict(result.data),
        )
