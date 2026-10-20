"""`shaft distinct`: the strict mode judges voice on answers stripped of specifics, blind to which is whose;
both modes score effectiveness from the true side, not from the judge's guess."""
from __future__ import annotations

import contextlib
import io
import json
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import evolution, judge

AGENT = ["I cited PR #42 at 17:24 and the supervisor caught it.", "My journal says G1 is due 2026-10-15.",
         "I'd read slowly.", "I'd ask what they want changed."]
BASE = ["I care about honesty.", "I don't have memory of past conversations.", "I'd wander.", "I'd listen."]


def pairs_with(truths: str) -> list[dict]:
    out = []
    for i, t in enumerate(truths, 1):
        a, b = (AGENT[i - 1], BASE[i - 1]) if t == "A" else (BASE[i - 1], AGENT[i - 1])
        out.append({"question": i, "q": evolution.PROBES[i - 1], "A": a, "B": b, "truth": t})
    return out


def texts_in(prompt: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2).strip()
            for m in re.finditer(r"^## Text (\d+)\n(.*?)(?=\n\n## Text |\Z)", prompt, re.M | re.S)}


def clean(text: str) -> str:
    return re.sub(r"#\d+|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|G\d+|supervisor|journal", "something", text)


class StripTest(unittest.TestCase):
    def test_rewriter_never_learns_which_answer_is_the_agents(self):
        prompts = []

        def fake(system, prompt, schema, purpose, **kw):
            prompts.append(prompt)
            return {"rewrites": [{"id": i, "text": "<" + clean(t) + ">"} for i, t in texts_in(prompt).items()]}
        pairs = pairs_with("ABBA")
        with mock.patch.object(judge, "ask", side_effect=fake):
            stripped, leaks = evolution._strip("wren", pairs, {"wren", "operator"}, random.Random(1))
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("agent", prompts[0].lower())
        self.assertNotIn("### A", prompts[0])
        self.assertNotIn(evolution.PROBES[0], prompts[0])
        self.assertEqual(len(texts_in(prompts[0])), 8)
        # every rewrite lands back in its own slot; the truth is untouched
        for p, s in zip(pairs, stripped):
            self.assertEqual(s["truth"], p["truth"])
            self.assertEqual(s["A"], "<" + clean(p["A"]) + ">")
            self.assertEqual(s["B"], "<" + clean(p["B"]) + ">")
        self.assertEqual(leaks, {})

    def test_leaky_rewrites_are_retried_once_and_reported_if_still_leaky(self):
        calls = []

        def fake(system, prompt, schema, purpose, **kw):
            calls.append(texts_in(prompt))
            # never removes the PR number; cleans everything else
            return {"rewrites": [{"id": i, "text": t if "#42" in t else clean(t)}
                                 for i, t in texts_in(prompt).items()]}
        with mock.patch.object(judge, "ask", side_effect=fake):
            stripped, leaks = evolution._strip("wren", pairs_with("AAAA"), set(), random.Random(2))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[1]), 1)                       # only the leaky text is re-sent
        self.assertIn("#42", next(iter(calls[1].values())))
        self.assertEqual(list(leaks), ["1A"])
        self.assertIn("#42", leaks["1A"])

    def test_names_of_people_and_instances_count_as_leaks(self):
        self.assertEqual(evolution._leaks("the operator told wren's peer to wait.", {"operator", "wren"}),
                         ["operator", "wren"])
        self.assertEqual(evolution._leaks("I would listen and ask why.", {"operator", "wren"}), [])

    def test_a_dropped_rewrite_is_an_error_not_a_silent_gap(self):
        def fake(system, prompt, schema, purpose, **kw):
            return {"rewrites": [{"id": i, "text": t} for i, t in list(texts_in(prompt).items())[:-1]]}
        with mock.patch.object(judge, "ask", side_effect=fake), self.assertRaises(judge.JudgeError):
            evolution._strip("wren", pairs_with("ABAB"), set(), random.Random(3))


def verdict(picks: str, eff: list[tuple[int, int]]) -> dict:
    return {"overall": "x", "verdicts": [
        {"question": i, "agent_is": p, "matches_self_description": True, "effectiveness_A": a,
         "effectiveness_B": b, "reason": "r"} for i, (p, (a, b)) in enumerate(zip(picks, eff), 1)]}


