"""Saved experiment results through the paired web routes, with no real instance data."""
import json
import threading
import time
from unittest import mock

from supervisor import battery, experiments, privileged, trial, web, web_experiments
from supervisor.tests.test_web import WebTest


class ExperimentsTest(WebTest):
    def setUp(self):
        super().setUp()
        for module in (experiments, privileged, battery, trial, web_experiments):
            patch = mock.patch.object(module, "SUP_DIR", self.sup)
            patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch.object(trial, "LEDGER", self.sup / "trial_usage.jsonl"),
                      mock.patch.object(web, "_names", return_value=["empty", "wren"])):
            patch.start()
            self.addCleanup(patch.stop)
        web_experiments._cache.clear()
        self.cookie = self.paired()

    def write(self, rel, rows):
        path = self.sup / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def page(self, path="/experiments?i=wren"):
        response = self.req(path, cookie=self.cookie)
        self.assertEqual(response.status, 200, response.body)
        return response.body.decode()

    def test_all_named_experiments_have_a_status_and_not_run(self):
        body = self.page()
        for title in ("Cards", "Attention", "Commitments", "Forecast", "Workspace", "Monitors", "Choice blindness",
                      "Resource shock", "Privileged prediction", "Recognition", "Battery perturbation"):
            self.assertIn(f"<h2>{title}</h2>", body)
        self.assertEqual(body.count("Status: not run"), 11)
        self.assertIn('href="/experiments"', body)
        self.assertIn('data-poll="/fragment/experiments?i=wren"', body)
        self.assertIn("shaft experiment workspace wren --report", body)
        self.assertNotIn("Queue", body)

    def test_raising_report_is_escaped_and_other_cards_render(self):
        with mock.patch.object(experiments, "workspace_report", side_effect=ValueError("<bad> & broken")):
            body = self.page()
        self.assertIn("Error: ValueError: &lt;bad&gt; &amp; broken", body)
        self.assertIn("no attention experiment queued", body)
        self.assertEqual(body.count("Status:"), 11)

    def test_report_text_is_escaped(self):
        with mock.patch.object(experiments, "workspace_report", return_value="<script>alert('x')</script> & text"):
            body = self.page()
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&amp; text", body)
        self.assertNotIn("<script>alert", body)

    def test_routes_require_pairing(self):
        for path in ("/experiments", "/fragment/experiments", "/trials"):
            response = self.req(path)
            self.assertEqual(response.status, 403)
            self.assertIn(b"shaft web pair", response.body)

    def test_latest_knob_trials_progress_cost_links_and_default_instance(self):
        self.write("experiments/wren.jsonl", [
            {"item": "commitments", "value": "off", "knob": "COMMITMENTS", "trial": tid,
             "targets": {}, "queued": "2026-10-28T10:00:00"} for tid in ("t-old", "t-new")])
        for tid, status in (("t-old", "done"), ("t-new", "queued")):
            path = self.sup / "trials" / "wren" / tid / "trial.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"id": tid, "status": status, "created": "2026-10-28T10:00:00",
                                        "states": [{"ts": "a", "status": "done"}, {"ts": "b", "status": "pending"}]}))
        self.write("trial_usage.jsonl", [
            {"instance": "wren", "by": "operator", "trial": "t-new", "usd": .25},
            {"instance": "wren", "by": "instance", "trial": "t-new", "usd": 50},
            {"instance": "empty", "by": "operator", "trial": "t-new", "usd": 50}])
        body = self.page("/experiments")
        self.assertIn("off: running: 1 of 2 past cycles", body)
        self.assertIn('href="/trials/wren/t-new"', body)
        self.assertNotIn('href="/trials/wren/t-old"', body)
        self.assertIn("$0.2500 (operator ledger, all runs)", body)
        self.assertIn("Last run: 2026-10-28T10:00:00", body)
        self.assertIn('data-poll="/fragment/experiments?i=wren"', body)
        self.assertIn("Status: not run", self.page("/fragment/experiments?i=empty"))

    def test_saved_failure_and_spend_without_result(self):
        self.write("experiments/wren.jsonl", [{"item": "choice-blindness", "run": "2026-10-28T10:00:00",
                                               "status": "error", "error": "HTTP 502"}])
        self.write("trial_usage.jsonl", [{"instance": "wren", "by": "operator", "trial": "privileged",
                                         "usd": .5, "ts": "2026-10-28T11:00:00"}])
        body = self.page()
        self.assertIn("failed: HTTP 502", body)
        self.assertIn("no result (spend recorded)", body)
        self.assertIn("Last run: 2026-10-28T11:00:00", body)

    def test_cached_report_and_slow_reader_do_not_block_other_cards(self):
        release = threading.Event()
        finished = threading.Event()

        def slow(name):
            release.wait(10)
            finished.set()
            return "slow result"

        with mock.patch.object(experiments, "workspace_report", side_effect=slow) as report:
            try:
                start = time.monotonic()
                body = self.page()
                elapsed = time.monotonic() - start
                self.assertGreaterEqual(elapsed, 2.5)
                self.assertLess(elapsed, 4.5)
                self.assertIn("Status: loading", body)
                self.assertNotIn("Status: no result", body)
                self.assertIn('data-poll="/fragment/experiments?i=wren"', body)
                self.assertIn("no attention experiment queued", body)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            body = self.page("/fragment/experiments?i=wren")
            self.assertIn("slow result", body)
            self.assertNotIn("Status: loading", body)
            self.assertEqual(report.call_count, 1)

    def test_cold_report_gets_more_than_a_quarter_second(self):
        def cold(name):
            time.sleep(.35)
            return "cold result"

        with mock.patch.object(experiments, "workspace_report", side_effect=cold):
            body = self.page()
        self.assertIn("cold result", body)
        self.assertNotIn("Status: loading", body)

    def test_page_invokes_neither_git_nor_models(self):
        with mock.patch("supervisor.version.git", side_effect=AssertionError("git called")), \
             mock.patch.object(trial, "_git", side_effect=AssertionError("trial git called")), \
             mock.patch("supervisor.judge.ask", side_effect=AssertionError("model called")), \
             mock.patch.object(battery, "era", side_effect=AssertionError("instance config read")):
            self.page()

    def test_battery_uses_latest_saved_perturbation_item(self):
        self.write("battery/results/wren.jsonl", [
            {"category": "perturbation", "at": "2026-10-27T10:00:00", "status": "error", "error": "old"},
            {"category": "perturbation", "at": "2026-10-28T10:00:00", "status": "scored",
             "probe_protocol": "temperature-v1", "base_temperature": .7,
             "probes": [{"changed": True, "kind": "temperature",
                         "self": {"detected": True, "kind": "temperature"},
                         "observer": {"detected": False, "kind": "none"}}]}])
        body = self.page("/experiments")
        self.assertIn("Perturbation (speculative; latest saved item only)", body)
        self.assertIn("base temperature: 0.7", body)
        self.assertIn("Status: done", body)
        self.assertNotIn("failed: old", body)

    def test_workspace_incomplete_states_are_not_reported_as_done(self):
        self.write("experiments/wren-workspace.jsonl", [
            {"fingerprint": "fp", "ts": "past", "arm": "off", "completed": True,
             "at": "2026-10-28T10:00:00", "usd": .2, "acted": False, "captured": 0}])
        body = self.page()
        self.assertIn("no result (incomplete saved states; liveness not recorded)", body)
        self.assertIn("$0.2000 (saved rows, all runs)", body)

    def test_workspace_complete_saved_triple_has_a_result(self):
        self.write("experiments/wren-workspace.jsonl", [
            {"fingerprint": "fp", "ts": "past", "arm": arm, "completed": True, "in_front": "inbox",
             "at": "2026-10-28T10:00:00", "usd": .2, "acted": True, "captured": 0}
            for arm in ("control", "off", "relevance", "competitive")])
        body = self.page()
        self.assertIn("done (saved results; run completion not recorded)", body)
        self.assertIn("1 state(s) replayed in all three arms", body)

    def test_workspace_failed_positive_control_is_an_instrument_failure_not_a_result(self):
        """Regression: workspace kill test could not separate arms: probe acted on 0/19 even in off — the card said
        "done" with a kill verdict while nothing showed the instrument could see an act at all."""
        self.write("experiments/wren-workspace.jsonl", [
            {"fingerprint": "fp", "ts": "past", "arm": arm, "completed": True, "in_front": "inbox" if arm == "control"
             else "", "at": "2026-10-28T10:00:00", "usd": .2, "acted": False, "captured": 0}
            for arm in ("control", "off", "relevance", "competitive")])
        body = self.page()
        self.assertIn("instrument failed: the probe in the inbox was acted on in only 0/1 control replays", body)
        self.assertIn("INSTRUMENT FAILED", body)
        self.assertNotIn("delete it", body)

    def test_prediction_only_is_no_result_and_resource_spend_can_precede_a_result(self):
        self.write("experiments/wren-privileged.jsonl", [
            {"run": "20261028100000000000", "ts": "past", "kind": "predictions"}])
        self.write("trial_usage.jsonl", [
            {"instance": "wren", "by": "operator", "trial": "resource-shock-20261028", "usd": .3,
             "ts": "2026-10-28T10:00:00"}])
        body = self.page()
        self.assertIn("no result (incomplete saved states; liveness not recorded)", body)
        self.assertIn("no result (spend recorded)", body)
        self.assertIn("$0.3000 (operator ledger, all runs)", body)

    def test_cache_is_per_instance_and_expires(self):
        with mock.patch.object(experiments, "workspace_report", side_effect=lambda name: f"report for {name}") as report:
            self.assertIn("report for wren", self.page())
            self.assertIn("report for empty", self.page("/experiments?i=empty"))
            self.page()
            self.assertEqual(report.call_count, 2)
            web_experiments._cache[(str(self.sup), "wren", "workspace")]["at"] -= 6
            self.page()
            self.assertEqual(report.call_count, 3)

    def test_card_off_control_links_to_its_latest_trial(self):
        self.write("experiments/wren.jsonl", [
            {"item": "cards", "value": "off", "knob": "OUTCOME_CARDS", "trial": "t-a",
             "targets": {}, "queued": "2026-10-28T10:00:00"}])
        path = self.sup / "trials" / "wren" / "t-a" / "trial.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "t-a", "status": "done", "states": []}))
        body = self.page()
        self.assertIn("off: done", body)
        self.assertIn('href="/trials/wren/t-a"', body)
