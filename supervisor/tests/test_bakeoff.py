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
        return bakeoff.create("tundra", list(arms), [e["ts"] for e in EPS], project=self.tmp, **kw)

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
        self.assertTrue(all(c[4].startswith("tundra@bakeoff:") for c in calls))   # never billed to the instance

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
            again = bakeoff.create("tundra", [], [], bid=b["id"], project=self.tmp)   # resumed from disk
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
            bakeoff.create("tundra", ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5@xhigh"], [], bid=bid,
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
            bakeoff.create("tundra", ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5"], ["2026-01-01T00:00:00"],
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
                trial._replay_cycle(self.tmp, "tundra", {"t0": "2026-10-29T10:00:00", "start_commit": "c0"}, {},
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
        saved = json.loads(bakeoff.path("tundra", b["id"]).read_text())
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
        other = {**json.loads(json.dumps(b)), "name": "wisp"}
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
    """Defects the 2026-11-08 review found in bake-offs (a private review record)."""
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
            d = bakeoff.draw("tundra", 12, since="2026-11-07T00:00:00")
            el = {e["ts"] for e in bakeoff.eligible("tundra", "2026-11-07T00:00:00")}
        for skipped in (eps[3]["ts"], eps[5]["ts"], eps[4]["ts"]):   # budget end, inbox gone, paused at 02:00
            self.assertNotIn(skipped, el)
        self.assertEqual(d["population"], {"operator": 4, "writes": 8, "check": 15})
        kinds = [bakeoff.stratum(next(e for e in eps if e["ts"] == ts)) for ts in d["episodes"]]
        self.assertEqual(len(d["episodes"]), 12)
        self.assertEqual({k: kinds.count(k) for k in bakeoff.STRATA}, {"operator": 4, "writes": 4, "check": 4})
        self.assertEqual(d["episodes"], sorted(d["episodes"]))
        with self.assertRaises(bakeoff.BakeoffError), \
                mock.patch.object(bakeoff, "eligible", return_value=eps[:2]):
            bakeoff.draw("tundra", 12, since="2026-11-07T00:00:00")

    def test_draw_fills_past_the_minimum_by_each_kind_s_share(self):
        pool = [{"ts": f"t{i:02d}", "intent": {}, "tools": [] if i < 70 else [{"tool": "send_message"}]}
                for i in range(100)]
        with mock.patch.object(bakeoff, "eligible", return_value=pool):
            d = bakeoff.draw("tundra", 24, since="x")
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
            bakeoff.draw("tundra", 0, since="2026-11-07T00:00:00")


class OperatorJudgeTest(unittest.TestCase):
    """The operator as judge: any two models on one cycle, shown blind as A and B with what each run would have put
    in front of them; verdicts kept apart and set beside the LLM judges' (operator, 2026-11-09)."""
    setUp, create = BakeoffTest.setUp, BakeoffTest.create
    ARMS = ("openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "openrouter/deepseek/deepseek-v4.1-flash")

    def replayed(self, arms=ARMS):
        def replay(project, name, st, code, *, model, cache, cache_lock, billed, env):
            rec = _rec(model)
            rec["trace"] = [{"tool": "send_message", "args": json.dumps({"to": "operator", "text": f"hello from {model}"}),
                             "ok": True, "result": "delivered", "tags": ["simulated: not delivered"]}]
            rec["diff"] = ("--- /dev/null\n+++ b/notes/brief.md\n@@ -0,0 +1 @@\n+a brief <script>x</script>\n"
                           "--- a/memory/journal.md\n+++ b/memory/journal.md\n@@ -1 +1,2 @@\n x\n+y\n")
            rec["episode"] = {"intent": {"goal": "check the inbox"}, "finish": {"what_happened": f"done by {model}"}}
            return rec
        b = self.create(arms=arms)
        with mock.patch.object(trial, "replay_arm", side_effect=replay):
            bakeoff.run(b, parallel=1, project=self.tmp, replay_only=True)
        return b

    def test_the_queue_holds_every_pair_of_the_models_ticked_on_every_cycle(self):
        b = self.replayed()
        todo = bakeoff.operator_queue(b, list(self.ARMS))
        self.assertEqual(len(todo), 2 * 3)   # 2 cycles x 3 pairs, baseline or not
        self.assertIn((0, bakeoff._pair("claude-haiku-5-5", "openrouter/deepseek/deepseek-v4.1-flash")), todo)
        self.assertEqual(len(bakeoff.operator_queue(b, list(self.ARMS[1:]))), 2)   # the two candidates only

    def test_a_choice_reads_back_as_the_model_shown_on_that_side(self):
        b = self.replayed()
        pair = bakeoff._pair("claude-haiku-5-5", "openrouter/deepseek/deepseek-v4.1-flash")
        for flipped, choice, winner in ((False, "A", pair[0]), (False, "B", pair[1]), (True, "A", pair[1]),
                                        (True, "B", pair[0]), (True, "tie", "tie"), (False, "both bad", "both bad")):
            with self.subTest(flipped=flipped, choice=choice), mock.patch.object(bakeoff, "_flipped", return_value=flipped):
                case = bakeoff.operator_case(b, 0, pair, project=self.tmp)
                self.assertEqual(case["A"]["account"], "done by [model]")
                self.assertNotIn("deepseek", json.dumps({k: case[k] for k in ("A", "B", "context")}).lower())
                row = bakeoff.operator_record(b, 0, pair, choice, runs=case["runs"])
                self.assertEqual(row["winner"], winner)
                self.assertEqual(bakeoff.operator_verdicts(b)[(0, pair)]["winner"], winner)
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.operator_record(b, 0, pair, "first", runs=case["runs"])
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.operator_record(b, 0, (pair[0], pair[0]), "A", runs=case["runs"])

    def test_verdicts_from_before_free_pairs_still_count(self):
        b = self.replayed()
        base, cand = self.ARMS[0], self.ARMS[2]
        st = b["states"][1]
        row = {"state": 1, "arm": cand, "outcome": "win", "runs": bakeoff._runs(st["arms"][base], st["arms"][cand])}
        bakeoff.operator_path(b).parent.mkdir(parents=True, exist_ok=True)
        bakeoff.operator_path(b).write_text(json.dumps(row) + "\n")
        v = bakeoff.operator_verdicts(b)[(1, bakeoff._pair(base, cand))]
        self.assertEqual(v["winner"], cand)
        self.assertNotIn((1, bakeoff._pair(base, cand)), bakeoff.operator_queue(b, list(self.ARMS)))

    def test_sides_are_drawn_per_pair(self):
        b = self.replayed()
        self.assertEqual({bakeoff._flipped(b, i, ("a", f"m{k}")) for i in range(20) for k in range(3)}, {True, False})

    def test_a_verdict_on_runs_replayed_since_no_longer_counts_nor_lands(self):
        b = self.replayed()
        pair = bakeoff._pair(*self.ARMS[:2])
        shown = bakeoff.operator_case(b, 0, pair, project=self.tmp)["runs"]
        bakeoff.operator_record(b, 0, pair, "tie", runs=shown)
        b["states"][0]["arms"][self.ARMS[1]] = {**b["states"][0]["arms"][self.ARMS[1]], "usd": 0.09}   # replayed
        self.assertNotIn((0, pair), bakeoff.operator_verdicts(b))
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.operator_record(b, 0, pair, "A", runs=shown)

    def test_summary_counts_every_pair_and_compares_with_the_llm_judges_only_against_the_baseline(self):
        b = self.replayed()
        base, haiku, ds = self.ARMS
        win = {**VERDICT, "winner": "second"}
        b["states"][0]["outcomes"][ds] = {"outcome": "win", "why": "", "judgments": [win, {**win, "winner": "first"}],
                                          "judge_usd": 0}
        for pair, winner_side in (((base, ds), ds), ((haiku, ds), haiku)):
            pair = bakeoff._pair(*pair)
            case = bakeoff.operator_case(b, 0, pair, project=self.tmp)
            a_is = pair[1] if bakeoff._flipped(b, 0, pair) else pair[0]
            bakeoff.operator_record(b, 0, pair, "A" if a_is == winner_side else "B", runs=case["runs"])
        rows = {r["arm"]: r for r in bakeoff.operator_summary(b)}
        self.assertEqual((rows[ds]["W"], rows[ds]["L"], rows[ds]["judged"]), (1, 1, 2))
        self.assertEqual((rows[ds]["agree"], rows[ds]["compared"]), (1, 1))   # only the pair with the baseline
        self.assertEqual((rows[haiku]["W"], rows[base]["L"]), (1, 1))


class OperatorJudgeReviewTest(unittest.TestCase):
    """The Codex review of the operator pages (2026-11-09)."""
    setUp, create = BakeoffTest.setUp, BakeoffTest.create
    CAND = "openrouter/deepseek/deepseek-v4.1-flash"

    def test_the_diff_is_read_by_headers_and_hunk_lengths(self):
        diff = trial.file_diff({"notes/a.md": b"-- separator\nold\n", "notes/gone.md": b"x\n"},
                               {"notes/a.md": b"new\n++important\n", "notes/img.png": b"\xff\xfe\x00"}, limit=100000)
        files = {f["path"]: f for f in bakeoff._diff_files(diff)}
        self.assertEqual(files["notes/a.md"]["added"], ["new", "++important"])   # was lost to '--- '/'+++' lines
        self.assertTrue(files["notes/gone.md"]["deleted"])
        self.assertTrue(files["notes/img.png"]["binary"])

    def test_paths_and_attachments_are_redacted_too(self):
        rec = {"trace": [{"tool": "send_message", "ok": True, "result": "ok", "args": json.dumps(
                   {"to": "operator", "text": "t", "attachments": ["notes/deepseek-report.md"]})}],
               "diff": "--- /dev/null\n+++ b/notes/deepseek-report.md\n@@ -0,0 +1 @@\n+x\n", "episode": {}}
        shown = json.dumps(bakeoff._facing(rec, bakeoff._redactor({"arms": [{"model": self.CAND}]})))
        self.assertNotIn("deepseek", shown.lower())

    def test_a_hyphenated_instance_name_is_listed_whole(self):
        inst = self.tmp / "instances"
        (inst / "wisp-east").mkdir(parents=True)
        (inst / "wisp").mkdir()
        (self.tmp / "wisp-east-x1.json").write_text("{}")
        with mock.patch.object(bakeoff, "INSTANCES", inst):
            got = {(s["name"], s["id"]) for s in bakeoff.saved()}
        self.assertIn(("wisp-east", "x1"), got)


class OperatorEloTest(unittest.TestCase):
    """The ranking on the bake-off page: every model starts at 1000 and moves with the operator's verdicts on any
    pair (operator, 2026-11-09: 'start everyone at 1000')."""

    def test_with_no_verdict_everyone_is_at_1000(self):
        r = bakeoff.bradley_terry([], ["a", "b", "c"])
        self.assertEqual({v["elo"] for v in r.values()}, {1000})
        self.assertEqual({v["n"] for v in r.values()}, {0})

    def test_a_win_moves_both_models_the_same_distance_from_1000(self):
        r = bakeoff.bradley_terry([("a", "b", 1.0)], ["a", "b", "c"])
        self.assertGreater(r["a"]["elo"], 1000)
        self.assertEqual(r["a"]["elo"] - 1000, 1000 - r["b"]["elo"])
        self.assertEqual(r["c"]["elo"], 1000)   # not in the pair: where it started
        tie = bakeoff.bradley_terry([("a", "b", 0.5)], ["a", "b"])
        self.assertEqual((tie["a"]["elo"], tie["b"]["elo"]), (1000, 1000))

    def test_a_sweep_stays_finite_and_more_pairs_narrow_the_range(self):
        sweep = bakeoff.bradley_terry([("a", "b", 1.0)] * 8, ["a", "b"])
        self.assertTrue(1000 < sweep["a"]["elo"] < 2000)
        one = bakeoff.bradley_terry([("a", "b", 1.0), ("a", "b", 0.0)], ["a", "b"])["a"]
        many = bakeoff.bradley_terry([("a", "b", 1.0), ("a", "b", 0.0)] * 20, ["a", "b"])["a"]
        self.assertGreater(one["hi"] - one["lo"], many["hi"] - many["lo"])

    def test_models_that_never_met_are_ordered_through_the_others(self):
        games = [("a", "base", 1.0)] * 6 + [("a", "base", 0.0)] * 2 + [("b", "a", 1.0)] * 6 + [("b", "a", 0.0)] * 2
        r = bakeoff.bradley_terry(games, ["base", "a", "b"])
        self.assertGreater(r["b"]["elo"], r["a"]["elo"])
        self.assertGreater(r["a"]["elo"], r["base"]["elo"])
        self.assertEqual((r["a"]["n"], r["b"]["n"]), (16, 8))

    def test_ranking_orders_by_the_operator_s_elo_and_sets_the_judges_beside_it(self):
        b = {"name": "wisp", "id": "x", "created": "c", "arms": [{"label": "base"}, {"label": "c1"}, {"label": "c2"},
                                                                {"label": "c3"}], "states": []}
        verdicts = {(0, ("base", "c1")): {"pair": ["base", "c1"], "winner": "base"},
                    (1, ("base", "c1")): {"pair": ["base", "c1"], "winner": "base"},
                    (0, ("base", "c2")): {"pair": ["base", "c2"], "winner": "c2"}}
        with mock.patch.object(bakeoff, "operator_verdicts", return_value=verdicts):
            rank = bakeoff.ranking(b)
        self.assertEqual(rank[0]["arm"], "c2")
        self.assertEqual(rank[-1]["arm"], "c1")
        self.assertEqual(next(r for r in rank if r["arm"] == "c3")["you"]["elo"], 1000)
        self.assertEqual({r["llm"]["elo"] for r in rank}, {1000})   # no LLM verdict in this record


class OperatorPendingTest(unittest.TestCase):
    """An empty-inbox cycle is judged by what was waiting: the queue and the open promises at its start (operator,
    2026-11-09: 'hard to judge when the inbox is empty')."""

    def test_open_tasks_and_unkept_promises_as_of_the_cycle_s_start(self):
        queue = [{"id": 1, "status": "done", "task": "old", "due_at": "2026-11-01T09:00"},
                 {"id": 2, "status": "open", "task": "weekly re-arm", "due_at": "2026-11-07T18:00"},
                 {"id": 3, "status": "waiting", "task": "reply from Luigi", "due_at": "2026-11-07T09:00"},
                 {"id": 4, "status": "backlog", "task": "someday"}]
        promises = "\n".join(json.dumps(e) for e in (
            {"event": "made", "id": "c1", "promise": "remind at 15:00", "due": "2026-11-07T15:00:00", "made_to": "operator"},
            {"event": "made", "id": "c2", "promise": "kept already", "due": "2026-11-06T10:00:00"},
            {"event": "settled", "id": "c2", "status": "kept"}))
        files = {"notes/queue.json": json.dumps(queue), "memory/commitments.jsonl": promises}
        st = {"t0": "2026-11-07T14:13:21"}
        with mock.patch.object(trial, "mind_rev", return_value="r"), \
                mock.patch("supervisor.mind.show", side_effect=lambda n, r, path, p: files.get(path)):
            got = bakeoff._pending({"name": "tundra"}, st, lambda t: t)
        self.assertEqual([(x["id"], x["late"]) for x in got["queue"]], [(3, True), (2, False)])
        self.assertEqual([(c["promise"], c["late"]) for c in got["commitments"]], [("remind at 15:00", False)])
        self.assertEqual(got["backlog"], 1)
        with mock.patch.object(trial, "mind_rev", side_effect=trial.TrialError("no mind")):
            self.assertEqual(bakeoff._pending({"name": "x"}, st, lambda t: t)["queue"], [])


class OperatorSetTest(unittest.TestCase):
    """A set of the cycles that set out to do what the operator asked, shown with the operator's own last chat
    before them (operator, 2026-11-09: 'the others are too hard to judge')."""

    def test_draw_of_one_kind_only(self):
        pool = [{"ts": f"t{i:02d}", "intent": {"origin": "operator" if i % 3 == 0 else "self"}, "tools": []}
                for i in range(60)]
        with mock.patch.object(bakeoff, "eligible", return_value=pool):
            d = bakeoff.draw("tundra", 8, since="x", kind="operator")
            self.assertEqual(len(d["episodes"]), 8)
            self.assertTrue(all(int(ts[1:]) % 3 == 0 for ts in d["episodes"]))
            self.assertEqual((d["kind"], d["population"]["operator"]), ("operator", 20))
            with self.assertRaises(bakeoff.BakeoffError):
                bakeoff.draw("tundra", 21, since="x", kind="operator")
            with self.assertRaises(bakeoff.BakeoffError):
                bakeoff.draw("tundra", 3, since="x", kind="nonsense")

    def test_the_operator_s_last_chat_before_the_cycle(self):
        chats = [{"instance": "tundra", "turns": [
                     {"role": "user", "ts": "2026-11-05T10:41:00", "content": "Prepara la risposta a Concordia\n\n"
                      "[Web chat: a Markdown file you write under notes/docs/ ...]"},
                     {"role": "assistant", "ts": "2026-11-05T10:42:00", "content": "Lo faccio nel prossimo ciclo"},
                     {"role": "user", "ts": "2026-11-05T13:00:00", "content": "after the cycle began"}]},
                 {"instance": "tundra", "turns": [{"role": "user", "ts": "2026-11-03T09:00:00", "content": "too old"}]}]
        with mock.patch("supervisor.web_chat._all_chats", return_value=chats):
            got = bakeoff._asked("tundra", "2026-11-05T12:33:26", lambda t: t)
        self.assertEqual([(t["role"], t["text"]) for t in got],
                         [("you", "Prepara la risposta a Concordia"), ("the instance", "Lo faccio nel prossimo ciclo")])
        with mock.patch("supervisor.web_chat._all_chats", return_value=chats[1:]):
            self.assertEqual(bakeoff._asked("tundra", "2026-11-05T12:33:26", lambda t: t), [])


class InstanceRankingTest(unittest.TestCase):
    """One ranking per instance across its bake-offs (operator, 2026-11-09: 'the ranking must carry over from one
    bake-off to the next')."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-rank-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(bakeoff, "DIR", self.tmp), mock.patch.object(bakeoff, "INSTANCES", self.tmp / "i")):
            p.start()
            self.addCleanup(p.stop)
        bakeoff._POOLED.clear()
        bakeoff._LOADED.clear()

    def record(self, bid, arms, verdicts):
        rec = {"exit": 0, "ended": "ok", "completed": True, "usd": 0, "seconds": 1, "usage": {}, "trace": [], "diff": ""}
        b = {"id": bid, "name": "tundra", "created": "c", "arms": [{"label": a} for a in arms],
             "states": [{"ts": "t", "t0": "t", "arms": {a: dict(rec, model=a) for a in arms}, "outcomes": {}}]}
        (self.tmp / f"tundra-{bid}.json").write_text(json.dumps(b))
        (self.tmp / "operator").mkdir(exist_ok=True)
        rows = [{"state": 0, "pair": list(bakeoff._pair(x, y)), "winner": w,
                 "runs": bakeoff._pair_runs(b["states"][0], bakeoff._pair(x, y))} for x, y, w in verdicts]
        (self.tmp / "operator" / f"tundra-{bid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return b

    def test_verdicts_from_every_bake_off_of_the_instance_make_one_ranking(self):
        b1 = self.record("n24", ["sol", "ds", "argon"], [("sol", "ds", "ds"), ("sol", "argon", "sol")])
        b2 = self.record("op24", ["sol", "ds", "hy4"], [("ds", "hy4", "ds")])
        r1, r2 = bakeoff.ranking(b1), bakeoff.ranking(b2)
        self.assertEqual([(r["arm"], r["you"]["elo"]) for r in r1], [(r["arm"], r["you"]["elo"]) for r in r2])
        self.assertEqual(r1[0]["arm"], "ds")                       # 2 wins across the two bake-offs
        self.assertEqual({r["arm"] for r in r1}, {"sol", "ds", "argon", "hy4"})
        here = {r["arm"]: r["here"] for r in r2}
        self.assertEqual((here["ds"], here["sol"]), (1, 0))       # pairs judged in this bake-off
        self.assertFalse(next(r for r in r2 if r["arm"] == "argon")["in_this"])
        alone = bakeoff.ranking(None, "tundra")                    # the list page: the instance on its own
        self.assertEqual([r["arm"] for r in alone], [r["arm"] for r in r1])
        self.assertEqual(bakeoff.judged_instances(), ["tundra"])


class RankingCostTest(InstanceRankingTest):
    """$/cycle on the ranking (operator, 2026-11-09): billed dollars, or a plan arm's tokens at list price."""

    def test_cost_per_cycle_billed_or_at_list_price_for_a_plan_arm(self):
        usage = self.tmp / "cloud_usage.jsonl"
        rows = [{"instance": "tundra@bakeoff:n24:s0:codex/gpt-6.1-sol", "input_tokens": 200_000,
                 "cached_input_tokens": 150_000, "output_tokens": 1000},
                {"instance": "tundra@bakeoff:other:s0:codex/gpt-6.1-sol", "input_tokens": 9_000_000}]   # another bake-off
        usage.write_text("".join(json.dumps(r) + "\n" for r in rows))
        b = self.record("n24", ["codex/gpt-6.1-sol", "ds"], [("codex/gpt-6.1-sol", "ds", "ds")])
        b["states"][0]["arms"]["ds"]["usd"] = 0.04
        (self.tmp / "tundra-n24.json").write_text(json.dumps(b))
        bakeoff._COSTS.clear()
        with mock.patch("supervisor.cloudllm.USAGE", usage):
            cost = {r["arm"]: r["cost"] for r in bakeoff.ranking(b)}
        self.assertEqual(cost["ds"], {"usd": 0.04, "plan": False})
        self.assertTrue(cost["codex/gpt-6.1-sol"]["plan"])
        # 50K uncached at $2/M + 150K cached at $0.10/M + 1K out at $10/M
        self.assertAlmostEqual(cost["codex/gpt-6.1-sol"]["usd"], 0.1 + 0.015 + 0.01)


class OperatorNextTest(InstanceRankingTest):
    """The next pair shown: a balanced random draw, not a fixed order (operator, 2026-11-09: 'randomize better')."""

    def test_avoids_the_last_cycles_and_pairs_and_favours_the_models_judged_least(self):
        import random
        arms = ["a", "b", "c", "d"]
        b = self.record("x", arms, [("a", "b", "a")])
        b["states"] += [json.loads(json.dumps(b["states"][0])) for _ in range(3)]   # four cycles
        (self.tmp / "tundra-x.json").write_text(json.dumps(b))
        seen, pairs = set(), []
        for k in range(300):
            got = bakeoff.operator_next([b], rng=random.Random(k))
            b_, i, pair, left = got
            self.assertNotEqual(i, 0)                                # cycle 0 was just judged
            self.assertNotEqual(set(pair), {"a", "b"})               # nor the two models judged together
            seen.add(i)
            pairs.append(pair)
        self.assertEqual(seen, {1, 2, 3})                            # drawn, not one fixed cycle
        self.assertEqual(len(set(pairs)), 5)                         # every other pair comes up...
        self.assertGreater(pairs.count(("c", "d")), pairs.count(("a", "c")))   # ...the never-judged models more often
        self.assertEqual(left, 4 * 6 - 1)

    def test_draws_across_bake_offs_and_returns_none_when_all_are_judged(self):
        import random
        b1 = self.record("n24", ["a", "b"], [("a", "b", "a")])
        b2 = self.record("op24", ["a", "b"], [])
        nxt = bakeoff.operator_next([b1, b2], rng=random.Random(1))
        self.assertEqual((nxt[0]["id"], nxt[3]), ("op24", 1))       # the only pair left is in the other bake-off
        self.assertIsNone(bakeoff.operator_next([b1]))
