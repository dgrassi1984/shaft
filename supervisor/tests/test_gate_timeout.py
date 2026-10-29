"""Gate entry points share the configurable stage timeout."""
import inspect
import unittest

from supervisor import core, gate


class GateTimeoutTest(unittest.TestCase):
    def test_entry_points_default_to_the_gate_timeout(self):
        for function in (gate.warm, gate.run_tests, gate.run_instance_tests):
            with self.subTest(function=function.__name__):
                self.assertEqual(inspect.signature(function).parameters["timeout_s"].default,
                                 gate.GATE_TIMEOUT_S)

    def test_knob_defaults_to_300_seconds(self):
        """The operator's limit (2026-10-29): five minutes a stage. `shaft gate budget` keeps each part within half."""
        knob = core.KNOBS["SHAFT_GATE_TIMEOUT_S"]
        self.assertEqual(knob.parse(knob.default), 300)


if __name__ == "__main__":
    unittest.main()
