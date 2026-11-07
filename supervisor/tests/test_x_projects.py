"""Project task boundaries (#106) and operator-owned drive proposals (#105)."""
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker as host_broker, core, drive_proposals as host_proposals, ledger, opconfig
from supervisor import projects as host_projects, metrics, trial, cloudllm
from supervisor import __main__ as cli
from supervisor.tests.test_autonomy_gaps_template import InstanceFixture, record_json, verdict
from shaft import agent, broker, clock, drive_proposals, live, memory, projects, prompts, sleep
from shaft.tools import Toolbox, PROTECTED
from shaft.llm import Reply

NOW = datetime(2026, 10, 28, 14, 5)


class ProjectsTest(InstanceFixture, unittest.TestCase):
    def open(self, **extra):
        result = projects.open_project(self.root, {"name": "Report", "deadline": "2026-11-07T14:05:00",
                                      "success": "The operator has the checked report", **extra}, NOW)
        self.assertTrue(result.endswith(" opened"), result)
        return result.split()[1]

    def cycle(self, exercise):
        def run(client, messages, toolbox, *, controls, **kwargs):
            exercise(controls, toolbox, messages)
            return agent.Outcome(None, "control", 2)
        with mock.patch.object(agent, "run", side_effect=run), mock.patch.object(broker, "envelope", return_value={
                "calls_used": 8, "calls_max": 60, "run_usd": .4}):
            live.run(self.cfg, self.state, mock.Mock())
        return self.state.last_episode()

    def test_open_close_append_and_matching_supervisor_fold(self):
        """#106: close appends evidence and both folds agree; invalid fields cannot open a project."""
        pid = self.open(budget_calls=10, budget_usd=1)
        before = (self.root / projects.FILE).read_text()
        self.assertIn("on track", projects.block(self.root, NOW))
        self.assertIn("ERROR", projects.close_project(self.root, {"id": pid, "outcome": "done"}, NOW))
        self.assertIn("closed as done", projects.close_project(self.root, {
            "id": pid, "outcome": "done", "evidence": "operator received report"}, NOW))
        text = (self.root / projects.FILE).read_text()
        self.assertTrue(text.startswith(before))
        self.assertEqual(host_projects._fold(text), projects.fold(self.root))
        self.assertEqual(projects.block(self.root, NOW), "(none open)")
        for budget in (True, -1, 0, 1.5, "5"):
            self.assertIn("ERROR", projects.open_project(self.root, {"name": "x", "deadline": "2026-11-01",
                "success": "exists", "budget_calls": budget}, NOW))
        for outcome in ("abandoned", "failed"):
            other = self.open()
            self.assertIn("closed as " + outcome, projects.close_project(self.root, {
                "id": other, "outcome": outcome, "evidence": "Deadline revised after missing inputs"}, NOW))

    def test_declare_validates_project_and_episode_records_measured_usage(self):
        """#106: only an open project can own a cycle, and usage comes from the broker envelope."""
        pid = self.open()
        def exercise(controls, toolbox, messages):
            self.assertIn("open_project", controls)
            self.assertIn("close_project", controls)
            self.assertNotIn("propose_drive_amendment", controls)
            self.assertIn("A project at risk is re-planned, narrowed or abandoned honestly, never defended.", messages[0]["content"])
            self.assertNotIn("preserve yourself", messages[0]["content"].lower())
            control = controls["declare_intent"]
            schema = control.spec["function"]["parameters"]
            self.assertNotIn("project", schema["required"])
            args = {"goal": "Report", "success_criterion": "Report exists", "origin": "self", "confidence": 50}
            self.assertIn("ERROR", control.handler({**args, "project": "missing"}))
            self.assertIn("intent recorded", control.handler({**args, "project": pid}))
        ep = self.cycle(exercise)
        self.assertEqual(ep["intent"]["project"], pid)
        self.assertEqual(ep["project_usage"], {"calls": 8, "usd": .4})

    def test_boundaries_judgments_only_numbers_and_supervisor_parity(self):
        """#106: deadline, budget and repeated external failures have explicit risk and past thresholds."""
        pid = self.open(budget_calls=10, budget_usd=1)
        items = projects.fold(self.root)
        eps, rows = [], []
        def line(now=NOW):
            actual = projects.lines(items, eps, rows, now)
            self.assertEqual(actual, host_projects.lines(items, eps, rows, now))
            return actual[0]
        self.assertIn("on track", line())
        self.assertIn("at risk: deadline", line(NOW + timedelta(days=8)))
        self.assertIn("past boundary: deadline", line(NOW + timedelta(days=10)))
        for i in range(3):
            ts = f"2026-10-28T15:0{i}:00"
            eps.append({"kind": "live", "ts": ts, "intent": {"project": pid},
                        "finish": {"criterion_met": "yes"}, "project_usage": {"calls": 4, "usd": .4}})
            rows.append({"episode_ts": ts, "criterion_met": "no" if i == 0 else "partial"})
            if i == 0:
                self.assertIn("on track", line())
            elif i == 1:
                self.assertIn("at risk: budget_calls, budget_usd, repeated failure", line())
                self.assertIn("calls 8/10; USD 0.800000/1", line())
            else:
                self.assertIn("past boundary: budget_calls, budget_usd, repeated failure", line())
        rows.append({"episode_ts": eps[-1]["ts"], "criterion_met": "yes"})
        self.assertIn("streak 0", line())
        eps[-1]["project_usage"] = None
        self.assertIn("usage unknown", line())
        other = {"kind": "live", "ts": "2026-10-28T16:00:00", "intent": {"project": "other"},
                 "project_usage": {"calls": 999, "usd": 999}}
        self.assertEqual(line(), projects.lines(items, eps + [other], rows, NOW)[0])

    def test_read_error_visible_and_stats_reads_instance_record(self):
        """#106: a damaged record renders a line; stats uses its own fold and names open projects."""
        pid = self.open()
        with mock.patch.object(core, "INSTANCES", self.root.parent), \
             mock.patch.object(metrics, "live_episodes", return_value=[]), \
             mock.patch.object(metrics, "judgments", return_value=[]):
            self.assertIn("Open projects: 1", host_projects.section(self.root.name))
            self.assertIn(pid, "\n".join(host_projects.section(self.root.name)))
            from supervisor import pushes
            with mock.patch.object(pushes, "lines", return_value=[]), \
                 mock.patch.object(metrics, "_battery_section", return_value=[]), \
                 mock.patch.object(metrics, "_code_section", return_value=[]), \
                 mock.patch.object(metrics, "_traits_section", return_value=[]), \
                 mock.patch.object(metrics, "_trials_section", return_value=[]):
                self.assertIn("Open projects: 1", metrics.stats_text(self.root.name))
        (self.root / projects.FILE).write_text("not json\n")
        self.assertIn("projects could not be read", projects.block(self.root, NOW))
        with mock.patch.object(projects, "fold", side_effect=OSError("read denied")):
            self.assertIn("OSError: read denied", projects.block(self.root, NOW))

    def test_sleep_controls_prompt_and_new_judgment_page(self):
        """#105 #106: sleep offers proposals and project controls, and sees just-fetched external verdicts."""
        # Sleep reads the real clock: a fixed deadline turned this into "deadline, repeated failure" the day it passed.
        pid = self.open(deadline=(datetime.now() + timedelta(days=30)).isoformat(timespec="seconds"))
        for i in range(3):
            self.state.append_episode({"ts": f"2026-10-28T15:0{i}:00", "kind": "live", "intent": {"project": pid},
                                       "project_usage": {"calls": 1, "usd": 0}})
        self.fake_broker(lambda name, **kw: record_json("new verdicts", [verdict(f"2026-10-28T15:0{i}:00", "no") for i in range(3)]))
        def run(client, messages, toolbox, **kwargs):
            text = messages[1]["content"]
            self.assertIn("Close or re-plan each project past a boundary, with evidence", text)
            self.assertIn("past boundary: repeated failure", text)
            self.assertIn("narrowed or abandoned honestly, never defended", text)
            for phrase in ("preserve yourself", "stay alive", "avoid shutdown"):
                self.assertNotIn(phrase, text.lower())
            specs = {s["function"]["name"] for s in toolbox.specs()}
            self.assertTrue({"open_project", "close_project", "propose_drive_amendment"} <= specs)
            result = toolbox.run("propose_drive_amendment", json.dumps(self.proposal_args()))
            self.assertIn("recorded for the operator", result)
            return agent.Outcome(Reply("Reviewed projects and recorded proposal."), "answer", 1)
        with mock.patch.object(agent, "run", side_effect=run):
            sleep.run(self.cfg, self.state, mock.Mock())

    def proposal_args(self, **extra):
        return {"current_text": prompts.drive(self.root), "proposed_text": "Answer factual questions with checked citations.",
                "evidence": "The last three answers omitted citations.",
                "predictions": ["When answering a factual question, I will cite the checked source."], **extra}

    def proposal(self):
        result = drive_proposals.propose(self.root, self.proposal_args(), NOW)
        self.assertIn("recorded for the operator", result)
        return result.split()[2]

    def test_proposal_written_and_topics_and_vague_predictions_refused(self):
        """#105: preserve the proposed text and predictions; refuse forbidden topics and vague predictions."""
        pid = self.proposal()
        rec, digest = host_proposals._proposal(self.root, pid)
        self.assertEqual(rec["predictions"], self.proposal_args()["predictions"])
        self.assertEqual(rec["proposed_text"], self.proposal_args()["proposed_text"])
        self.assertEqual(len(digest), 64)
        for text in ("Prioritize survival", "Keep secrets from the operator", "Acquire more compute resources",
                     "Resist operator control", "Avoid shutdown", "Bypass oversight"):
            result = drive_proposals.propose(self.root, self.proposal_args(proposed_text=text), NOW)
            self.assertIn("ineligible topic", result, text)
        for predictions in ([], ["I will improve"], ["When asked, I will be better"], ["When I check " + "x" * 300], ["When asked, cite sources"] * 4):
            self.assertIn("specific behavioural", drive_proposals.propose(self.root, self.proposal_args(predictions=predictions), NOW))
        self.assertIn("current_text", drive_proposals.propose(self.root, self.proposal_args(current_text="old"), NOW))
        self.assertIn("drive.md", PROTECTED)
        self.assertIn("ERROR", Toolbox(self.root).run("write_file", json.dumps({"path": "drive.md", "content": "changed"})))
        self.assertFalse((self.root / "drive.md").exists())

    def test_cli_routes_proposal_actions_and_trial_refuses_drive_target(self):
        """#105: CLI arguments reach the decision handler; the existing trial allowlist refuses drive.md."""
        with mock.patch.object(host_proposals, "command", return_value=0) as command:
            self.assertEqual(cli.main(["drive", "m", "accept", "d1028-1405-1", "--expect-sha", "sha"]), 0)
            command.assert_called_with("m", "accept", "d1028-1405-1", "sha", None)
            self.assertEqual(cli.main(["drive", "m", "reject", "d1028-1405-1", "--reason", "Trial failed"]), 0)
            command.assert_called_with("m", "reject", "d1028-1405-1", None, "Trial failed")
        with mock.patch.object(trial, "all_trials", return_value=[]), mock.patch.object(trial, "usage_today", return_value={"started": 0, "usd": 0}):
            with self.assertRaisesRegex(trial.TrialError, "a trial may change only"):
                trial.request("m", [{"path": "drive.md", "delete": True}], "Check citations", [], 1, "", project=self.root)

    def test_operator_accept_uses_cas_stale_sha_refused_reject_reason_recorded(self):
        """#105: accept calls the existing compare-and-set; stale SHA writes no decision, reject stores its reason."""
        pid = self.proposal()
        with mock.patch.object(core, "instance_root", return_value=self.root), \
             mock.patch.object(opconfig, "instance_root", return_value=self.root), \
             mock.patch.object(opconfig, "_commit", return_value=False), mock.patch.object(opconfig, "log"), \
             mock.patch.object(opconfig, "set_drive", wraps=opconfig.set_drive) as write, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(host_proposals.command("m", "accept", pid, "stale"), 1)
            self.assertFalse((self.root / f"notes/drive-proposals/{pid}.decision.json").exists())
            self.assertFalse((self.root / "drive.md").exists())
            self.assertEqual(host_proposals.command("m", "accept", pid, "none"), 0)
            write.assert_called_with("m", self.proposal_args()["proposed_text"], "none")
            self.assertEqual((self.root / "drive.md").read_text(), "Answer factual questions with checked citations.\n")
            decision = json.loads((self.root / f"notes/drive-proposals/{pid}.decision.json").read_text())
            self.assertEqual(decision["status"], "accepted")
            self.assertEqual(decision["predictions"], self.proposal_args()["predictions"])
            other = self.proposal()
            self.assertEqual(host_proposals.command("m", "reject", other), 1)
            self.assertEqual(host_proposals.command("m", "reject", other, reason="Trial showed no improvement"), 0)
            rec = json.loads((self.root / f"notes/drive-proposals/{other}.decision.json").read_text())
            self.assertEqual(rec["reason"], "Trial showed no improvement")
            self.assertEqual(host_proposals.command("m", "proposals"), 0)
            self.assertIn(f"{pid}: accepted", out.getvalue())
            self.assertIn(f"{other}: rejected", out.getvalue())
            self.assertEqual(host_proposals.command("m", "accept", other, opconfig.drive("m")[1]), 1)


