"""shaft gate budget: the check before a merge."""
import unittest
from pathlib import Path
from unittest import mock

from supervisor import gate, gate_suite

VAR = gate_suite.UNDER_TEST   # spelled through gate_suite: a module naming it is in the instance part (gate_suite.Split)


class BudgetTest(unittest.TestCase):
    def run_budget(self, seconds: dict[str, float], ok: bool = True, stderr: str = "") -> tuple[int, list[dict]]:
        calls = []

        def part(name, tree, env, timeout_s, label):
            calls.append({"part": name, "env": env, "timeout_s": timeout_s})
            return ok, "section", stderr, 10, seconds[name]
        with mock.patch.object(gate, "committed_tree", return_value=(Path("/tree"), "abc")), \
                mock.patch.object(gate, "_part", side_effect=part), mock.patch("builtins.print"):
            return gate.budget(timeout_s=300), calls

    def test_the_template_runs_as_the_template_not_as_an_instance(self):
        """Codex review: with the instance-under-test variable set to the template, the invariants skipped the template's own cases
        (every CONFIDENCE_MODE, TemplateMeasurement) that the plain suite ran."""
        with mock.patch.dict("os.environ", {VAR: "/somewhere"}):
            code, calls = self.run_budget({"instance": 1, "shared": 1})
        self.assertEqual(code, 0)
        self.assertEqual([c["part"] for c in calls], ["instance", "shared"])
        self.assertTrue(all(VAR not in c["env"] for c in calls))

    def test_a_part_over_half_the_stage_limit_is_over_budget(self):
        self.assertEqual(self.run_budget({"instance": 1, "shared": 151})[0], 1)
        self.assertEqual(self.run_budget({"instance": 1, "shared": 149})[0], 0)

    def test_a_slow_test_a_twice_run_test_or_a_failure_is_over_budget(self):
        slow = gate_suite.TIMES + '{"seconds": 1, "slowest_tests": [["t (m.C.t)", 10.5]], "run_twice": []}'
        twice = gate_suite.TIMES + '{"seconds": 1, "slowest_tests": [], "run_twice": ["t (m.C.t)"]}'
        self.assertEqual(self.run_budget({"instance": 1, "shared": 1}, stderr=slow)[0], 1)
        self.assertEqual(self.run_budget({"instance": 1, "shared": 1}, stderr=twice)[0], 1)
        self.assertEqual(self.run_budget({"instance": 1, "shared": 1}, ok=False)[0], 1)


if __name__ == "__main__":
    unittest.main()
