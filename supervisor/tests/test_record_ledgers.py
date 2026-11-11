"""Record's declared kinds and dispatch preserve paging and replay cutoffs."""
import importlib
from unittest import mock

from supervisor import battery, broker, jobs
from supervisor.tests.test_idle_exit import IsolatedCase, NOW


class RecordLedgersTest(IsolatedCase):
    def test_each_ledger_dispatches_limit_as_of_and_facts_offset(self):
        self.assertEqual(set(broker.LEDGER_KINDS), {"families", "habits", "usage", "facts", "predictions", "unrecorded_orders"})
        for replay in (False, True):
            b = self.box(replay=replay)
            for kind, module_name in broker.LEDGER_KINDS.items():
                with self.subTest(kind=kind, replay=replay):
                    module = importlib.import_module("supervisor." + module_name)
                    with mock.patch.object(module, "record_text", return_value="example record") as record:
                        result = b.call("record", {"kind": kind, "limit": 7, "offset": 3})
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["result"], "example record")
                    more = {"offset": 3} if kind == "facts" else {"extra": []} if kind == "predictions" else {}
                    record.assert_called_once_with("ember", limit=7, as_of=NOW if replay else None, **more)

    def test_unknown_kind_error_names_all_declared_kinds(self):
        result = self.box().call("record", {"kind": "unknown"})
        self.assertFalse(result["ok"], result)
        kinds = broker.SPECS["record"]["function"]["parameters"]["properties"]["kind"]["enum"]
        for kind in kinds:
            self.assertIn(kind, result["error"])

    def test_commitment_source_stays_accepted_though_not_offered(self):
        """`commitment_source` is the instance code's own lookup (its commit_to), older than the ledgers: the handler
        answers it, and the model's list of kinds does not offer it. Adding the ledger kinds must not change either."""
        result = self.box().call("record", {"kind": "commitment_source", "cycle": ""})
        self.assertTrue(result["ok"], result)
        kinds = broker.SPECS["record"]["function"]["parameters"]["properties"]["kind"]["enum"]
        self.assertNotIn("commitment_source", kinds)

    def test_every_spec_kind_is_accepted_on_empty_record(self):
        kinds = broker.SPECS["record"]["function"]["parameters"]["properties"]["kind"]["enum"]
        self.assertEqual(set(kinds), {"judgments", "runs", "tools", "code_changes", "trials", "egress", "jobs",
            "consults", "calendar", "gate", "expectations"} | set(broker.LEDGER_KINDS))
        # All declared kinds read empty fixture records.
        with mock.patch.object(jobs, "declared", return_value=([], [])), \
             mock.patch.object(jobs, "uncommitted", return_value=set()):
            for kind in kinds:
                with self.subTest(kind=kind):
                    result = self.box().call("record", {"kind": kind})
                    self.assertNotIn("kind must be", result.get("error", ""))
                    self.assertTrue(result["ok"], result)

    def test_gauges_idle_exit_and_predict_are_off_in_exams_and_jobs(self):
        for tool in ("gauges", "nothing_to_do", "predict"):
            self.assertIn(tool, battery.EXAM_OFF)
            self.assertIn(tool, jobs.OFF_IN_JOBS)
