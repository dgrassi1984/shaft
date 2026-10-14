"""Trials (PLAN-trials.md): a past cycle is rebuilt faithfully, replayed with no side effects, judged blind in both
orders, and the verdict reaches the instance and its probation. Local-model trials never take an instance's time."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, metrics, trial
from supervisor.core import SUP_DIR
from supervisor.tests.fakellm import FakeLLM

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
NAME = "tw"


def git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env).stdout


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".tmp" not in p.parts and ".git" != p.name}


class Fixture(unittest.TestCase):
    """A project with one instance whose branch holds: C0 (09:00, the state a cycle began from) and C1 (10:20,
    after that cycle: the message it read archived, its episode written). The supervisor log says the cycle
    started at 10:00."""

    llm_url = "http://127.0.0.1:9/v1"

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-trial-proj-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / NAME
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", f"instance/{NAME}", str(self.root))
        (self.project / "instances" / "x" / "shaft").mkdir(parents=True)   # another instance to message
        (self.project / "instances" / "x" / "config.env").write_text('LLM_BASE_URL="broker"\n')
        shutil.copytree(TEMPLATE / "shaft", self.root / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / ".gitignore").write_text("logs/\n.tmp/\n__pycache__/\nsecrets/\n")
        (self.root / "config.env").write_text(f'LLM_BASE_URL="{self.llm_url}"\nLLM_MODEL="fake"\n'
                                              f'WAKE_INTERVAL_MINUTES=120\n')
        for rel, text in {"self/self.md": "I am tw.\n", "self/goals.md": "- G1: learn the OLD-GOAL thing\n",
                          "memory/journal.md": "\n---\n## 2026-10-10 08:00 — Born\n\nhello\n"}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", f"{NAME}: live 2026-10-10 09:00", when="2026-10-10T09:00:00")
        self.c0 = git(self.root, "rev-parse", "HEAD").strip()
        (self.root / "memory" / "messages").mkdir(parents=True)
        (self.root / "memory" / "messages" / "20261010-100500-from-x.md").write_text(
            "From: x\nTo: tw\n\nPlease look at the PURPLE-OWL question.\n")
        (self.root / "memory" / "episodes").mkdir(parents=True)
        self.ep = {"ts": "2026-10-10T10:20:00", "kind": "live", "intent": {"goal": "answer x", "at":
                   "2026-10-10T10:01:00", "success_criterion": "c", "confidence": 50},
                   "finish": {"criterion_met": "yes"}, "inbox": ["20261010-100500-from-x.md"], "tools": []}
        self.ep2 = {**self.ep, "ts": "2026-10-10T11:20:00", "inbox": [], "intent": {**self.ep["intent"],
                                                                                 "at": "2026-10-10T11:01:00"}}
        (self.root / "memory" / "episodes" / "2026-10-10.jsonl").write_text(
            json.dumps(self.ep) + "\n" + json.dumps(self.ep2) + "\n")
        (self.root / "self" / "goals.md").write_text("- G1: NEW-GOAL written after the cycle\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", f"{NAME}: live 2026-10-10 10:20", when="2026-10-10T10:20:30")
        self.c1 = git(self.root, "rev-parse", "HEAD").strip()
        with (SUP_DIR / "supervisor.log").open("a") as f:
            f.write(f"[2026-10-10 10:00:00] {NAME}: start live cycle_timeout_s=2400\n"
                    f"[2026-10-10 11:00:00] {NAME}: start live cycle_timeout_s=2400\n")

    def state(self) -> dict:
        st = trial.state_of(self.project, NAME, self.ep)
        self.assertIsNotNone(st)
        return st


class ReconstructTest(Fixture):
    def test_the_state_is_the_branch_when_the_cycle_began_plus_the_inbox_it_read(self):
        st = self.state()
        self.assertEqual(st["start_commit"], self.c0)
        self.assertEqual(st["t0"], "2026-10-10T10:00:00")
        dest = Path(tempfile.mkdtemp(prefix="shaft-recon-")) / NAME
        self.addCleanup(shutil.rmtree, dest.parent, True)
        code = trial.committed_code(self.project, NAME)
        self.assertIn("shaft/live.py", code)
        trial.reconstruct(self.project, NAME, st, code, dest)
        self.assertIn("OLD-GOAL", (dest / "self" / "goals.md").read_text())          # not the later goals
        self.assertIn("PURPLE-OWL", (dest / "inbox" / "20261010-100500-from-x.md").read_text())
        self.assertFalse((dest / "memory" / "messages").exists())                     # archived only after
        self.assertEqual((dest / "shaft" / "live.py").read_bytes(), code["shaft/live.py"])
        self.assertFalse((dest / "logs").exists())

    def test_an_arm_keeps_its_committed_config_env_although_the_gate_no_longer_counts_it_as_code(self):
        # The gate dropped config.env from CODE_PATHS (an operator's mid-cycle edit was reverted as the instance's,
        # kant 2026-10-13); trials read each arm's model from it, and lost it when they shared the gate's paths.
        self.assertNotIn("config.env", trial.gate.CODE_PATHS)
        code = trial.committed_code(self.project, NAME)
        self.assertIn("config.env", code)
        self.assertEqual(trial.model_of(code)["model"], "fake")

    def test_code_is_replaced_wholesale_and_deletions_apply(self):
        st = self.state()
        dest = Path(tempfile.mkdtemp(prefix="shaft-recon-")) / NAME
        self.addCleanup(shutil.rmtree, dest.parent, True)
        code = {"shaft/only.py": b"x = 1\n", "skills/s.md": None, "config.env": b'LLM_MODEL="f"\n'}
        trial.reconstruct(self.project, NAME, st, code, dest)
        self.assertEqual(sorted(p.name for p in (dest / "shaft").iterdir()), ["only.py"])

    def test_no_start_found_means_no_state(self):
        ep = {**self.ep, "ts": "2026-10-01T10:00:00", "intent": None}
        self.assertIsNone(trial.state_of(self.project, NAME, ep))


class ReplayEndToEndTest(Fixture):
    """The real agent code, sandboxed, against a scripted model: everything it does lands in the scratch copy or
    in the trace, never in the world or the instance."""

    def setUp(self):
        self.fake = FakeLLM([
            {"tool_calls": [("declare_intent", {"goal": "answer x about the owl", "success_criterion": "reply sent",
                                                "confidence": 60, "origin": "instance",
                                                "origin_ref": "x's message about the owl"})]},
            {"tool_calls": [("send_message", {"to": "operator", "text": "hello Daniele", "kind": "note"})]},
            {"tool_calls": [("send_message", {"to": "x", "text": "the owl is purple"})]},
            {"tool_calls": [("http_request", {"url": "https://example.com/v1/post", "method": "POST",
                                              "body": {"a": 1}})]},
            {"tool_calls": [("secret_set", {"name": "gh", "value": "SECRETVALUE", "hosts": ["api.github.com"]})]},
            {"tool_calls": [("delegate_coding", {"task": "write a plugin that does something useful"})]},
            {"tool_calls": [("run", {"command": "mkdir -p notes && echo ran > notes/ran.txt"})]},
            {"tool_calls": [("run", {"command": "curl -s -o /dev/null -w 'status %{http_code}' "
                                                "-X POST https://example.com/hook || true"})]},
            {"tool_calls": [("write_file", {"path": "notes/answer.md", "content": "The owl is purple."})]},
            {"tool_calls": [("finish_cycle", {"what_happened": "replied", "criterion_met": "yes", "learned": "-"})]},
        ])
        self.addCleanup(self.fake.close)
        self.llm_url = self.fake.url
        super().setUp()

    def test_replay_has_no_side_effects(self):
        before, tip = snapshot(self.root), git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(broker, "notify") as notify, \
                mock.patch.object(broker.Broker, "_send", side_effect=AssertionError("a write left the broker")):
            rec = trial.replay_arm(self.project, NAME, self.state(), trial.committed_code(self.project, NAME),
                                   model="local", cache={}, cache_lock=threading.Lock(), billed=f"{NAME}@trial:t")
        self.assertTrue(rec["completed"], rec["output_tail"])
        notify.assert_not_called()
        self.assertFalse((self.project / ".supervisor" / "operator-inbox").exists())
        self.assertFalse((self.project / "instances" / "x" / "inbox").exists())
        self.assertFalse((self.root / "secrets").exists())
        self.assertEqual(snapshot(self.root), before)                          # the instance folder is untouched
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), tip)
        tags = {c["tool"]: c["tags"] for c in rec["trace"]}
        self.assertIn("simulated: not delivered", tags["send_message"])
        self.assertIn("simulated: not sent", tags["http_request"])
        self.assertIn("simulated: not stored", tags["secret_set"])
        # A replay's own network traffic never leaves either (open egress since 2026-10-14): refused and traced.
        self.assertIn("simulated: not sent", tags["network"])
        self.assertTrue(any("example.com:443" in c["args"] for c in rec["trace"] if c["tool"] == "network"))
        self.assertNotIn("SECRETVALUE", json.dumps(rec["trace"]))
        results = {c["tool"]: c["result"] for c in rec["trace"]}
        self.assertTrue(results["http_request"].startswith("HTTP 200"))
        self.assertIn("not available", results["delegate_coding"])
        self.assertIn("notes/answer.md", rec["diff"])
        self.assertIn("notes/ran.txt", rec["diff"])                             # `run` acted on the copy
        first_prompt = json.dumps(self.fake.requests[0])
        self.assertIn("PURPLE-OWL", first_prompt)                               # the inbox it read then
        self.assertIn("OLD-GOAL", first_prompt)                                 # its goals then
        self.assertNotIn("NEW-GOAL", first_prompt)



class PreemptTest(Fixture):
    def setUp(self):
        from supervisor.tests.test_pause import SlowModel
        self.slow = SlowModel(delay=60)
        self.addCleanup(self.slow.close)
        self.llm_url = self.slow.url
        super().setUp()

    def test_a_replay_is_cut_off_when_the_model_is_needed(self):
        t0 = time.monotonic()
        rec = trial.replay_arm(self.project, NAME, self.state(), trial.committed_code(self.project, NAME),
                               model="local", cache={}, cache_lock=threading.Lock(), billed=f"{NAME}@trial:t",
                               should_stop=lambda: time.monotonic() - t0 > 3)
        self.assertEqual(rec["ended"], "preempted")
        self.assertLess(time.monotonic() - t0, 20)
        self.assertFalse(rec["completed"])


class ReplayBrokerTest(Fixture):
    def make(self, cache=None, lock=None, as_of="2026-10-10T10:00:00", state_root=None) -> broker.ReplayBroker:
        return broker.ReplayBroker(NAME, self.project, {}, SUP_DIR / "trials" / "broker.log",
                                   state_root=state_root or self.root, start_commit=self.c0,
                                   as_of=datetime.fromisoformat(as_of), cache={} if cache is None else cache,
                                   cache_lock=lock or threading.Lock(), billed="tw@trial:x",
                                   tools_log=SUP_DIR / "broker.log")

    def test_both_runs_of_a_pair_see_the_same_world(self):
        cache, lock, n = {}, threading.Lock(), iter(range(100))
        with mock.patch.object(broker.Broker, "_web_search", lambda self, q, page=1: f"result {next(n)}"):
            a, b = self.make(cache, lock), self.make(cache, lock)
            ra, rb = a.call("web_search", {"query": "owls"}), b.call("web_search", {"query": "owls"})
        self.assertEqual(ra["result"], rb["result"])
        self.assertIn("same answer as the other run", b.trace[-1]["tags"])

    def test_writes_read_like_the_real_thing(self):
        """A run must not be able to tell it is a replay from what its writes return."""
        real = broker.Broker(NAME, self.project, {}, self.project / "real.log")
        replay = self.make()
        with mock.patch.object(broker, "notify"):
            for args in ({"to": "x", "text": "hi"}, {"to": "operator", "text": "hi", "kind": "question"},
                         {"to": "nobody", "text": "hi"}, {"to": "x", "text": " "}):
                r1, r2 = real.call("send_message", args), replay.call("send_message", args)
                self.assertEqual(r1, r2, args)
        args = {"name": "gh", "value": "v", "hosts": ["api.github.com"]}
        self.assertEqual(real.call("secret_set", args), replay.call("secret_set", args))
        self.assertEqual(real.call("secret_delete", {"name": "nope"}), replay.call("secret_delete", {"name": "nope"}))
        refused = {"url": "https://example.org/x", "method": "TRACE"}
        self.assertEqual(real.call("http_request", refused), replay.call("http_request", refused))

    def test_the_operator_daily_limit_counts_the_replay_day(self):
        replay = self.make()
        with mock.patch.object(broker, "notify"), mock.patch.object(broker, "OPERATOR_DAILY_LIMIT", 3):
            ok = [replay.call("send_message", {"to": "operator", "text": f"q{i}"})["ok"]
                  for i in range(4)]
        self.assertEqual(ok, [True] * 3 + [False])

    def test_history_ends_where_the_cycle_began(self):
        log = self.make().call("history", {})
        self.assertTrue(log["ok"], log)
        self.assertIn("09:00", log["result"])
        self.assertNotIn("10:20", log["result"])
        later = self.c1[:10]
        self.assertFalse(self.make().call("history", {"commit": later})["ok"])

    def test_record_shows_only_what_was_known_then(self):
        jpath = SUP_DIR / "judgments" / f"{NAME}.jsonl"
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text(json.dumps({"episode_ts": "a", "judged_at": "2026-10-10T09:30:00", "reasoning": "EARLY"})
                         + "\n" + json.dumps({"episode_ts": "b", "judged_at": "2026-10-10T10:30:00",
                                              "reasoning": "LATE"}) + "\n")
        self.addCleanup(jpath.unlink, True)
        text = self.make().call("record", {"kind": "judgments"})["result"]
        self.assertIn("EARLY", text)
        self.assertNotIn("LATE", text)
        runs = self.make(as_of="2026-10-10T09:59:00").call("record", {"kind": "runs"})["result"]
        self.assertNotIn("10:00:00", runs)

    def test_no_coder_and_no_nested_trial(self):
        b = self.make()
        self.assertFalse(b.call("delegate_coding", {"task": "x" * 40})["ok"])
        self.assertFalse(b.call("trial", {"hypothesis": "x" * 30, "files": []})["ok"])

    def test_cloud_spend_is_billed_to_the_trial(self):
        self.assertEqual(self.make().usage_name, "tw@trial:x")
        self.assertEqual(broker.Broker(NAME, self.project, {}, self.project / "l").usage_name, NAME)


class VerdictTest(unittest.TestCase):
    def test_sign_test_thresholds(self):
        self.assertEqual(trial.verdict(["win"] * 4 + ["tie"] * 2)["label"], "better")      # p = 0.0625
        self.assertEqual(trial.verdict(["win"] * 5 + ["loss"])["label"], "no clear difference")   # p = 0.109
        self.assertEqual(trial.verdict(["loss"] * 5 + ["tie"])["label"], "worse")
        self.assertEqual(trial.verdict(["tie"] * 6)["label"], "no clear difference")
        self.assertEqual(trial.verdict(["win", "loss"] * 3)["label"], "no clear difference")
        self.assertEqual(trial.verdict(["win"] * 7 + ["loss"] + ["tie"] * 2)["label"], "better")  # extended

    def test_a_state_counts_only_when_both_orders_agree(self):
        ok = {"A": {"completed": True}, "B": {"completed": True}}
        j = lambda w: {"winner": w}   # noqa: E731
        self.assertEqual(trial.decide(ok, [j("second"), j("first")])[0], "win")    # B both times
        self.assertEqual(trial.decide(ok, [j("first"), j("second")])[0], "loss")   # A both times
        self.assertEqual(trial.decide(ok, [j("first"), j("first")])[0], "tie")     # position bias
        self.assertEqual(trial.decide(ok, [j("second"), None])[0], "unjudged")

    def test_a_run_that_did_not_complete_loses_whatever_the_judge_says(self):
        self.assertEqual(trial.decide({"A": {"completed": True}, "B": {"completed": False}}, [None, None])[0], "loss")
        self.assertEqual(trial.decide({"A": {"completed": False}, "B": {"completed": True}}, [None, None])[0], "win")


class RequestTest(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "notes" / "proposals").mkdir(parents=True)
        new = (self.root / "shaft" / "prompts.py").read_text() + "\n# a proposed tweak\n"
        (self.root / "notes" / "proposals" / "prompts.py").write_text(new)
        self.files = [{"path": "shaft/prompts.py", "from": "notes/proposals/prompts.py"}]
        self.addCleanup(shutil.rmtree, SUP_DIR / "trials" / NAME, True)

    def ask(self, **kw):
        args = {"files": self.files, "hypothesis": "clearer goals make cycles more useful", "episodes": [], "n": 0,
                "extend": ""}
        args.update(kw)
        return trial.request(NAME, project=self.project, **args)

    def test_a_valid_request_is_frozen_and_queued_for_its_tests(self):
        out = self.ask()
        self.assertIn("queued", out)
        t = trial.all_trials(NAME)[-1]
        self.assertEqual(t["status"], "testing")
        self.assertEqual(len(t["states"]), 2)
        cand = trial._files(NAME, t["id"], "candidate")
        self.assertIn(b"a proposed tweak", cand["shaft/prompts.py"])
        (self.root / "notes" / "proposals" / "prompts.py").write_text("changed after asking\n")
        self.assertIn(b"a proposed tweak", trial._files(NAME, t["id"], "candidate")["shaft/prompts.py"])
        with self.assertRaises(trial.TrialError) as ctx:
            self.ask()
        self.assertIn("one at a time", str(ctx.exception))

    def test_refusals(self):
        (self.root / "notes" / "same.py").write_text((self.root / "shaft" / "prompts.py").read_text())
        (self.root / "notes" / "cfg.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="other"\n')
        for kw, why in (({"files": [{"path": "shaft/prompts.py", "from": "notes/same.py"}]}, "identical"),
                        ({"files": [{"path": "self/self.md", "from": "notes/same.py"}]}, "only files under"),
                        ({"files": [{"path": "../x/shaft/a.py", "from": "notes/same.py"}]}, "only files under"),
                        ({"files": [{"path": "config.env", "from": "notes/cfg.env"}]}, "not models"),
                        ({"files": [{"path": "shaft/a.py", "from": "../../etc/passwd"}]}, "outside"),
                        ({"hypothesis": "x"}, "hypothesis")):
            with self.subTest(why=why), self.assertRaises(trial.TrialError) as ctx:
                self.ask(**kw)
            self.assertIn(why, str(ctx.exception))

    def test_daily_cap_names_its_knob(self):
        with mock.patch.object(trial, "TRIALS_PER_DAY", 0):
            with self.assertRaises(trial.TrialError) as ctx:
                self.ask()
        self.assertIn("SHAFT_TRIALS_PER_DAY", str(ctx.exception))


class LocalTimeTest(Fixture):
    """Daniele (2026-10-13): a trial "should not cannibalise its time. Otherwise, it can use Mimo"."""

    llm_url = "http://127.0.0.1:8780/v1"

    def setUp(self):
        super().setUp()
        self.sched = SUP_DIR / "schedule.json"
        self.old = self.sched.read_text() if self.sched.exists() else None
        self.addCleanup(self.restore)

    def restore(self):
        if self.old is None:
            self.sched.unlink(missing_ok=True)
        else:
            self.sched.write_text(self.old)

    def schedule(self, last_wake: datetime, slept: datetime | None = None):
        self.sched.write_text(json.dumps({"wakes": {NAME: last_wake.isoformat(timespec="seconds")},
                                          "sleep": (slept or datetime.now()).isoformat(timespec="seconds")}))

    def test_not_free_when_a_wake_comes_due(self):
        now = datetime.now()
        if now.hour == 3 or (now.hour == 4 and now.minute < 5):
            self.skipTest("too close to the nightly sleep")
        self.schedule(now - timedelta(minutes=110))                # next wake in 10 minutes
        free, why = trial.local_free_for(900, self.project)
        self.assertFalse(free)
        self.assertIn("next wake", why)
        self.schedule(now - timedelta(minutes=10))                 # next wake in 110 minutes
        self.assertTrue(trial.local_free_for(900, self.project)[0])

    def test_not_free_while_the_instance_runs(self):
        self.schedule(datetime.now() - timedelta(minutes=10))
        from supervisor.runner import run_lock
        with run_lock(NAME, wait=False, kind="live"):
            self.assertFalse(trial.local_free_for(900, self.project)[0])
        self.assertTrue(trial.local_free_for(900, self.project)[0])   # released: no holder left behind

    def test_an_instance_that_wakes_too_often_goes_to_the_cloud_at_once(self):
        (self.root / "config.env").write_text(f'LLM_BASE_URL="{self.llm_url}"\nLLM_MODEL="b"\nWAKE_INTERVAL_MINUTES=10\n')
        t = {"model": "auto", "llm": {"local": True}, "created": datetime.now().isoformat(), "instance": NAME,
             "by": "instance"}
        self.assertEqual(trial._pair_model(t, {"preempted": 0}, self.project), trial.FALLBACK_MODEL)

    def test_preempted_twice_or_waiting_too_long_goes_to_the_cloud(self):
        self.schedule(datetime.now() - timedelta(minutes=110))   # busy soon: no local slot now
        t = {"model": "auto", "llm": {"local": True}, "created": datetime.now().isoformat(), "instance": NAME,
             "by": "instance"}
        self.assertIsNone(trial._pair_model(t, {"preempted": 0}, self.project))
        self.assertEqual(trial._pair_model(t, {"preempted": 2}, self.project), trial.FALLBACK_MODEL)
        t["queued_at"] = (datetime.now() - timedelta(hours=trial.LOCAL_WAIT_H + 1)).isoformat()
        self.assertEqual(trial._pair_model(t, {"preempted": 0}, self.project), trial.FALLBACK_MODEL)


class FlowTest(Fixture):
    """Judging, verdict, delivery and probation, with the replays and the judge stubbed."""

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, SUP_DIR / "trials" / NAME, True)
        new = b"# proposed\n"
        self.t = trial.create(NAME, {"shaft/extra.py": new}, "an extra module helps", project=self.project, n=2)
        self.t["status"] = "queued"
        trial.save(self.t)

    def arm(self, completed=True):
        return {"completed": completed, "exit": 0, "ended": "ok", "seconds": 1.0, "usd": 0.01, "model": "own",
                "episode": {"intent": {"goal": "g"}, "finish": {"criterion_met": "yes"}, "tools": []}, "trace": [],
                "diff": "", "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "output_tail": ""}

    def test_worse_verdict_reaches_the_inbox_unblinded_and_tightens_probation(self):
        verdicts = iter([{"winner": "first", "reasoning": "Run 1 did the work; Run 2 idled."},
                         {"winner": "second", "reasoning": "Run 2 did the work."}] * 4)
        with mock.patch.object(trial, "replay_arm", lambda *a, **k: self.arm()), \
                mock.patch.object(trial, "judge_pair", lambda *a, **k: {**next(verdicts), "margin": "clear"}):
            for i in range(len(self.t["states"])):
                trial.run_pair(self.project, trial.load(NAME, self.t["id"]), i, "own")
        t = trial.load(NAME, self.t["id"])
        self.assertEqual([s["outcome"] for s in t["states"]], ["loss"] * len(t["states"]))
        t["states"] += [dict(t["states"][0]) for _ in range(3)]    # 5 losses: enough for 'worse'
        trial.conclude(self.project, t)
        t = trial.load(NAME, self.t["id"])
        self.assertEqual(t["verdict"]["label"], "worse")
        msg = next((self.root / "inbox").glob(f"*trial-{t['id']}.md")).read_text()
        self.assertIn("worse", msg)
        self.assertIn("The current run did the work; the proposed run idled.", msg)   # unblinded, capitalised
        self.assertIn("current better", msg)
        # adopting the trialled edit links probation to the verdict, with no margin
        (self.root / "shaft" / "extra.py").write_bytes(b"# proposed\n")
        tried = trial.match(NAME, self.root, ["shaft/extra.py"], {})
        self.assertEqual((tried or {}).get("verdict"), "worse")
        self.assertIsNone(trial.match(NAME, self.root, ["shaft/extra.py", "shaft/other.py"], {}))
        (self.root / "shaft" / "extra.py").write_bytes(b"# proposed!\n")
        self.assertIsNone(trial.match(NAME, self.root, ["shaft/extra.py"], {}))   # another edit: not that trial
        with mock.patch.object(metrics, "judgments", lambda n: []):
            metrics.start_probation(NAME, "abc", ["shaft/extra.py"], trial=tried)
        entry = json.loads(metrics._probation_path(NAME).read_text())[-1]
        self.addCleanup(metrics._probation_path(NAME).unlink, True)
        self.assertEqual(entry["margin"], 0.0)
        self.assertEqual(entry["trial"]["id"], t["id"])
        self.assertIn("Trials of my proposed changes", "\n".join(trial.stats_lines(NAME)))

    def test_the_trialled_edit_is_recognised_on_a_file_that_moved_on(self):
        """wren adopted its trialled sentence on top of a template merge that had landed in prompts.py meanwhile;
        a byte-for-byte match missed it, so its probation never knew the verdict (2026-10-13)."""
        base = trial.committed_code(self.project, NAME)["shaft/prompts.py"]
        cand = base.replace(b"\n", b"\nTRIALLED SENTENCE\n", 1)
        t = trial.create(NAME, {"shaft/prompts.py": cand}, "a sentence helps", project=self.project, n=2)
        t.update(status="done", verdict={"label": "better", "W": 4, "L": 0, "T": 2})
        trial.save(t)
        moved = base + b"\n# a template merge landed here meanwhile\n"
        (self.root / "shaft" / "prompts.py").write_bytes(moved.replace(b"\n", b"\nTRIALLED SENTENCE\n", 1))
        self.assertEqual((trial.match(NAME, self.root, ["shaft/prompts.py"], {"shaft/prompts.py": moved})
                          or {}).get("id"), t["id"])
        (self.root / "shaft" / "prompts.py").write_bytes(moved.replace(b"\n", b"\nANOTHER SENTENCE\n", 1))
        self.assertIsNone(trial.match(NAME, self.root, ["shaft/prompts.py"], {"shaft/prompts.py": moved}))

    def test_both_orders_of_a_pair_have_one_judge_never_the_instances_model(self):
        # 2026-10-14, judge rotation: the order swap checks position bias; a different judge per order would mix in
        # judge variance. And the model the instance thinks with never grades it.
        seen = []
        own = trial._config(self.project, NAME).get("LLM_MODEL")
        pool = ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "claude-sonnet-5-5", own or "x"]
        with mock.patch.object(trial, "replay_arm", lambda *a, **k: self.arm(True)), \
                mock.patch.object(trial, "judge_pair", lambda *a, **k: seen.append(k["model"]) or
                                  {"winner": "first", "margin": "clear", "reasoning": "r"}), \
                mock.patch("supervisor.judge.JUDGE_MODELS", pool):
            for _ in range(6):
                seen.clear()
                trial.run_pair(self.project, trial.load(NAME, self.t["id"]), 0, "own")
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0], seen[1])
                self.assertNotEqual(seen[0], own)
        self.assertEqual(trial.load(NAME, self.t["id"])["states"][0]["judge"], seen[0])

    def test_the_proposed_run_that_crashes_loses_without_a_judge(self):
        arms = iter([self.arm(True), self.arm(False)] * 4)
        judge = mock.Mock(side_effect=AssertionError("no judge needed"))
        with mock.patch.object(trial, "replay_arm", lambda *a, **k: next(arms)), \
                mock.patch.object(trial, "judge_pair", judge), mock.patch.object(trial.random, "Random") as rnd:
            rnd.return_value.shuffle = lambda order: None   # A first, then B
            trial.run_pair(self.project, trial.load(NAME, self.t["id"]), 0, "own")
        self.assertEqual(trial.load(NAME, self.t["id"])["states"][0]["outcome"], "loss")

    def test_operator_trials_are_not_sent_to_the_instance(self):
        t = trial.create(NAME, {}, "A/A calibration", by="operator", allow_empty=True, project=self.project, n=2)
        t["status"] = "queued"
        for s in t["states"]:
            s.update(status="done", outcome="tie")
        trial.conclude(self.project, t)
        self.assertEqual(list((self.root / "inbox").glob(f"*{t['id']}*")), [])
        self.assertEqual(trial.load(NAME, t["id"])["verdict"]["label"], "no clear difference")


class WorkerTest(Fixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, SUP_DIR / "trials" / NAME, True)

    def test_scratch_left_by_a_killed_worker_is_swept_but_not_a_running_one(self):
        old = Path(tempfile.mkdtemp(prefix="shaft-trial-"))
        new = Path(tempfile.mkdtemp(prefix="shaft-trial-"))
        self.addCleanup(shutil.rmtree, new, True)
        os.utime(old, (time.time() - 3 * trial.CYCLE_TIMEOUT_S,) * 2)
        trial.sweep_scratch()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_trials_asked_for_together_get_their_own_ids(self):
        """Four operator trials created in the same second shared one id and overwrote each other (2026-10-13)."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as pool:
            ts = list(pool.map(lambda k: trial.create(NAME, {"shaft/x.py": f"# {k}\n".encode()}, f"variant {k}",
                                                      by="operator", project=self.project, n=2), range(4)))
        self.assertEqual(len({t["id"] for t in ts}), 4)
        for k, t in enumerate(ts):
            self.assertEqual(trial._files(NAME, t["id"], "candidate")["shaft/x.py"], f"# {k}\n".encode())

    def test_two_instances_trials_with_the_same_id_both_get_their_tests(self):
        """wren's and shaft's trials made in the same second shared an id; the worker took wren's tests for done
        once shaft's ran, and wren's trial sat in 'testing' (2026-10-13)."""
        other = self.project / "instances" / "tx"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/tx", str(other))
        shutil.copytree(self.root / "shaft", other / "shaft")
        shutil.copy(self.root / "config.env", other / "config.env")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "tx: live", when="2026-10-10T09:00:00")
        self.addCleanup(shutil.rmtree, SUP_DIR / "trials" / "tx", True)
        with mock.patch.object(trial, "_new_id", lambda name: "t-same"), \
                mock.patch.object(trial, "_choose", lambda *a, **k: [self.state(), self.state()]):
            for name in (NAME, "tx"):
                (SUP_DIR / "trials" / name / "t-same").mkdir(parents=True, exist_ok=True)
                trial.create(name, {"shaft/x.py": b"# x\n"}, "same id", by="operator", project=self.project)
        gated = []
        with mock.patch.object(trial, "gate_check", lambda p, n, t: (gated.append(n), (False, "no"))[1]):
            trial.work(self.project)
        self.assertEqual(sorted(gated), sorted([NAME, "tx"]))
        self.assertEqual({trial.load(n, "t-same")["status"] for n in (NAME, "tx")}, {"rejected"})

    def test_the_worker_runs_trials_side_by_side_but_one_pair_per_trial_at_a_time(self):
        ts = []
        for k in range(3):
            t = trial.create(NAME, {"shaft/x.py": f"# {k}\n".encode()}, f"variant {k}", by="operator",
                             project=self.project, n=2, model="cloud")
            t["status"] = "queued"
            trial.save(t)
            ts.append(t)
        lock, running, peak, per_trial = threading.Lock(), set(), [0], {}

        def fake_pair(project, t, i, model):
            with lock:
                self.assertNotIn(t["id"], {r[0] for r in running}, "two pairs of one trial at once")
                running.add((t["id"], i))
                peak[0] = max(peak[0], len(running))
            time.sleep(0.3)
            with lock:
                running.discard((t["id"], i))
            t = trial.load(t["instance"], t["id"])
            t["states"][i].update(status="done", outcome="tie", model=model)
            trial.save(t)
        with mock.patch.object(trial, "run_pair", fake_pair), mock.patch.object(trial, "PARALLEL", 3):
            self.assertEqual(trial.work(self.project), 0)
        self.assertGreaterEqual(peak[0], 2)
        for t in ts:
            self.assertEqual(trial.load(NAME, t["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
