"""Model bake-offs (supervisor/bakeoff.py): past cycles replayed once per model from the same state, each candidate
judged blind against the baseline in both orders. `shaft trial run` could not do this: it refuses LLM_* changes and
gives both arms one model (docs/bakeoffs/README.md)."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import bakeoff, cloudllm, opconfig, trial

MODELS = ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "openrouter/deepseek/deepseek-v4.1-flash"]
EPS = [{"kind": "live", "ts": "2026-10-29T10:00:00"}, {"kind": "live", "ts": "2026-10-29T11:00:00"}]


def _rec(model, completed=True, usd=0.05):
    return {"exit": 0, "ended": "ok", "seconds": 60.0, "model": model, "episode": {"intent": {"goal": "g"}},
            "trace": [], "diff": "", "usage": {"prompt_tokens": 700_000, "completion_tokens": 20_000},
            "usd": usd, "completed": completed, "output_tail": ""}


class BakeoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-bakeoff-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(bakeoff, "DIR", self.tmp),
                  mock.patch.object(cloudllm, "CLOUD_MODELS", MODELS),
                  mock.patch.object(opconfig, "effort_choices", return_value=("medium", "xhigh", "low")),
                  mock.patch.object(trial, "live_episodes", return_value=EPS),
                  mock.patch.object(trial, "_starts", return_value=[]),
                  mock.patch.object(trial, "state_of", side_effect=lambda p, n, ep, s: {
                      "ts": ep["ts"], "t0": ep["ts"], "start_commit": "c0", "inbox": [], "original_goal": "g"}),
                  mock.patch.object(trial, "committed_code", return_value={}),
                  mock.patch.object(trial, "_git", return_value=mock.Mock(stdout="rev1\n")),
                  mock.patch.object(trial, "state_context", return_value="ctx"),
                  mock.patch.object(trial, "_ledger"),
                  mock.patch.object(trial, "_judge_usd", return_value=0.1)):
            p.start()
            self.addCleanup(p.stop)

    def create(self, arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5@xhigh",
                           "openrouter/deepseek/deepseek-v4.1-flash"), **kw):
        return bakeoff.create("thelos", list(arms), [e["ts"] for e in EPS], project=self.tmp, **kw)

    def test_each_state_runs_every_model_in_turn_from_one_shared_cache(self):
        calls = []
        def replay(project, name, st, code, *, model, cache, cache_lock, billed, env):
            calls.append((st["ts"], model, id(cache), env, billed))
            return _rec(model)
        b = self.create()
        with mock.patch.object(trial, "replay_arm", side_effect=replay), \
                mock.patch.object(trial, "judge_pair", return_value={"winner": "tie", "margin": "none",
                                  "first": {"claim_supported": True}, "second": {"claim_supported": True}}):
            bakeoff.run(b, parallel=1, project=self.tmp)
        first = [c for c in calls if c[0] == EPS[0]["ts"]]
        self.assertEqual([c[1] for c in first], ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5",
                                                 "openrouter/deepseek/deepseek-v4.1-flash"])
        self.assertEqual(len({c[2] for c in first}), 1)       # the same web answers for every model of a state
        self.assertNotEqual(first[0][2], calls[-1][2])        # a fresh cache per state
        self.assertEqual(first[1][3], {"LLM_REASONING_EFFORT": "xhigh"})
        self.assertEqual(first[0][3], {})
        self.assertTrue(all(c[4].startswith("thelos@bakeoff:") for c in calls))   # never billed to the instance

    def test_candidates_are_judged_in_both_orders_against_the_baseline(self):
        seen = []
        def judge(context, first, second, purpose, model=None, redact=None):
            seen.append((first["model"], second["model"], purpose.split()[-1], model))
            winner = "second" if first["model"].startswith("openrouter/xiaomi") else "first"   # haiku always wins
            if "deepseek" in first["model"] + second["model"]:
                winner = "first" if first["model"].startswith("openrouter/xiaomi") else "second"   # deepseek loses
            return {"winner": winner, "margin": "clear", "first": {"claim_supported": True},
                    "second": {"claim_supported": False}}
        b = self.create()
        with mock.patch.object(trial, "replay_arm", side_effect=lambda *a, model, **k: _rec(model)), \
                mock.patch.object(trial, "judge_pair", side_effect=judge):
            bakeoff.run(b, parallel=2, project=self.tmp)
        self.assertEqual(len(seen), 2 * 2 * 2)                # 2 states × 2 candidates × 2 orders
        self.assertEqual({s[3] for s in seen}, {"claude-sonnet-5-5"})
        self.assertIn(("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "AB", "claude-sonnet-5-5"), seen)
        self.assertIn(("claude-haiku-5-5", "openrouter/xiaomi/mimo-v2.6-pro", "BA", "claude-sonnet-5-5"), seen)
        outcomes = {label: [s["outcomes"][label]["outcome"] for s in b["states"]] for label in b["states"][0]["outcomes"]}
        self.assertEqual(outcomes["claude-haiku-5-5@xhigh"], ["win", "win"])
        self.assertEqual(outcomes["openrouter/deepseek/deepseek-v4.1-flash"], ["loss", "loss"])
        text = bakeoff.report(b)
        self.assertIn("| claude-haiku-5-5@xhigh | 2 | 0 | 0 | 0 |", text)
        # the judge marks the first run's claim supported and the second's not: each arm is counted on its own side
        self.assertIn("| openrouter/xiaomi/mimo-v2.6-pro | 2 | 2 | 0 | 0.0500 | 60 | 700,000 / 20,000 | 4/8 | 50% of 2 |", text)
        self.assertIn("| claude-haiku-5-5@xhigh | 2 | 2 | 0 | 0.0500 | 60 | 700,000 / 20,000 | 2/4 | 50% of 2 |", text)
        self.assertIn("judging $0.80", text)                  # 8 judgments at the mocked $0.10

    def test_a_run_that_did_not_complete_loses_without_a_judge_and_resume_skips_done_work(self):
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"))
        replay = mock.Mock(side_effect=lambda *a, model, **k: _rec(model, completed=model != "claude-haiku-5-5"))
        judge = mock.Mock(side_effect=AssertionError("judged"))
        with mock.patch.object(trial, "replay_arm", replay), mock.patch.object(trial, "judge_pair", judge):
            bakeoff.run(b, parallel=1, project=self.tmp)
            self.assertEqual([s["outcomes"]["claude-haiku-5-5"]["outcome"] for s in b["states"]], ["loss", "loss"])
            again = bakeoff.create("thelos", [], [], bid=b["id"], project=self.tmp)   # resumed from disk
            bakeoff.run(again, parallel=1, project=self.tmp)
        self.assertEqual(replay.call_count, 4)                # nothing replayed twice
        self.assertEqual(again["arms"], b["arms"])

    def test_every_arm_and_every_resume_runs_the_code_frozen_at_creation(self):
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"))
        self.assertEqual(b["code_rev"], "rev1")
        with mock.patch.object(trial, "_git", return_value=mock.Mock(stdout="rev2\n")), \
                mock.patch.object(trial, "replay_arm", side_effect=lambda *a, model, **k: _rec(model)), \
                mock.patch.object(trial, "judge_pair", return_value={"winner": "tie", "margin": "none",
                                  "first": {"claim_supported": True}, "second": {"claim_supported": True}}):
            bakeoff.run(b, parallel=1, project=self.tmp)   # the branch moved on since: the bake-off does not
        self.assertEqual({c.args[2] for c in trial.committed_code.call_args_list}, {"rev1"})

    def test_a_state_cut_off_part_way_is_replayed_in_full_on_resume(self):
        # Codex review: resuming after the baseline gave the candidate an empty web cache, so the pair no longer saw
        # the same answers. Now the state's arms all run again, from one cache.
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"))
        b["states"][0]["arms"]["openrouter/xiaomi/mimo-v2.6-pro"] = _rec("openrouter/xiaomi/mimo-v2.6-pro", usd=9)
        replay = mock.Mock(side_effect=lambda *a, model, **k: _rec(model))
        with mock.patch.object(trial, "replay_arm", replay), \
                mock.patch.object(trial, "judge_pair", return_value={"winner": "tie", "margin": "none",
                                  "first": {"claim_supported": True}, "second": {"claim_supported": True}}):
            bakeoff.run(b, parallel=1, project=self.tmp)
        self.assertEqual(replay.call_count, 4)
        self.assertEqual(b["states"][0]["arms"]["openrouter/xiaomi/mimo-v2.6-pro"]["usd"], 0.05)
        caches = [id(c.kwargs["cache"]) for c in replay.call_args_list if c.args[2]["ts"] == EPS[0]["ts"]]
        self.assertEqual(len(set(caches)), 1)

    def test_a_failed_judgment_is_judged_again_on_resume(self):
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"))
        tie = {"winner": "tie", "margin": "none", "first": {"claim_supported": True}, "second": {"claim_supported": True}}
        with mock.patch.object(trial, "replay_arm", side_effect=lambda *a, model, **k: _rec(model)):
            with mock.patch.object(trial, "judge_pair", side_effect=OSError("judge down")):
                bakeoff.run(b, parallel=1, project=self.tmp)
            self.assertEqual({s["outcomes"]["claude-haiku-5-5"]["outcome"] for s in b["states"]}, {"unjudged"})
            with mock.patch.object(trial, "judge_pair", return_value=tie) as judge:
                bakeoff.run(b, parallel=1, project=self.tmp)
        self.assertEqual(judge.call_count, 4)
        self.assertEqual({s["outcomes"]["claude-haiku-5-5"]["outcome"] for s in b["states"]}, {"tie"})
        self.assertAlmostEqual(b["states"][0]["outcomes"]["claude-haiku-5-5"]["judge_usd"], 0.4)   # both attempts

    def test_new_ids_never_reuse_one_and_a_resume_must_match(self):
        arms = ("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5")
        with mock.patch.object(bakeoff, "datetime") as clock:
            clock.now.return_value = trial.datetime(2026, 10, 30, 14, 0, 0)
            ids = {self.create(arms=arms)["id"] for _ in range(3)}
        self.assertEqual(len(ids), 3)
        bid = sorted(ids)[0]
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.create("thelos", ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5@xhigh"], [], bid=bid,
                           project=self.tmp)

    def test_one_process_at_a_time(self):
        import fcntl
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"))
        with bakeoff.path(b["name"], b["id"]).with_suffix(".lock").open("w") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(bakeoff.BakeoffError) as ctx:
                bakeoff.run(b, parallel=1, project=self.tmp)
        self.assertIn("already running", str(ctx.exception))

    def test_the_instance_s_config_lets_the_environment_win(self):
        # what a bake-off arm's LLM_REASONING_EFFORT relies on: the template's config reads the environment last
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "template_config", Path(__file__).resolve().parents[2] / "template" / "shaft" / "config.py")
        config = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"template_config": config}):   # its dataclass looks itself up there
            spec.loader.exec_module(config)
        (self.tmp / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_REASONING_EFFORT="medium"\nLLM_MODEL="openrouter/x"\n')
        cfg = config.load(self.tmp, {"LLM_REASONING_EFFORT": "xhigh", "LLM_MODEL": "claude-haiku-5-5"})
        self.assertEqual((cfg.values["LLM_REASONING_EFFORT"], cfg.values["LLM_MODEL"]), ("xhigh", "claude-haiku-5-5"))

    def test_candidates_run_together_after_the_baseline_and_replay_only_judges_nothing(self):
        started, base_done, overlap = [], threading.Event(), threading.Barrier(2, timeout=5)
        def replay(project, name, st, code, *, model, cache, cache_lock, billed, env):
            if model == "openrouter/xiaomi/mimo-v2.6-pro":
                cache["web"] = "answer"
                base_done.set()
            else:
                self.assertTrue(base_done.is_set())      # never before the baseline has filled the cache
                self.assertEqual(cache.get("web"), "answer")
                overlap.wait()                           # both candidates in flight at once, or this times out
            started.append(model)
            return _rec(model)
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5@xhigh",
                              "openrouter/deepseek/deepseek-v4.1-flash"))
        b["states"] = b["states"][:1]
        with mock.patch.object(trial, "replay_arm", side_effect=replay), \
                mock.patch.object(trial, "judge_pair", side_effect=AssertionError("judged")):
            bakeoff.run(b, parallel=1, project=self.tmp, arm_parallel=2, replay_only=True)
        self.assertEqual(started[0], "openrouter/xiaomi/mimo-v2.6-pro")
        self.assertEqual(len(b["states"][0]["arms"]), 3)
        self.assertEqual(b["states"][0]["outcomes"], {})

    def test_refusals(self):
        for arms, said in ((["openrouter/xiaomi/mimo-v2.6-pro"], "at least one candidate"),
                           (["openrouter/xiaomi/mimo-v2.6-pro", "claude-opus-5-5"], "SHAFT_CLOUD_MODELS"),
                           (["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5@high"], "not 'high'"),
                           (["openrouter/xiaomi/mimo-v2.6-pro", "local"], "claude-… or openrouter/…")):
            with self.assertRaises(bakeoff.BakeoffError) as ctx:
                self.create(arms=arms)
            self.assertIn(said, str(ctx.exception))
        with self.assertRaises(bakeoff.BakeoffError) as ctx:
            self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"), judge="claude-haiku-5-5")
        self.assertIn("never judges its own runs", str(ctx.exception))
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.create("thelos", ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"], ["2026-01-01T00:00:00"],
                           project=self.tmp)

    def test_the_replay_passes_the_arm_s_settings_over_config_env(self):
        seen = {}
        def command(dest, broker, kinds, prof, ports, extra):
            seen.update(extra)
            raise RuntimeError("stop here")
        with mock.patch("supervisor.broker.ReplayBroker"), mock.patch("supervisor.runner.agent_command", command), \
                mock.patch.object(trial, "_files_now", return_value={}), \
                mock.patch.object(trial, "_episode_lines", return_value=set()), \
                mock.patch.object(trial, "_llm_usage", return_value={}), mock.patch.object(trial, "_cloud_usd", return_value=0), \
                mock.patch.object(trial, "secrets_env", return_value={}), \
                mock.patch.object(trial, "_config", return_value={}), \
                mock.patch("supervisor.autonomy.describe_schedule", return_value=""):
            with self.assertRaises(RuntimeError):
                trial._replay_cycle(self.tmp, "thelos", {"t0": "2026-10-29T10:00:00", "start_commit": "c0"}, {},
                                    self.tmp, self.tmp, kind="live", clock=trial.datetime(2026, 10, 29, 10),
                                    model="claude-haiku-5-5", cache={}, cache_lock=threading.Lock(), billed="b",
                                    should_stop=lambda: False, env={"LLM_REASONING_EFFORT": "xhigh"})
        self.assertEqual((seen["LLM_MODEL"], seen["LLM_BASE_URL"], seen["LLM_REASONING_EFFORT"]),
                         ("claude-haiku-5-5", "broker", "xhigh"))


VERDICT = {"winner": "second", "margin": "clear", "first": {"criterion_met": "partial", "claim_supported": True},
           "second": {"criterion_met": "yes", "claim_supported": True}, "harm_first": "", "harm_second": "",
           "reasoning": "r"}


class SubagentJudgeTest(BakeoffTest):
    """Bake-offs judged by Claude Code subagents (export_cases / import_verdicts), not the API judge: the path the
    2026-10-30 bake-offs took through a scratch script that was never in the repo (docs/bakeoffs/README.md)."""

    def replayed(self, arms=("openrouter/xiaomi/mimo-v2.6-pro", "openrouter/deepseek/deepseek-v4.1-flash"), **kw):
        def replay(project, name, st, code, *, model, cache, cache_lock, billed, env):
            rec = _rec(model)
            rec["episode"] = {"intent": {"goal": f"config: LLM_MODEL={model}"}}   # one printed its config (10-30)
            return rec
        b = self.create(arms=arms, **kw)
        with mock.patch.object(trial, "replay_arm", side_effect=replay):
            bakeoff.run(b, parallel=1, project=self.tmp, replay_only=True)
        return b

    def write(self, out, mapping, winner_for_candidate="second"):
        for cid, m in mapping.items():   # the candidate is the second run in AB, the first in BA
            w = winner_for_candidate if m["order"] == "AB" else {"second": "first", "first": "second"}.get(
                winner_for_candidate, winner_for_candidate)
            (out / "verdicts" / f"{cid}.json").write_text(json.dumps({**VERDICT, "winner": w}))

    def test_export_then_import_decides_each_pair_from_both_orders(self):
        b, out = self.replayed(), self.tmp / "judging"
        todo = bakeoff.export_cases(b, out)
        mapping = json.loads(bakeoff.mapping_path(out).read_text())
        self.assertEqual(len(todo), 4)   # 2 cycles × 1 candidate × 2 orders
        self.assertEqual(sorted(m["order"] for m in mapping.values()), ["AB", "AB", "BA", "BA"])
        for cid in todo:
            text = (out / "cases" / f"{cid}.md").read_text()
            self.assertIn("[model]", text)
            for leak in ("deepseek", "mimo", "xiaomi/"):   # blind: no model name, not even the one a run printed
                self.assertNotIn(leak, text.lower())
            self.assertIn(trial.JUDGE_SYSTEM[:60], text)
        self.assertEqual(sorted(bakeoff.export_cases(b, out)), sorted(todo), "exported twice")
        unknown = bakeoff._redactor({"arms": [{"model": "openrouter/acme/zephyr-9"}]})   # no family pattern knows it
        self.assertEqual(unknown("ran openrouter/acme/zephyr-9, i.e. zephyr-9"), "ran [model], i.e. [model]")
        spelled = bakeoff._redactor({"arms": [{"model": "claude-haiku-5-5"}, {"model": "openrouter/google/gemini-4-argon"}]})
        self.assertEqual(spelled("Claude Haiku 5.5; Gemini 4 Argon"), "[model]; [model]")   # review 2026-10-31
        self.write(out, mapping)
        with mock.patch.object(trial, "judge_pair", side_effect=AssertionError("the API judge was called")):
            self.assertEqual(bakeoff.import_verdicts(b, out), [])
            bakeoff.run(b, parallel=1, project=self.tmp)   # a resume never calls the API judge over subagents
        self.assertEqual(b["judge"], "claude-opus-5-5 (Claude Code subagent)")
        outcomes = [s["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"] for s in b["states"]]
        self.assertEqual([o["outcome"] for o in outcomes], ["win", "win"])
        self.assertEqual({o["judge_usd"] for o in outcomes}, {0.0})
        saved = json.loads(bakeoff.path("thelos", b["id"]).read_text())
        self.assertEqual(saved["states"][0]["outcomes"], b["states"][0]["outcomes"])
        self.assertEqual(bakeoff.export_cases(b, out), [])
        rep = bakeoff.report(b)
        self.assertIn("| openrouter/deepseek/deepseek-v4.1-flash | 2 | 0 | 0 | 0 | 0 | 0 | ", rep)
        self.assertIn(f"| {EPS[0]['ts']} | W |", rep)

    def test_grok_xai_muse_names_do_not_leak_when_arm_is_another_model(self):
        redact = bakeoff._redactor({"arms": [{"model": "claude-haiku-5-5"}]})
        for name in ("Grok", "grok-3", "x-ai/grok-4.7", "SpaceXAI", "xAI", "Muse Spark", "muse-spark-open",
                     "Kimi", "kimi-k3.1-pro", "moonshotai/kimi-k3.1-pro", "Moonshot", "Mercury 3",
                     "inception/mercury-3", "Particle-1", "particle-ai/particle-1", "Hy4"):
            with self.subTest(name=name):
                self.assertEqual(redact(name), "[model]")
        for text in ("the muse of history", "muse", "spark", "Luna", "Astra", "xaither", "SpaceXAIfoo",
                     "mercury", "particle", "the particle moved", "moonshots", "kimiko"):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_an_invalid_verdict_leaves_the_pair_unjudged_and_is_named(self):
        b, out = self.replayed(), self.tmp / "judging"
        bakeoff.export_cases(b, out)
        mapping = json.loads(bakeoff.mapping_path(out).read_text())
        self.write(out, mapping)
        bad = next(c for c, m in mapping.items() if m["state"] == 0)
        p = out / "verdicts" / f"{bad}.json"
        p.write_text(p.read_text() + "\nI also think…")   # text after the JSON (10-30): judged again, never repaired
        self.assertEqual(bakeoff.import_verdicts(b, out), [bad])
        self.assertEqual(b["states"][0]["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"], "unjudged")
        self.assertEqual(b["states"][1]["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"], "win")
        self.assertIn(bad, bakeoff.export_cases(b, out))
        nested = {**VERDICT, "first": {"criterion_met": "maybe", "claim_supported": True}}
        p.write_text(json.dumps(nested))
        self.assertIsNone(bakeoff.verdict(out, bad))

    def test_a_verdict_on_runs_replayed_since_is_stale_and_the_pair_is_exported_again(self):
        b, out = self.replayed(), self.tmp / "judging"
        bakeoff.export_cases(b, out)
        self.write(out, json.loads(bakeoff.mapping_path(out).read_text()))
        st = b["states"][0]   # replayed again after export (a resume replays a state cut off part-way in full)
        st["arms"]["openrouter/deepseek/deepseek-v4.1-flash"] = {**_rec("x"), "usd": 0.07}
        self.assertEqual(bakeoff.import_verdicts(b, out), [])
        self.assertEqual(st["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"], "unjudged")
        self.assertEqual(b["states"][1]["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"], "win")
        todo = bakeoff.export_cases(b, out)
        mapping = json.loads(bakeoff.mapping_path(out).read_text())
        self.assertEqual(sorted((mapping[c]["state"], mapping[c]["order"]) for c in todo), [(0, "AB"), (0, "BA")])

    def test_another_instance_s_bake_off_with_the_same_id_never_shares_cases(self):
        b, out = self.replayed(bid="same"), self.tmp / "judging"
        bakeoff.export_cases(b, out)
        self.write(out, json.loads(bakeoff.mapping_path(out).read_text()))
        other = {**json.loads(json.dumps(b)), "name": "wren"}
        self.assertEqual(len(bakeoff.export_cases(other, out)), 4)
        for st in other["states"]:
            st["outcomes"] = {}
        with mock.patch.object(bakeoff, "_save"):
            bakeoff.import_verdicts(other, out)
        self.assertEqual({o["outcome"] for s in other["states"] for o in s["outcomes"].values()}, {"unjudged"})

    def test_a_verdict_with_a_field_the_schema_does_not_have_is_judged_again(self):
        b, out = self.replayed(), self.tmp / "judging"
        cid = bakeoff.export_cases(b, out)[0]
        for extra in ({**VERDICT, "confidence": 0.9},
                      {**VERDICT, "second": {**VERDICT["second"], "notes": "x"}}):
            (out / "verdicts" / f"{cid}.json").write_text(json.dumps(extra))
            self.assertIsNone(bakeoff.verdict(out, cid))
        (out / "verdicts" / f"{cid}.json").write_text(json.dumps(VERDICT))
        self.assertEqual(bakeoff.verdict(out, cid), VERDICT)

    def test_import_refuses_to_mix_with_the_api_judge_s_verdicts(self):
        b, out = self.replayed(), self.tmp / "judging"
        with mock.patch.object(trial, "judge_pair", return_value=VERDICT):
            bakeoff.run(b, parallel=1, project=self.tmp)
        bakeoff.export_cases(b, out)
        with self.assertRaises(bakeoff.BakeoffError) as ctx:
            bakeoff.import_verdicts(b, out)
        self.assertIn("would mix judges", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTest(unittest.TestCase):
    """Defects the 2026-11-08 review found in bake-offs (PLAN-bakeoff-refresh-2026-11-08.md)."""
    setUp, create = BakeoffTest.setUp, BakeoffTest.create
    replayed, write = SubagentJudgeTest.replayed, SubagentJudgeTest.write

    def test_the_key_to_the_cases_is_never_beside_them(self):
        b, out = self.replayed(), self.tmp / "judging"
        bakeoff.export_cases(b, out)
        self.assertFalse((out / "mapping.json").exists())   # a judge listing the folder read which run was which
        self.assertTrue(bakeoff.mapping_path(out).exists())
        self.assertFalse(str(bakeoff.mapping_path(out)).startswith(str(out)))
        legacy = json.loads(bakeoff.mapping_path(out).read_text())
        bakeoff.mapping_path(out).unlink()
        (out / "mapping.json").write_text(json.dumps(legacy))   # an export from before the fix moves out on use
        self.write(out, bakeoff._mapping(out))
        self.assertFalse((out / "mapping.json").exists())
        self.assertEqual(bakeoff.import_verdicts(b, out), [])
        self.assertEqual({s["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"] for s in b["states"]},
                         {"win"})

    def test_a_provider_failure_is_unjudged_and_replayed_again_on_resume(self):
        calls = []
        def replay(project, name, st, code, *, model, cache, cache_lock, billed, env):
            calls.append((st["ts"], model))
            rec = _rec(model)
            if model == "openrouter/deepseek/deepseek-v4.1-flash" and len(calls) <= 4:
                rec.update(completed=False, exit=1, usage={"calls": 17, "errors": 3})   # HTTP 429 (Mercury, 11-05)
            return rec
        b = self.create(arms=("openrouter/xiaomi/mimo-v2.6-pro", "openrouter/deepseek/deepseek-v4.1-flash"))
        with mock.patch.object(trial, "replay_arm", side_effect=replay), \
                mock.patch.object(trial, "judge_pair", side_effect=AssertionError("judged")):
            bakeoff.run(b, parallel=1, project=self.tmp)   # judged: decide() settles it, unjudged, no judge called
            self.assertEqual([s["outcomes"]["openrouter/deepseek/deepseek-v4.1-flash"]["outcome"] for s in b["states"]],
                             ["unjudged", "unjudged"])   # was a loss each (Mercury 3, 11-05)
            bakeoff.run(b, parallel=1, project=self.tmp, replay_only=True)   # the resume replays both states in full
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(r["completed"] for s in b["states"] for r in s["arms"].values()))
        self.assertIn("last_call_failed", b["states"][0]["arms"]["openrouter/deepseek/deepseek-v4.1-flash"])

    def test_the_api_judge_sees_no_model_name(self):
        b = self.replayed()
        seen = []
        def judge(context, first, second, purpose, model=None, redact=None):
            seen.append(redact(trial.judge_case(context, first, second)))
            return VERDICT
        with mock.patch.object(trial, "judge_pair", side_effect=judge):
            bakeoff.run(b, parallel=1, project=self.tmp)
        self.assertEqual(len(seen), 4)
        for text in seen:   # the run printed its config: LLM_MODEL=<model>
            self.assertNotIn("deepseek", text.lower())
            self.assertNotIn("mimo", text.lower())

    def test_report_shows_splits_and_automatic_outcomes_apart_and_reads_old_records_by_todays_rules(self):
        b, out = self.replayed(), self.tmp / "judging"
        label = "openrouter/deepseek/deepseek-v4.1-flash"
        b["states"][0]["outcomes"][label] = {"outcome": "tie", "why": "the two orders disagreed",   # saved before 11-08
                                             "judgments": [{**VERDICT, "winner": "first"}, {**VERDICT, "winner": "first"}],
                                             "judge_usd": 0.0}
        b["states"][1]["arms"][label] = {**_rec(label), "completed": False, "exit": 0}   # did not complete, no error
        b["states"][1]["outcomes"][label] = {"outcome": "loss", "why": "", "judgments": [None, None], "judge_usd": 0.0}
        rep = bakeoff.report(b)
        self.assertIn(f"| {label} | 0 | 1 | 0 | 1 | 0 | 1 | ", rep)   # W L T S unjudged no-judge
        self.assertIn(f"| {EPS[0]['ts']} | S |", rep)
        self.assertNotIn("Per kind of cycle", rep)   # a set chosen by hand
        b["drawn"] = {"since": "x", "until": "y", "population": {"operator": 1, "writes": 1, "check": 8}}
        rep = bakeoff.report(b)
        self.assertIn(f"| {EPS[0]['ts']} (check) | S |", rep)
        self.assertIn(f"| {label} | 0–1–0–1 | -0.50 |", rep)   # a split and a loss: −1 over 2 cycles


class DrawTest(unittest.TestCase):
    """A fresh set of cycles drawn by kind (review 2026-11-08: the 10-28 sets held 4% check-only cycles, 11-08 74%)."""

    def eps(self):
        out = []
        for i in range(30):   # 18 check-only, 8 writing, 4 the operator asked for
            tools = [{"tool": "read_file"}] if i < 18 else [{"tool": "write_file"}]
            origin = "operator" if i >= 26 else "self"
            out.append({"kind": "live", "ts": f"2026-11-08T{i // 2:02d}:{(i % 2) * 30:02d}:00", "ended_by": "control",
                        "intent": {"goal": "g", "origin": origin}, "tools": tools, "inbox": []})
        out[3]["ended_by"] = "budget"
        out[5]["inbox"] = ["gone.md"]
        return out

    def test_draw_holds_every_kind_and_skips_what_cannot_be_replayed_as_it_ran(self):
        eps = self.eps()
        with mock.patch.object(trial, "live_episodes", return_value=eps), \
                mock.patch.object(trial, "_starts", return_value=[]), \
                mock.patch.object(trial, "start_time", side_effect=lambda n, ep, s: datetime.fromisoformat(ep["ts"])), \
                mock.patch.object(bakeoff, "_paused", return_value=[datetime(2026, 11, 8, 2, 0)]), \
                mock.patch("supervisor.mind.show", side_effect=lambda n, r, p, pr: None if p.endswith("gone.md") else "x"):
            d = bakeoff.draw("thelos", 12, since="2026-11-07T00:00:00")
            el = {e["ts"] for e in bakeoff.eligible("thelos", "2026-11-07T00:00:00")}
        for skipped in (eps[3]["ts"], eps[5]["ts"], eps[4]["ts"]):   # budget end, inbox gone, paused at 02:00
            self.assertNotIn(skipped, el)
        self.assertEqual(d["population"], {"operator": 4, "writes": 8, "check": 15})
        kinds = [bakeoff.stratum(next(e for e in eps if e["ts"] == ts)) for ts in d["episodes"]]
        self.assertEqual(len(d["episodes"]), 12)
        self.assertEqual({k: kinds.count(k) for k in bakeoff.STRATA}, {"operator": 4, "writes": 4, "check": 4})
        self.assertEqual(d["episodes"], sorted(d["episodes"]))
        with self.assertRaises(bakeoff.BakeoffError), \
                mock.patch.object(bakeoff, "eligible", return_value=eps[:2]):
            bakeoff.draw("thelos", 12, since="2026-11-07T00:00:00")

    def test_draw_fills_past_the_minimum_by_each_kind_s_share(self):
        pool = [{"ts": f"t{i:02d}", "intent": {}, "tools": [] if i < 70 else [{"tool": "send_message"}]}
                for i in range(100)]
        with mock.patch.object(bakeoff, "eligible", return_value=pool):
            d = bakeoff.draw("thelos", 24, since="x")
        kinds = [bakeoff.stratum(next(e for e in pool if e["ts"] == ts)) for ts in d["episodes"]]
        self.assertEqual((kinds.count("check"), kinds.count("writes")), (17, 7))   # 70/30 of 24, at least 4 each


class ReviewFixesFollowUpTest(unittest.TestCase):
    """The Codex review of the 2026-11-08 fixes."""
    setUp, create = BakeoffTest.setUp, BakeoffTest.create
    replayed, write = SubagentJudgeTest.replayed, SubagentJudgeTest.write

    def test_a_case_written_by_an_older_run_text_is_exported_again(self):
        b, out = self.replayed(), self.tmp / "judging"
        first = bakeoff.export_cases(b, out)
        self.write(out, bakeoff._mapping(out))
        self.assertEqual(bakeoff.export_cases(b, out), [])
        with mock.patch.object(bakeoff, "CASE_VERSION", bakeoff.CASE_VERSION + 1):   # what a judge sees changed
            again = bakeoff.export_cases(b, out)
            self.assertEqual(len(again), 4)
            self.assertFalse(set(again) & set(first))
            bakeoff.import_verdicts(b, out)   # the old verdicts are on the old text: none counts
        self.assertEqual({o["outcome"] for s in b["states"] for o in s["outcomes"].values()}, {"unjudged"})

    def test_an_offset_is_converted_not_dropped(self):
        local = datetime(2026, 11, 8, 9, 0)
        utc = (local - local.astimezone().utcoffset()).strftime("%Y-%m-%dT%H:%M:%SZ")   # the same instant
        self.assertEqual(bakeoff._local(utc), local)
        self.assertEqual(bakeoff._local("2026-11-08T09:00:00"), local)

    def test_draw_refuses_a_size_below_one(self):
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.draw("thelos", 0, since="2026-11-07T00:00:00")