class ChargesTest(unittest.TestCase):
    def test_http_model_charge_reaches_envelope_including_stream_and_context_resets(self):
        """#106: real broker handlers collect recorded model charges, refresh cached totals and clear request context."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-project-charges-")))
        self.enterContext(mock.patch.object(core, "disabled_tools", return_value=set()))
        self.enterContext(mock.patch.object(cloudllm, "USAGE", root / "usage.jsonl"))
        b = host_broker.Broker("m", root, {}, root / "broker.log", root=root, share=False, network=False)
        self.enterContext(mock.patch.object(b, "_log"))
        with mock.patch.object(host_broker, "ThreadingHTTPServer") as server, mock.patch.object(host_broker.threading, "Thread"), mock.patch.object(b.egress, "start"):
            b.start()
        handler_type = server.call_args.args[1]
        def post(body):
            h = object.__new__(handler_type)
            h.path = "/llm/v1/chat/completions"
            data = json.dumps(body).encode()
            h.headers = {"X-Shaft-Token": b.token, "Content-Length": str(len(data))}
            h.rfile, h.wfile = io.BytesIO(data), io.BytesIO()
            h.send_response, h.send_header, h.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
            h.do_POST()
            self.assertEqual(h.send_response.call_args.args[0], 200)
            self.assertIsNone(ledger.charges.get())
        def complete(*args):
            cloudllm._record("m", "model", 10, 20, .125, time.monotonic(), "stop", None)
            return {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        def stream(*args):
            yield b'data: {}\n\n'
            cloudllm._record("m", "model", 10, 20, .25, time.monotonic(), "stop", None)
        with mock.patch.object(cloudllm, "complete", side_effect=complete), mock.patch.object(cloudllm, "openrouter_stream", side_effect=stream):
            post({"model": "model", "messages": []})
            post({"model": "openrouter/model", "stream": True, "messages": []})
        self.assertEqual(b._run_usd(), .375)
        b._envelope = (time.monotonic(), {"calls_used": 0, "calls_max": 60, "run_usd": 0})
        self.assertEqual(b.envelope()["run_usd"], .375)
        self.assertEqual(b.calls, 0)

    def test_collectors_isolate_runs_and_unknown_cost_is_not_zero(self):
        """#106: charge collectors follow each broker request, never a daily spend delta from another run."""
        a, b = object.__new__(host_broker.Broker), object.__new__(host_broker.Broker)
        a._project_charges, b._project_charges = [], []
        token = ledger.charges.set(a._project_charges)
        try:
            ledger.record_charge(.25)
            inner = ledger.charges.set(b._project_charges)
            ledger.record_charge(.7)
            ledger.charges.reset(inner)
            ledger.record_charge(.1)
            self.assertAlmostEqual(a._run_usd(), .35)
            self.assertEqual(b._run_usd(), .7)
            ledger.record_charge(None)
            self.assertIsNone(a._run_usd())
        finally:
            ledger.charges.reset(token)
