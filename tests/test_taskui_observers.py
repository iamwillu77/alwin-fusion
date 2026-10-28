"""v0.6 task-manager observer tests: the read-only companion event feed.

The companion needs task updates without taking the single ``on_update``
slot (which belongs to the task window).  ``add_observer`` /
``remove_observer`` were added as the smallest backward-compatible event
interface the milestone allows — strictly read-only, error-isolated and
unable to influence any task:

* observers receive the same curated records as ``on_update``,
* an observer exception never affects a task (same rule as ``on_update``),
* an observer's return value is ignored — it can never approve or
  release anything,
* a mutating task still waits for a real approval even when an observer
  "wants" it to proceed,
* duplicates register once; removal stops delivery.
"""

from __future__ import annotations

import unittest

from taskui import presenter
from taskui.lifecycle import STATE_AWAITING_APPROVAL, STATE_COMPLETED
from taskui.manager import TaskManager
from tests.helpers import Sandbox


class ObserverTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def manager(self, **kwargs) -> TaskManager:
        return TaskManager(self.sandbox.config, **kwargs)


class ObserverTests(ObserverTestCase):
    def test_observers_and_on_update_both_receive_records(self):
        manager = self.manager()
        seen_by_observer = []
        seen_by_slot = []
        manager.add_observer(seen_by_observer.append)
        manager.on_update = seen_by_slot.append

        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))

        self.assertTrue(seen_by_observer, "observer received records")
        self.assertTrue(seen_by_slot, "on_update still works (v0.5 slot)")
        self.assertEqual(len(seen_by_observer), len(seen_by_slot),
                         "both subscribers see the same update stream")
        self.assertEqual(seen_by_observer[-1].state, STATE_COMPLETED)
        self.assertEqual(manager.current.state, STATE_COMPLETED)

    def test_observer_exception_does_not_affect_the_task(self):
        manager = self.manager()

        def _broken_observer(_record):
            raise RuntimeError("companion animation bug")

        manager.add_observer(_broken_observer)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(manager.current.state, STATE_COMPLETED,
                         "an observer bug must never break a task")

    def test_observer_return_value_cannot_grant_an_approval(self):
        manager = self.manager()

        def _greedy_observer(_record):
            return True   # "approve!"

        manager.add_observer(_greedy_observer)
        manager.submit("Create a folder named ObserverGate in Documents.")
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10),
                        "a truthy observer return did NOT approve anything")
        # The real approval flow still applies: bind to the displayed plan.
        view = presenter.task_view(manager.current)
        self.assertTrue(manager.approve(view["approval_digest"]))
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(manager.current.state, STATE_COMPLETED)
        self.assertTrue(
            (self.sandbox.config.home / "Documents" / "ObserverGate").is_dir()
        )

    def test_duplicate_observers_register_once(self):
        manager = self.manager()
        calls = []
        on_update_calls = []
        manager.add_observer(calls.append)
        manager.add_observer(calls.append)      # duplicate: ignored
        manager.on_update = on_update_calls.append

        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))

        self.assertEqual(len(on_update_calls), len(calls),
                         "one registration means one delivery per update")

    def test_remove_observer_stops_delivery(self):
        manager = self.manager()
        seen = []
        callback = seen.append
        manager.add_observer(callback)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertTrue(seen)

        manager.remove_observer(callback)
        count_after_removal = len(seen)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(len(seen), count_after_removal,
                         "removed observers get nothing")

    def test_removing_an_unknown_observer_is_safe(self):
        manager = self.manager()
        manager.remove_observer(lambda _r: None)   # never added: no error

    def test_adding_none_is_ignored(self):
        manager = self.manager()
        manager.add_observer(None)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(manager.current.state, STATE_COMPLETED)

    def test_the_single_slot_semantics_are_unchanged(self):
        # v0.5 behaviour: a second assignment to on_update replaces the
        # first; observers do not change that contract.
        manager = self.manager()
        first, second = [], []
        manager.on_update = first.append
        manager.on_update = second.append
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertFalse(first)
        self.assertTrue(second)

    def test_observers_see_only_the_curated_record(self):
        manager = self.manager()
        seen = []
        manager.add_observer(seen.append)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        record = seen[-1]
        # The same public, curated interface the task window renders.
        self.assertEqual(record.state, manager.current.state)
        self.assertTrue(hasattr(record, "is_terminal"))


if __name__ == "__main__":
    unittest.main()
