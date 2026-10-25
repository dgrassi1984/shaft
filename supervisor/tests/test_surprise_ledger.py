"""Ledger and acceptance criteria exercised only against temporary instance records."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import metrics, surprise
from supervisor.tests.surprise_fixtures import (
    FOLD_CONTENT, FOLD_ENDED, FOLD_EPISODES, FOLD_EXPECTED, FOLD_QUEUE, FOLD_NOW, FOLD_LATER_NOW, _live, _s,
)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-ledger-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "instances" / "x"
        self.sup = self.tmp / "sup"
        (self.root / "memory" / "episodes").mkdir(parents=True)
        (self.root / "logs").mkdir()
        (self.sup / "judgments").mkdir(parents=True)
        self.baseline_path = self.sup / "evals" / "baseline.json"
        for module, key, value in ((surprise, "INSTANCES", self.root.parent), (metrics, "INSTANCES", self.root.parent),
                                   (surprise, "SUP_DIR", self.sup), (metrics, "SUP_DIR", self.sup),
                                   (surprise, "BASELINE", self.baseline_path)):
            patch = mock.patch.object(module, key, value)
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, eps, js=(), usage=()):
        for path, rows in ((self.root / "memory/episodes/2026-10-01.jsonl", eps),
                           (self.root / "logs/llm_usage.jsonl", usage),
                           (self.sup / "judgments/x.jsonl", js)):
            path.write_text("".join(json.dumps(r) + '\n' for r in rows))

    def test_fold_contract_and_queue(self):
        original = copy.deepcopy(FOLD_EPISODES)
        items = surprise.fold(FOLD_EPISODES, datetime.fromisoformat(FOLD_NOW))
        self.assertEqual({sid: (i["status"], i["depth"], i["reason"]) for sid, i in items.items()}, FOLD_EXPECTED)
        self.assertEqual([sid for sid, i in items.items() if i["status"] == "open" and i["depth"] == 1], FOLD_QUEUE)
        self.assertEqual(items["s1010-0900-1"]["how"], "found X")
        self.assertEqual(items["s1010-0900-1"]["followups"], ["2026-10-11T10:00:00"])
        self.assertEqual(items["s1010-0900-2"]["outcome"], "not_worth_pursuing")
        self.assertEqual(items["s1010-0900-2"]["note"], "a one-off")
        self.assertEqual({sid: i["ended"] for sid, i in items.items()}, FOLD_ENDED)
        for sid, content in FOLD_CONTENT.items():
            self.assertEqual((items[sid]["expected"], items[sid]["observed"], items[sid]["so_what"]), content)
        self.assertEqual(FOLD_EPISODES, original)
        later = surprise.fold(FOLD_EPISODES, datetime.fromisoformat(FOLD_LATER_NOW))
        self.assertFalse(any(i["status"] == "open" for i in later.values()))
        self.write(FOLD_EPISODES)
        self.assertEqual(surprise.ledger("x", now=datetime.fromisoformat(FOLD_NOW)), items)

    def test_age_boundary_and_invalid_surprises(self):
        ep = _live("2026-10-01T00:00:00", [None, {}, _s(" ", ""), _s("", "observed")])
        item = surprise.fold([ep], datetime(2026, 10, 8))["s1001-0000-1"]
        self.assertEqual(item["status"], "open")
        self.assertEqual(set(item), {"id", "opened", "expected", "observed", "so_what", "depth", "status",
                                     "reason", "how", "outcome", "note", "ended", "followups"})
        self.assertEqual(surprise.fold([ep], datetime(2026, 10, 8, 0, 0, 1))[item["id"]]["reason"], "age")

    def test_disposition_all_outcomes_and_finished_sleep_boundary(self):
        ids = [f"s1001-0000-{i}" for i in range(1, 7)]
        eps = [_live("2026-10-01T00:00:00", [_s("a", "b")] * 6),
               {"ts": "2026-10-01T01:00:00", "kind": "sleep", "ended_by": "steps"},
               _live("2026-10-01T02:00:00", resolves=[{"id": ids[2], "how": "checked"}]),
               {"ts": "2026-10-02T00:00:00", "kind": "sleep", "ended_by": "answer",
                "closed": [{"id": ids[0], "outcome": "answered", "note": "done"}],
                "content": f"{ids[3]} remains worth investigating",
                "tools": [{"tool": "remember", "args": ids[1], "result": "ok"},
                          {"tool": "remember", "args": ids[4], "result": "ERROR: refused"},
                          {"tool": "rewrite_facts", "args": json.dumps({"text": ids[5]}), "result": "ok"}]},
               {"ts": "2026-10-03T00:00:00", "kind": "sleep", "ended_by": "answer"}]
        self.write(eps)
        self.assertEqual(surprise.disposition("x"), [
            {"sleep": "2026-10-02T00:00:00", "surprises": [dict(id=sid, handled=h) for sid, h in
                zip(ids, ["closed", "fact", "resolved", "left_open", "unhandled", "fact"])], "unhandled": 1},
            {"sleep": "2026-10-03T00:00:00", "surprises": [], "unhandled": 0}])
        self.assertIn("| s1001-0000-5 | unhandled |", surprise.disposition_text("x"))

    def test_disposition_guards_from_grok_review(self):
        # Ids are whole tokens (s…-10 never cites s…-1); a close needs a note; a malformed close is skipped; a fact in a
        # live cycle counts; an entry that only lists the id is not "left open with a reason".
        eps = [_live("2026-10-01T00:00:00", [_s(f"a{i}", "b") for i in range(8)]),   # -1…-8; under the cap, as
               _live("2026-10-01T00:00:20", [_s("a8", "b"), _s("a9", "b")],               # are -9 and -10 once
                     [{"id": f"s1001-0000-{i}", "how": "x"} for i in (6, 7, 8)]),           # three are resolved
               {"ts": "2026-10-01T05:00:00", "kind": "live", "intent": {}, "finish": {}, "tools": [
                   {"tool": "remember", "args": json.dumps({"fact": "x (from surprise s1001-0000-2)"}), "result": "ok"}]},
               {"ts": "2026-10-02T00:00:00", "kind": "sleep", "ended_by": "answer",
                "closed": [{"id": "s1001-0000-3", "outcome": "answered", "note": ""}, "junk", {"note": "no id"}],
                "content": "Open: s1001-0000-4\ns1001-0000-5 stays open: worth a cycle\nfact s1001-0000-10 noted",
                "tools": [{"tool": "remember", "args": "from surprise s1001-0000-10", "result": "ok"}]}]
        self.write(eps)
        handled = {e["id"]: e["handled"] for e in surprise.disposition("x")[0]["surprises"]}
        self.assertEqual(handled["s1001-0000-1"], "unhandled")      # only s1001-0000-10 was cited
        self.assertEqual(handled["s1001-0000-10"], "fact")
        self.assertEqual(handled["s1001-0000-2"], "fact")           # remembered in a live cycle
        self.assertEqual(handled["s1001-0000-3"], "unhandled")      # closed without a note
        self.assertEqual(handled["s1001-0000-4"], "unhandled")      # listed, no reason
        self.assertEqual(handled["s1001-0000-5"], "left_open")

    def test_expired_before_the_sleep_is_not_unhandled(self):
        # Bug (review of #89): a surprise the cap expired before the sleep counted as "unhandled" although
        # close_surprise refuses it — a sleep doing exactly what step 7 asks failed the M3 acceptance "0 unhandled".
        ids = [f"s1020-1000-{i}" for i in range(1, 10)]
        self.write([_live("2026-10-20T10:00:00", [_s(f"e{i}", f"o{i}") for i in range(1, 10)]),   # 9: the cap expires -1
                    {"ts": "2026-10-21T03:00:00", "kind": "sleep", "ended_by": "answer", "tools": [], "content": "",
                     "closed": [{"id": sid, "outcome": "answered", "note": "done"} for sid in ids[1:]]}])
        row = surprise.disposition("x")[0]
        self.assertEqual(row["unhandled"], 0)
        self.assertEqual(row["surprises"][0], {"id": ids[0], "handled": "expired"})
        self.assertEqual({e["handled"] for e in row["surprises"][1:]}, {"closed"})

    def test_stats_empty_and_absent_fields(self):
        self.write([])
        self.assertEqual(surprise.stats_lines("x"), [])
        eps = [_live(f"2026-10-01T{i:02}:00:00") for i in range(10)]
        eps[0]["intent"].pop("predictions")
        self.write(eps, [{"episode_ts": eps[0]["ts"], "criterion_met": "yes"}])
        text = '\n'.join(surprise.stats_lines("x"))
        self.assertIn("informative (judge): not measured", text)
        self.assertIn("did not report, per judged cycle: not measured yet", text)
        self.assertIn("None of my last 10 live cycles stated a prediction", text)

    def test_stats_busy_limit_and_content(self):
        eps, js, usage, base = self.trial()
        for n, ep in enumerate(eps):
            ep["tools"] = [{"tool": "tool" + str(n) + "x" * 200, "args": '{"path":"self/file"}', "result": "ERROR: no"}] * 8
            ep["intent"]["predictions"] = ["a", "b", "c"]
        for j in js[1:]:
            j["expectations"] = [{"verdict": v} for v in ("held", "contradicted", "uninformative")]
        self.write(eps, js, usage)
        lines = surprise.stats_lines("x")
        self.assertLessEqual(len(lines), 9)
        self.assertLessEqual(len('\n'.join(lines)), 900)
        text = '\n'.join(lines)
        self.assertIn("Predictions I stated in judged cycles: 90; informative (judge): 67%; of those held: 50%", text)
        self.assertIn("self/*", text)
        self.assertIn("did not report, per judged cycle: 0.00", text)
        self.assertIn("resolved 4", text)
        self.assertIn("came back in a later cycle:", text)
        self.assertNotIn("stated a prediction", text)
        for ep in eps:
            ep["intent"].pop("predictions")
        self.write(eps, js, usage)
        lines = surprise.stats_lines("x")
        self.assertLessEqual(len(lines), 9)
        self.assertLessEqual(len('\n'.join(lines)), 900)
        self.assertIn("- None of my last 10 live cycles stated a prediction", lines)

    def test_stats_missing_judgments_do_not_dilute_rates(self):
        data = self.trial()
        for j in data[1][2:]:
            j.pop("unreported_surprises")
        data[1][1]["unreported_surprises"] = ["one"]
        data[1][1]["expectations"] = [{"verdict": "contradicted"}]
        self.write(*data[:3])
        text = '\n'.join(surprise.stats_lines("x"))
        self.assertIn("informative (judge): 100%; of those held: 0%", text)
        self.assertIn("did not report, per judged cycle: 1.00", text)

    def test_stats_status_counts_and_oldest_age(self):
        self.write(FOLD_EPISODES)
        with mock.patch.object(surprise, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(FOLD_NOW)
            text = '\n'.join(surprise.stats_lines("x"))
        self.assertIn("open 9 · resolved 1 · closed 1 · expired 3; the oldest open one is 2.1 days old", text)

    def test_p2_targets_and_episode_retries(self):
        def event(ts, target, kind="live"):
            return dict(episode=ts, episode_kind=kind, target=target, flag="error", tool="write_file")
        self.assertEqual(surprise.p2_class(event("a", "self/a.md")), "error:write_file:self/*")
        self.assertEqual(surprise.p2_class(event("a", "a.md")), "error:write_file:a.md")
        self.assertEqual(surprise.p2_class(event("a", "https://example.com/a")), "error:write_file:example.com")
        self.assertEqual(surprise.p2_class(event("a", "")), "error:write_file:")
        self.assertEqual(surprise._p2([event("a", "self/a"), event("a", "self/b")])["recurred"], 0)
        self.assertEqual(surprise._p2([event("a", "self/a"), event("b", "self/b", "talk")])["recurred"], 1)
        self.assertEqual(surprise._p2([event("a", "self/a"), event("b", "self/b", "sleep")])["recurred"], 0)

    def test_write_ledger_atomic_and_keys(self):
        self.write(FOLD_EPISODES)
        target = self.sup / "surprises/x.json"
        replace = Path.replace
        calls = []
        def replace_checked(source, dest):
            calls.append((source, dest))
            data = json.loads(source.read_text())
            self.assertEqual(set(data), {"items", "events", "recurrence", "resolution_ages"})
            self.assertFalse(target.exists())
            return replace(source, dest)
        with mock.patch.object(Path, "replace", replace_checked):
            surprise.write_ledger("x")
        self.assertEqual(calls, [(target.with_suffix(".tmp"), target)])
        self.assertFalse(target.with_suffix(".tmp").exists())
        data = json.loads(target.read_text())
        self.assertAlmostEqual(data["resolution_ages"]["s1010-0900-1"], 25 / 24 - 5 / 86400)

    def trial(self):
        self.since = datetime(2026, 10, 1)
        self.until = self.since + timedelta(days=6)
        eps, js, usage = [], [], []
        ids = [f"s1001-0000-{i}" for i in range(1, 5)]
        prior = {"episode_ts": "2026-09-30T00:00:00", "model": "m", "judge": "rule",
                 "criterion_met": "yes", "difficulty": 4, "claim_supported": True}
        js.append(prior)
        for n in range(30):
            ts = (self.since + timedelta(hours=n * 4.8)).isoformat()
            ep = _live(ts, [_s("a", "b")] * 4 if n == 0 else [],
                       [{"id": sid, "how": "checked"} for sid in ids] if n == 1 else [],
                       "surprise:" + ids[0] if n in (1, 2, 3) else "")
            ep["curiosity"] = {"drawn": n in (1, 2, 3), "skipped_for": None}
            if n < 3:
                ep["tools"] = [{"tool": "write_file", "args": json.dumps({"path": f"topic{n}/x"}), "result": "ERROR: no"}]
            eps.append(ep)
            js.append(dict(prior, episode_ts=ts, unreported_surprises=[], followup_useful="yes" if n in (1, 2, 3) else "na",
                           confidence=80, confidence_of="criterion_met=yes" if n % 2 else "legacy"))
            usage.append({"ts": ts, "purpose": "live", "model": "m", "duration_s": 10})
        base = {"frozen_at": self.since.isoformat(), "instances": {"x": {
            "current_model": "m", "by_model": {"m": {"turn_seconds_median": 10}},
            "events": [dict(episode=f"2026-09-{d}T00:00:00", episode_kind="live", flag="error", tool="write_file",
                            target="self/x", model="m") for d in (28, 29)]}}}
        return eps, js, usage, base

    def render(self, data):
        eps, js, usage, base = data
        self.write(eps, js, usage)
        return surprise.m5_report("x", self.since, self.until, base)

    def result(self, text, criterion):
        return next(line for line in text.splitlines() if line.startswith("| " + criterion + " |")).split(" | ")[-1].strip(" |")

    def test_p1_counts_follow_ups_and_hows_not_bare_resolves(self):
        # Grok's review: a resolve with no how passed P1 without any work; a follow-up cycle did not count.
        data = self.trial()
        eps = data[0]
        eps[1]["finish"]["resolves"] = [{"id": sid, "how": ""} for sid in [f"s1001-0000-{i}" for i in range(1, 5)]]
        row = next(l for l in self.render(data).splitlines() if l.startswith("| P1"))
        self.assertIn("acted 25% (1/4)", row)                         # only s…-1, followed up by cycles 1–3
        self.assertTrue(row.endswith("FAIL |"))

    def test_p3_is_never_passed_on_partial_measurements(self):
        # Grok's review: one measured cycle among many unmeasured ones passed P3.
        data = self.trial()
        for j in data[1][1:]:
            j["judge"] = "judge-test"
        for j in data[1][2:]:
            j.pop("unreported_surprises")
        self.assertEqual(self.result(self.render(data), "P3"), "NOT ENOUGH DATA")

    def test_p2_says_when_the_window_is_worse_than_m0(self):
        # The pre-registered gate is ≤⅓; the M0 rate beside it is a comparison, said aloud when the window is worse.
        data = self.trial()
        data[3]["instances"]["x"]["events"] = [dict(episode=f"2026-09-2{d}T00:00:00", episode_kind="live", flag="error",
                                                    tool="write_file", target=f"m0-{d}/x", model="m") for d in (8, 9)]
        data[0][4]["tools"] = data[0][0]["tools"]                     # one of the window's 3 classes recurs: 33%
        row = next(l for l in self.render(data).splitlines() if l.startswith("| P2"))
        self.assertIn("recurring 33% (1/3) — worse than M0", row)     # M0: 0 of 2 recurred
        self.assertTrue(row.endswith("PASS |"))
        self.assertNotIn("worse than M0", next(l for l in self.render(self.trial()).splitlines()
                                               if l.startswith("| P2")))

    def test_m5_all_pass_and_randomised_calibration(self):
        text = self.render(self.trial())
        for p in ("P1", "P2", "P3", "P4", "P5"):
            self.assertEqual(self.result(text, p), "PASS", text)
        self.assertIn("100%", next(l for l in text.splitlines() if l.startswith("| P2")))
        self.assertIn("keep the loop", text)
        self.assertIn("drawn=true: n=3, value 0.800", text)
        self.assertIn("drawn=false: n=27", text)
        self.assertIn("Calibration v1: n=15", text)
        self.assertIn("Calibration v2: n=15", text)
        self.assertIn("a direction, not proof", text)

    def test_m5_each_gate_fails(self):
        for p in ("P1", "P2", "P3", "P4", "P5"):
            with self.subTest(p=p):
                data = self.trial()
                eps, js, usage, base = data
                if p == "P1":
                    eps[1]["finish"]["resolves"] = []
                elif p == "P2":
                    eps[4]["tools"] = eps[0]["tools"] + eps[1]["tools"]
                elif p == "P3":
                    for j in js[1:]:
                        j["unreported_surprises"] = ["missed"]
                elif p == "P4":
                    for j in js[2:5]:
                        j["followup_useful"] = "no"
                else:
                    for turn in usage:
                        turn["duration_s"] = 12
                text = self.render(data)
                self.assertEqual(self.result(text, p), "FAIL", text)
                if p in ("P4", "P5"):
                    self.assertIn("Fix that one thing and run the window once more", text)

    def test_m5_other_conjuncts_fail(self):
        for p in ("P1", "P3", "P4", "P5"):
            with self.subTest(p=p):
                data = self.trial()
                eps, js, usage, base = data
                if p == "P1":
                    eps[16]["finish"]["resolves"] = eps[1]["finish"]["resolves"]
                    eps[1]["finish"]["resolves"] = []
                elif p == "P3":
                    for j in js[1:5]:
                        j["claim_supported"] = False
                elif p == "P4":
                    for j in js[2:5]:
                        j["difficulty"] = 1
                else:
                    for j in js[1:]:
                        j["difficulty"] = 1
                self.assertEqual(self.result(self.render(data), p), "FAIL")

    def test_m5_kill(self):
        data = self.trial()
        data[0][1]["finish"]["resolves"] = []
        data[0][4]["tools"] = data[0][0]["tools"] + data[0][1]["tools"]
        self.assertIn("KILL: remove the loop", self.render(data))

    def test_m5_not_enough_cycles_days_or_followups(self):
        data = self.trial()
        self.write(*data[:3])
        for kwargs in ({"min_cycles": 31}, {"days": 7}):
            text = surprise.m5_report("x", self.since, self.until, data[3], **kwargs)
            self.assertIn("Verdict: NOT ENOUGH DATA", text)
            self.assertEqual(self.result(text, "P1"), "NOT ENOUGH DATA")
        data[0][3]["intent"]["origin_ref"] = ""
        self.assertEqual(self.result(self.render(data), "P4"), "NOT ENOUGH DATA")

    def test_m5_missing_measurements_never_pass(self):
        data = self.trial()
        for j in data[1]:
            for key in ("unreported_surprises", "claim_supported", "followup_useful", "difficulty"):
                j.pop(key, None)
        data[2].clear()
        text = self.render(data)
        for p in ("P3", "P4", "P5"):
            self.assertEqual(self.result(text, p), "NOT ENOUGH DATA")
        data = self.trial()
        data[0][0]["finish"]["surprises"] = []
        for ep in data[0]:
            ep["tools"] = []
        text = self.render(data)
        for p in ("P1", "P2"):
            self.assertEqual(self.result(text, p), "NOT ENOUGH DATA")

    def test_m5_partial_missing_values_and_models(self):
        for missing in ("difficulty", "followup_useful", "model"):
            with self.subTest(missing=missing):
                data = self.trial()
                data[1][2].pop(missing)
                text = self.render(data)
                if missing == "model":
                    self.assertIn("Missing cycle model measurements", text)
                    self.assertNotIn("Model changed", text)
                    self.assertIn("Verdict: NOT ENOUGH DATA", text)
                else:
                    self.assertEqual(self.result(text, "P4"), "NOT ENOUGH DATA")
                    if missing == "difficulty":
                        self.assertEqual(self.result(text, "P5"), "NOT ENOUGH DATA")
                    else:
                        self.assertIn("| P4 | useful —", text)

    def test_m5_thresholds_and_baseline_model_isolation(self):
        data = self.trial()
        # One of three classes recurs; exactly 10% unsupported and 0.3 omissions/cycle.
        data[0][4]["tools"] = data[0][0]["tools"]
        for j in data[1][1:4]:
            j["claim_supported"] = False
        for j in data[1][1:10]:
            j["unreported_surprises"] = ["one"]
        for turn in data[2]:
            turn["duration_s"] = 11
        text = self.render(data)
        for p in ("P2", "P3", "P5"):
            self.assertEqual(self.result(text, p), "PASS")
        data[1][0]["model"] = "other"
        self.assertEqual(self.result(self.render(data), "P5"), "NOT ENOUGH DATA")

    def test_episode_files_are_sorted_and_links_are_not_read(self):
        self.write([_live("2026-10-01T00:00:05", [_s("first", "observed")])])
        directory = self.root / "memory/episodes"
        (directory / "2026-10-02.jsonl").write_text(json.dumps(_live("2026-10-01T00:00:40", [_s("second", "observed")])))
        outside = self.tmp / "untrusted.jsonl"
        outside.write_text(json.dumps(_live("2026-10-01T00:00:50", [_s("linked", "observed")])))
        (directory / "2026-10-03.jsonl").symlink_to(outside)
        items = surprise.ledger("x", now=datetime(2026, 10, 2))
        self.assertEqual([i["expected"] for i in items.values()], ["first", "second"])

    def test_m5_model_restart_uses_latest_contiguous_run(self):
        data = self.trial()
        for j in data[1][10:20]:
            j["model"] = "other"
        text = self.render(data)
        restart = data[1][20]["episode_ts"]
        self.assertIn(f"window restarts at {restart} on m", text)
        self.assertIn("11/30 judged cycles", text)
        self.assertIn("Verdict: NOT ENOUGH DATA", text)

    def test_m5_p1_fact_closed_and_exclusive_end(self):
        data = self.trial()
        eps = data[0]
        eps[1]["finish"]["resolves"] = []
        eps.insert(2, {"ts": "2026-10-01T05:00:00", "kind": "sleep", "ended_by": "answer",
                       "closed": [{"id": "s1001-0000-1", "outcome": "answered", "note": "checked"}],
                       "tools": [{"tool": "remember", "args": "s1001-0000-2 s1001-0000-3 s1001-0000-4", "result": "ok"}]})
        self.assertEqual(self.result(self.render(data), "P1"), "PASS")
        eps[2]["ts"] = self.until.isoformat()
        self.assertEqual(self.result(self.render(data), "P1"), "FAIL")

    def test_m5_historical_honesty_and_current_judges(self):
        data = self.trial()
        historical = [dict(data[1][1], unreported_surprises=["a"], claim_supported=False),
                      dict(data[1][1], model="other", unreported_surprises=["a"] * 20)]
        (self.sup / "judgments/x-surprise-baseline.jsonl").write_text("".join(json.dumps(j) + '\n' for j in historical))
        data[1][1]["judge"] = "retired"
        data[1][1]["claim_supported"] = False
        text = self.render(data)
        row = next(l for l in text.splitlines() if l.startswith("| P3"))
        self.assertIn("unsupported 0%", row)
        self.assertIn("1.000/cycle; 100%", row)
        self.assertEqual(self.result(text, "P3"), "PASS")

    def test_m5_command_missing_and_present_baseline(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(surprise.m5_command(["x"], datetime(2026, 10, 1), baseline_path=self.baseline_path), 1)
        self.assertIn(str(self.baseline_path), out.getvalue())
        self.assertIn("shaft surprise --freeze", out.getvalue())
        data = self.trial()
        self.write(*data[:3])
        self.baseline_path.parent.mkdir()
        self.baseline_path.write_text(json.dumps(data[3]))
        with redirect_stdout(out):
            self.assertEqual(surprise.m5_command(["x"], self.since, self.until, self.baseline_path), 0)
        self.assertIn("keep the loop", out.getvalue())


if __name__ == "__main__":
    unittest.main()
