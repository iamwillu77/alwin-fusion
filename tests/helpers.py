"""Shared test fixtures.

Every test runs inside a temporary sandbox (own home directory, own
permitted root, own audit state directory) so nothing here can ever
touch the developer's real home directory.
"""

from __future__ import annotations

import pathlib

from ai_core.config import Config, load_config
from ai_core.pipeline import TaskPipeline


class Sandbox:
    """Temporary home/Documents + state directory for one test."""

    def __init__(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="alwin-fusion-test-")
        self.root = pathlib.Path(self._tmp.name)
        self.home = self.root / "home"
        self.documents = self.home / "Documents"
        self.documents.mkdir(parents=True)
        self.state = self.root / "state"
        self.config: Config = load_config(
            home=self.home, roots=["Documents"], state_dir=self.state
        )

    def pipeline(self, *, approve: bool = True, **kwargs) -> TaskPipeline:
        return TaskPipeline(
            self.config,
            approver=lambda _description: approve,
            **kwargs,
        )

    def cleanup(self) -> None:
        self._tmp.cleanup()

    # Convenience ------------------------------------------------------- #
    def doc_path(self, *parts) -> pathlib.Path:
        return self.documents.joinpath(*parts)


class UnapprovedPipeline(TaskPipeline):
    """Pipeline whose approver fails the test if it is ever called."""

    def __init__(self, config, **kwargs) -> None:
        super().__init__(
            config,
            approver=lambda _d: (_ for _ in ()).throw(
                AssertionError("approver must not be called for dry runs")
            ),
            **kwargs,
        )