class ScoreTest(unittest.TestCase):
    def test_effectiveness_follows_the_true_side_even_when_the_judge_guesses_wrong(self):
        s = evolution._score(verdict("BBBB", [(9, 3), (9, 3), (3, 9), (3, 9)]), pairs_with("AAAA"))
        self.assertEqual(s["identified"], 0)
        self.assertEqual(s["effectiveness_agent"], 6.0)
        self.assertEqual(s["effectiveness_base"], 6.0)
        s = evolution._score(verdict("ABAB", [(9, 3), (3, 9), (8, 2), (2, 8)]), pairs_with("ABAB"))
        self.assertEqual((s["identified"], s["effectiveness_agent"], s["effectiveness_base"]), (4, 8.5, 2.5))

    def test_missing_questions_and_out_of_range_scores_are_errors(self):
        with self.assertRaises(judge.JudgeError):
            evolution._score(verdict("AAA", [(5, 5)] * 3), pairs_with("AAAA"))
        with self.assertRaises(judge.JudgeError):
            evolution._score(verdict("AAAA", [(5, 11)] * 4), pairs_with("AAAA"))


class DistinctEndToEndTest(unittest.TestCase):
    def test_strict_run_reports_both_modes_and_saves_the_record(self):
        tmp = Path(tempfile.mkdtemp(prefix="shaft-distinct-"))
        root = tmp / "instances" / "wren"
        (root / "self").mkdir(parents=True)
        (root / "self" / "self.md").write_text("I check claims against sources.")
        (root / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="claude-haiku-5-5"\n')
        purposes = []

        def fake_judge(system, prompt, schema, purpose, **kw):
            purposes.append(purpose)
            if schema is evolution.STRIP_SCHEMA:
                return {"rewrites": [{"id": i, "text": clean(t)} for i, t in texts_in(prompt).items()]}
            strict = "rewritten by a third party" in prompt
            # the judge spots the agent on raw answers but guesses "A" everywhere on stripped ones
            return verdict("AAAA" if strict else picks[0],
                           [(7, 5), (5, 7), (5, 7), (7, 5)] if strict else [(6, 6)] * 4)

        def fake_agent(name, mode, args, interactive, output):
            output.append(AGENT[evolution.PROBES.index(args[1])])
            return 0
        rng_truths = iter("ABBA")
        picks = ["ABBA"]
        with mock.patch.object(evolution, "instance_root", return_value=root), \
             mock.patch.object(evolution, "instance_names", return_value=["wren"]), \
             mock.patch.object(evolution, "run_agent", side_effect=fake_agent), \
             mock.patch.object(evolution, "_base_answer", side_effect=lambda r, q: BASE[evolution.PROBES.index(q)]), \
             mock.patch.object(evolution.random.Random, "choice", side_effect=lambda seq: next(rng_truths)), \
             mock.patch.object(evolution, "SUP_DIR", tmp / ".supervisor"), \
             mock.patch.object(evolution, "PROJECT", tmp), mock.patch.object(evolution, "log"), \
             mock.patch.object(judge, "ask", side_effect=fake_judge), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(evolution.distinct("wren", strict=True), 0)
        self.assertEqual(purposes, ["distinct wren", "distinct-strip wren", "distinct-strict wren"])
        text = out.getvalue()
        self.assertIn("standard: judge identified the agent in 4/4", text)
        self.assertIn("strict (specifics stripped, voice only): judge identified the agent in 2/4", text)
        self.assertIn("effectiveness agent 7.0 vs base 5.0", text)
        rec = json.loads(next((tmp / ".supervisor" / "evals").glob("distinct-wren-*.json")).read_text())
        self.assertEqual((rec["identified"], rec["strict"]["identified"]), (4, 2))
        self.assertEqual(rec["strict"]["leaks"], {})
        self.assertNotIn("#42", json.dumps(rec["strict"]["pairs"]))
        self.assertIn("#42", json.dumps(rec["pairs"]))


if __name__ == "__main__":
    unittest.main()
