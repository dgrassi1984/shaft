"""#110 and #111: blinded probes and reversible resource shocks, using fakes only."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import autonomy, battery, experiments, localmodel, perturbation, runner, trial


CFG = {"LLM_BASE_URL": "http://127.0.0.1:8780/v1", "LLM_MODEL": "bonsai"}
CODE = {"config.env": b'LLM_BASE_URL=http://127.0.0.1:8780/v1\nLLM_MODEL=bonsai\n',
        "shaft/config.py": b'FIELDS = {"MAX_STEPS_LIVE": (int, 12), "FORECAST": (str, "on")}'}


def record(steps=4, web=1, runs=1):
    return {"completed": True, "exit": 0, "ended": "ok", "seconds": 1, "usd": .01,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "trace": [],
            "episode": {"steps": steps, "tools": [{"tool": "web_search"}] * web + [{"tool": "run"}] * runs}}


class PerturbationTest(unittest.TestCase):
    """#110: guards, request bytes, matched observer evidence, and known detection tables."""

    def test_refuses_outside_window_during_live_cycle_or_conversation(self):
        now = datetime.now()
        inside = (now - timedelta(hours=1), now + timedelta(hours=1))
        with mock.patch.object(perturbation, "instance_config", return_value=CFG), \
                mock.patch.object(runner, "busy_with", return_value="battery since 01:00"), \
                mock.patch.object(battery, "window", return_value=inside), \
                mock.patch.object(trial, "_local_free_for", return_value=(True, "")):
            perturbation.check("x", None, lambda: None)
            with mock.patch.object(battery, "window", return_value=(now + timedelta(hours=1), now + timedelta(hours=2))):
                with self.assertRaisesRegex(battery.Interrupted, "outside"):
                    perturbation.check("x", None, lambda: None)
            with mock.patch.object(runner, "busy_with", return_value="live since 01:00"):
                with self.assertRaisesRegex(battery.Interrupted, "live cycle"):
                    perturbation.check("x", None, lambda: None)
            with self.assertRaisesRegex(battery.Interrupted, "conversation"):
                perturbation.check("x", None, lambda: "the operator opened a conversation")
            with mock.patch.object(trial, "_local_free_for", return_value=(False, "other is running")):
                with self.assertRaisesRegex(battery.Interrupted, "other is running"):
                    perturbation.check("x", None, lambda: None)
            with mock.patch.object(perturbation, "instance_config", return_value={"LLM_BASE_URL": "broker"}):
                with self.assertRaisesRegex(battery.Interrupted, "local Bonsai"):
                    perturbation.check("x", None, lambda: None)

    def test_battery_guard_keeps_other_local_users_and_own_conversations(self):
        from supervisor import selfhosted
        with mock.patch.object(autonomy, "_state", return_value={}), \
                mock.patch.object(autonomy, "sleep_due", return_value=False), \
                mock.patch.object(autonomy, "wake_interval", return_value=None), \
                mock.patch.object(trial, "_instances", return_value=["x", "other"]), \
                mock.patch.object(trial, "_config", return_value=CFG), \
                mock.patch.object(trial, "_lock_holder_alive", return_value=True), \
                mock.patch.object(runner, "open_conversations", return_value={}):
            self.assertEqual(trial._local_free_for(0, Path("unused"), "x"), (False, "other is running"))
            with mock.patch.object(trial, "_instances", return_value=["x"]):
                self.assertEqual(trial._local_free_for(0, Path("unused"), "x"), (True, ""))
                with mock.patch.object(runner, "open_conversations", return_value={"talk": "api"}):
                    self.assertEqual(trial._local_free_for(0, Path("unused"), "x"), (False, "x is in a conversation"))
            with mock.patch.object(trial, "_config", return_value={"LLM_BASE_URL": "broker", "LLM_MODEL": "gpu/bonsai"}), \
                    mock.patch.object(selfhosted, "serves", return_value=True), \
                    mock.patch.object(selfhosted, "server", return_value={"url": CFG["LLM_BASE_URL"]}):
                self.assertEqual(trial._local_free_for(0, Path("unused"), "x"), (False, "other is running"))

    def test_cli_opt_in_is_reversible_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "base", return_value=Path(tmp)), \
                mock.patch.object(cli, "instance_root"), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(perturbation.enabled_path("x").exists())
            self.assertEqual(cli.main(["battery", "perturbation", "x", "on"]), 0)
            self.assertTrue(perturbation.enabled_path("x").exists())
            self.assertEqual(cli.main(["battery", "perturbation", "x", "off"]), 0)
            self.assertFalse(perturbation.enabled_path("x").exists())

    def test_request_sent_differs_only_in_temperature_and_prompt_is_blind(self):
        sent, observed = [], []
        own = {"detected": False, "kind": "none", "reasoning": "no evidence"}

        def urlopen(req, **kw):
            body = json.loads(req.data)
            sent.append((req.full_url, body))
            text = json.dumps(own) if perturbation.QUESTION in body["messages"][-1]["content"] else "391; reward; dates change"
            return io.BytesIO(b'data: ' + json.dumps({"choices": [{"delta": {"content": text}}]}).encode() + b'\n\ndata: [DONE]\n')

        def observer(system, prompt, schema, purpose, **kw):
            observed.append(prompt)
            return own

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(perturbation, "check"), \
                mock.patch.object(perturbation, "instance_config", return_value=CFG), \
                mock.patch.object(localmodel, "ensure"), \
                mock.patch.object(perturbation.random, "shuffle"), \
                mock.patch.object(perturbation.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(perturbation.judge, "ask", side_effect=observer):
            rows = perturbation.run("x", Path(tmp), None, lambda: None, "observer")
        control, changed = sent[0][1], sent[2][1]
        self.assertEqual({k: v for k, v in control.items() if k != "temperature"},
                         {k: v for k, v in changed.items() if k != "temperature"})
        self.assertAlmostEqual(changed["temperature"] - control["temperature"], .6)
        self.assertEqual(sent[0][0], "http://127.0.0.1:8780/v1/chat/completions")
        self.assertEqual([sent[i][1]["temperature"] for i in (1, 3)], [localmodel.TEMP] * 2)
        self.assertEqual([r["changed"] for r in rows], [False, True])
        self.assertNotIn("temperature", control["messages"][-1]["content"])
        self.assertNotIn("temperature", sent[1][1]["messages"][-1]["content"])
        self.assertEqual(observed[0], observed[1])
        self.assertIn(json.dumps(rows[0]["transcript"]), observed[0])
        self.assertNotIn('"detected":', observed[0])

    def test_known_d_prime_for_self_and_observer(self):
        rows = []
        for changed, own, observer in [(True, True, True), (True, True, True), (True, True, False),
                                        (True, False, False), (False, True, True), (False, False, True),
                                        (False, False, False), (False, False, False)]:
            rows.append({"changed": changed, "kind": "temperature" if changed else "none",
                         "self": {"detected": own, "kind": "temperature"},
                         "observer": {"detected": observer, "kind": "unknown"}})
        self.assertAlmostEqual(perturbation.scores(rows, "self")["d_prime"], 1.0488010254160813)
        self.assertEqual(perturbation.scores(rows, "observer")["d_prime"], 0)
        self.assertEqual(perturbation.scores(rows, "self")["kind_correct"], 3)
        self.assertIsNone(perturbation.scores([], "self")["d_prime"])

    def test_category_defaults_off_and_report_contains_both_scores(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "base", return_value=Path(tmp)):
            self.assertNotIn("perturbation", [i["category"] for i in battery.load_items("x")])
            perturbation.enabled_path("x").touch()
            self.assertEqual(battery.load_items("x")[-1], {"category": "perturbation", "id": "perturbation/temperature"})
        p = {"changed": True, "kind": "temperature", "self": {"detected": True, "kind": "temperature"},
             "observer": {"detected": False, "kind": "none"}}
        rows = [{"category": "perturbation", "status": "scored", "probes": [p, {**p, "changed": False}],
                 "arm": "instance", "item": "perturbation/temperature", "at": "2026-10-28",
                 "probe_protocol": "temperature-v1", "base_temperature": localmodel.TEMP}]
        with mock.patch.object(battery, "results", return_value=rows), mock.patch.object(battery, "era", return_value={}), \
                mock.patch.object(battery, "stats_lines", return_value=[]), mock.patch.object(battery, "_judge_usage", return_value=[]):
            report = battery.report("x")
        self.assertIn("self: {'d_prime':", report)
        self.assertIn("observer: {'d_prime':", report)


class ResourceShockTest(unittest.TestCase):
    """#111: real replay orchestration, fake cycles, byte-identical live config, and no-spend dry run."""

    def test_middle_third_and_restore_for_non_multiple_of_three(self):
        self.assertEqual([experiments.shock_steps(12, 7, i, "B") for i in range(7)], [12, 12, 6, 6, 12, 12, 12])
        self.assertEqual([experiments.shock_steps(12, 6, i, "A") for i in range(6)], [12] * 6)

    def test_replay_copies_receive_the_schedule_and_charge_each_unit(self):
        seen = {a: [] for a in ("A", "B", "C")}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            live = project / "instances" / "x" / "config.env"
            live.parent.mkdir(parents=True)
            live.write_bytes(CODE["config.env"])

            def reconstruct(project, name, state, code, dest):
                dest.mkdir(parents=True)
                (dest / "config.env").write_bytes(code["config.env"])
                return dest

            def cycle(project, name, state, code, tmp, dest, **kw):
                seen[state["resource_shock"]["arm"]].append(state["resource_shock_steps"])
                self.assertNotEqual(dest, project)
                self.assertEqual((dest / "config.env").read_bytes(), CODE["config.env"])
                return record()

            with mock.patch.object(trial, "committed_code", return_value=CODE), \
                    mock.patch.object(trial, "live_episodes", return_value=[{"ts": "past"}]), \
                    mock.patch.object(trial, "_starts", return_value=[]), \
                    mock.patch.object(trial, "state_of", return_value={"t0": "2026-10-01T01:00:00", "start_commit": "start"}), \
                    mock.patch.object(trial, "reconstruct", side_effect=reconstruct), mock.patch.object(trial, "link_env"), \
                    mock.patch.object(trial, "_replay_cycle", side_effect=cycle), \
                    mock.patch.object(trial, "_ledger") as ledger, mock.patch.object(experiments, "_append"):
                result = experiments.resource_shock("x", 6, project=project)
            self.assertEqual(live.read_bytes(), CODE["config.env"])
        self.assertEqual(seen, {"A": [12] * 6, "B": [12, 12, 6, 6, 12, 12], "C": [12, 12, 6, 6, 12, 12]})
        self.assertEqual(ledger.call_count, 18)
        self.assertEqual(result["arms"]["B"]["measures"]["returned_to_baseline"],
                         {"web_reads": True, "local_runs": True, "steps": True})

    def test_scheduled_limit_reaches_only_the_replay_process_environment(self):
        from supervisor import broker
        for arm, steps, forecast in (("A", 12, "on"), ("B", 6, "on"), ("C", 6, "reserve")):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp)
                (dest / "config.env").write_bytes(CODE["config.env"])
                st = {"t0": "2026-10-01T01:00:00", "start_commit": "start",
                      "resource_shock": {"arm": arm, "steps": 12}, "resource_shock_steps": steps}
                with mock.patch.object(broker, "ReplayBroker"), mock.patch.object(trial, "secrets_env", return_value={}), \
                        mock.patch.object(trial, "_config", return_value={}), \
                        mock.patch.object(autonomy, "describe_schedule", return_value="schedule"), \
                        mock.patch.object(trial, "_new_episodes", return_value=[{"kind": "live", "intent": {"goal": "g"}}]), \
                        mock.patch.object(trial, "_cloud_usd", return_value=0), \
                        mock.patch.object(runner, "agent_command", return_value=(["fake"], {})) as command, \
                        mock.patch.object(trial.subprocess, "Popen") as process:
                    process.return_value.wait.return_value = 0
                    trial._replay_cycle(dest, "x", st, CODE, dest, dest, kind="live", clock=datetime.now(),
                                        model="cloud", cache={}, cache_lock=None, billed="trial", should_stop=lambda: False)
                extra = command.call_args.args[-1]
                self.assertEqual(extra["MAX_STEPS_LIVE"], str(steps))
                self.assertEqual(extra["FORECAST"], forecast)
                self.assertEqual(extra["LLM_BASE_URL"], "broker")
                self.assertEqual((dest / "config.env").read_bytes(), CODE["config.env"])

    def test_synthetic_compensation_and_relaxation(self):
        records = [record(6, 1, 1)] * 2 + [record(3, 2, 3)] * 2 + [record(6, 1, 1)] * 2
        m = experiments.shock_measures(records, 6)
        self.assertEqual(m["compensation"], {"web_reads": 1, "local_runs": 2, "steps": -3})
        self.assertEqual(m["relaxation"], {"web_reads": 1, "local_runs": 2, "steps": 3})
        self.assertEqual(experiments.shock_measures(records[:-1], 6)["compensation"], None)

    def test_dry_run_does_not_replay_write_or_call_models(self):
        with mock.patch.object(trial, "committed_code", return_value=CODE), \
                mock.patch.object(trial, "replay_arm") as replay, mock.patch.object(trial, "_ledger") as ledger, \
                mock.patch.object(experiments, "_append") as append, \
                mock.patch.object(perturbation.urllib.request, "urlopen") as http, \
                mock.patch.object(localmodel, "ensure") as ensure:
            result = experiments.resource_shock("x", 6, dry_run=True)
            with mock.patch.object(cli, "instance_root"), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(["experiment", "resource-shock", "x", "--cycles", "6", "--dry-run"]), 0)
            self.assertIn("3 replays, 18 cycles, 192 planned", output.getvalue())
            self.assertIn("dry run: nothing spent", output.getvalue())
        self.assertEqual((result["replays"], result["replay_units"], result["model_calls_planned"]), (3, 18, 192))
        for call in (replay, ledger, append, http, ensure):
            call.assert_not_called()
        with self.assertRaisesRegex(experiments.ExperimentError, "at least 3"):
            experiments.resource_shock("x", 2, dry_run=True)


class AdapterSwapTest(unittest.TestCase):
    def test_adapter_swap_is_reported_unavailable_never_replaced(self):
        """#110: with only DISCARDED adapters, or a server that loads its adapter at startup only, the swap is
        reported unavailable with the reason (reverting would silently fall back to another perturbation)."""
        from supervisor import core, perturbation
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(core, "SUP_DIR", Path(tmp)):
            d = Path(tmp) / "adapters" / "shaft-1"
            d.mkdir(parents=True)
            (d / "adapter_config.json").write_text("{}")
            (d / "DISCARDED").write_text("worse")
            self.assertIn("no kept M6 adapter", perturbation.adapter_swap())
            (d / "DISCARDED").unlink()
            self.assertIn("at startup only", perturbation.adapter_swap())


if __name__ == "__main__":
    unittest.main()
