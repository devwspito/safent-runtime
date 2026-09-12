"""Diagnostic model of Podman 6.1.1's documented name -> lock -> Refresh boundary.

No subprocess, network, Podman, VM, or product implementation is exercised.
This demonstrates why a wrapper precheck is insufficient; it does NOT certify
a native exploit or implement a safe stop command. Run this file explicitly.
"""

import threading
import unittest


class NameScopedMachine:
    def __init__(self):
        self.lock = threading.Lock()
        self.identity = "original-generation"
        self.stopped = []

    def inspect(self):
        with self.lock:
            return self.identity

    def recreate(self):
        with self.lock:
            self.identity = "replacement-generation"

    def stop(self, expected_inside_lock=None):
        with self.lock:
            # Model of MachineConfig.Refresh(): current config replaces the
            # earlier identity; current CLI has no expected-identity argument.
            refreshed = self.identity
            if expected_inside_lock is not None and refreshed != expected_inside_lock:
                return "identity_changed"
            self.stopped.append(refreshed)
            return "stopped"


class StopBoundaryReproduction(unittest.TestCase):
    def run_interleaving(self, compare_inside_lock):
        machine = NameScopedMachine()
        approved_identity = machine.inspect()
        checked = threading.Event()
        replaced = threading.Event()
        outcomes = []

        def wrapper():
            if machine.inspect() != approved_identity:
                outcomes.append("precheck_rejected")
                return
            checked.set()
            if not replaced.wait(2):
                outcomes.append("fixture_timeout")
                return
            outcomes.append(machine.stop(approved_identity if compare_inside_lock else None))

        worker = threading.Thread(target=wrapper, daemon=True)
        worker.start()
        self.assertTrue(checked.wait(2), "fixture did not reach the precheck")
        machine.recreate()
        replaced.set()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "fixture thread did not finish")
        return machine, outcomes

    def test_wrapper_precheck_allows_the_replacement_to_be_stopped(self):
        machine, outcomes = self.run_interleaving(compare_inside_lock=False)
        self.assertEqual(outcomes, ["stopped"])
        self.assertEqual(machine.stopped, ["replacement-generation"])

    def test_required_native_compare_inside_lock_would_reject_replacement(self):
        # Reference requirement only: this branch is NOT available in Podman's
        # current CLI and is NOT installed as an alternate Safent provider.
        machine, outcomes = self.run_interleaving(compare_inside_lock=True)
        self.assertEqual(outcomes, ["identity_changed"])
        self.assertEqual(machine.stopped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
