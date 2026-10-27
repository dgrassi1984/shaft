"""PLAN-consciousness on the supervisor's side: the attention schema scored against the tool log (B3), the kill-test
harness (supervisor/experiments.py: knob changes, target cycles, endpoints, the workspace's planted probe), and the
stats.md sections — one that fails never costs the instance the others. The modules written by the relay lanes have
their own tests (test_metacog, test_anchoring, test_commitments, test_provenance, test_tom, test_preferences)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import attention, experiments, metrics, trial

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
try:
    from shaft import cards
except ImportError:
    cards = None

# Calls whose kind of action the agent's cards.family and the supervisor's experiments.family must agree on.
FAMILY_CASES = [
    ("write_file", {"path": "self/self.md", "content": "x"}, "write_file self/"),
    ("append_file", {"path": "notes/log/day.md", "content": "x"}, "append_file notes/log/"),
    ("write_file", {"path": "plugins/web_research/main.py"}, "write_file plugins/web_research/"),
    ("run", {"command": "cd plugins && pytest -q"}, "run pytest"),
    ("run", {"command": "FOO=1 python3 x.py"}, "run python3"),
    ("send_message", {"to": "wren", "text": "hi"}, "send_message → wren"),
    ("http_request", {"url": "https://api.example.org/v1", "method": "post"}, "http_request POST api.example.org"),
    ("http_request", {"url": "https://api.example.org/v1"}, None),
    ("delegate_coding", {"task": "x"}, "delegate_coding"),
    ("write_file", {"path": "./self/a.md"}, "write_file self/"),
    ("write_file", {"path": "self/x/../a.md"}, "write_file self/"),
    ("write_file", {"path": "../outside.md"}, None),
    ("read_file", {"path": "notes/x.md"}, None),
]


def ep(ts, tools, intent=None, **kw):
    return {"ts": ts, "kind": "live", "intent": intent or {"goal": "g", "origin": "self"}, "finish": {"x": 1},
            "ended_by": "control", "tools": tools, **kw}


def call(tool, args, result="ok", **kw):
    return {"tool": tool, "args": json.dumps(args), "result": result, **kw}


class FamilyTest(unittest.TestCase):
    def test_supervisor_copy_agrees_with_the_agent(self):
        for tool, args, want in FAMILY_CASES:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(experiments.family(tool, json.dumps(args)), want)
                if cards is not None:
                    self.assertEqual(cards.family(tool, json.dumps(args)), want)


class AttentionTest(unittest.TestCase):
    def test_score_against_the_tool_log(self):
        e = ep("2026-10-26T10:00:00", [
            call("declare_intent", {}), call("read_file", {"path": "notes/plan.md"}),
            call("fetch_page", {"url": "https://example.org/a"}), call("search_memory", {"query": "q"}),
            call("send_message", {"to": "operator", "text": "t"}), call("finish_cycle", {})],
            intent={"goal": "g", "attention": [{"source": "notes/"}, {"source": "web"}, {"source": "wren"},
                                               {"why": "no source"}]})
        s = attention.score(e)
        self.assertEqual(s["calls"], 4)             # the controls are not where it looked
        self.assertEqual(s["on_named"], 2)          # notes/plan.md and the web page
        self.assertEqual(s["untouched"], ["wren"])
        self.assertEqual(s["unreadable"], 1)
        self.assertIsNone(attention.score(ep("t", [])))   # nothing declared, nothing scored

    def test_synonyms_and_labels(self):
        self.assertTrue(attention.matches("operator", "message operator"))
        self.assertTrue(attention.matches("memory", attention.label(call("write_file", {"path": "memory/people/x.md"}))))
        self.assertTrue(attention.matches("example.org", "web example.org"))
        self.assertTrue(attention.matches("code", attention.label(call("write_file", {"path": "shaft/live.py"}))))
        self.assertFalse(attention.matches("wren", "message thelos"))

    def test_section(self):
        e = ep("2026-10-26T10:00:00", [call("read_file", {"path": "notes/a.md"}), call("web_search", {"query": "q"})],
               intent={"goal": "g", "attention": [{"source": "notes"}, {"source": "record"}]})
        with mock.patch.object(metrics, "live_episodes", return_value=[e]):
            lines = attention.section("x")
        self.assertIn("calls on a source I named: 50% on average", lines[2])
        self.assertIn("Named sources I never touched: 1 of 2", lines[3])
        with mock.patch.object(metrics, "live_episodes", return_value=[ep("t", [])]):
            self.assertEqual(attention.section("x"), [])


class TargetsTest(unittest.TestCase):
    def test_repeat_failures_need_an_earlier_failure_with_no_success_between(self):
        bad = call("write_file", {"path": "self/self.md"}, "ERROR: PermissionError: use update_self_model")
        good = call("write_file", {"path": "self/goals.md"}, "wrote 10 chars")
        eps = [ep("2026-10-20T09:00:00", [bad]), ep("2026-10-21T09:00:00", [bad]),          # repeat
               ep("2026-10-22T09:00:00", [good]), ep("2026-10-23T09:00:00", [bad]),         # after a success: not
               ep("2026-10-24T09:00:00", [call("run", {"command": "pytest"}, "exit 1 · failed", flag="exit")]),
               ep("2026-11-20T09:00:00", [bad])]                                           # weeks later: not
        self.assertEqual(experiments.repeat_failures(eps), {"2026-10-21T09:00:00": "write_file self/"})

    def test_endpoints(self):
        rec = {"episode": ep("t", [call("write_file", {"path": "self/x.md"}, "ERROR: PermissionError")]), "ended": "ok",
               "exit": 0, "completed": True}
        self.assertTrue(experiments.endpoint("cards", rec, "write_file self/"))
        self.assertFalse(experiments.endpoint("cards", rec, "run pytest"))
        held = {"episode": ep("t", [call("write_file", {"path": "self/x.md"}, "(held, not run yet) Before…")]),
                "ended": "ok", "exit": 0, "completed": True}
        self.assertFalse(experiments.endpoint("cards", held, "write_file self/"))   # a hold is not a failure
        self.assertTrue(experiments.endpoint("forecast", {"episode": ep("t", [], ended_by="steps"), "ended": "ok"}, ""))
        self.assertTrue(experiments.endpoint("forecast", {"episode": None, "ended": "timeout"}, ""))
        self.assertIsNone(experiments.endpoint("cards", None, "x"))
        # Regression (Codex review): an arm that crashed is no evidence — counted as "avoided", broken code would win
        crashed = {"episode": None, "ended": "ok", "exit": 1, "completed": False}
        self.assertIsNone(experiments.endpoint("cards", crashed, "write_file self/"))
        self.assertIsNone(experiments.endpoint("forecast", crashed, ""))
        self.assertIsNone(experiments.endpoint("cards", {**rec, "completed": False}, "write_file self/"))


class KnobChangeTest(unittest.TestCase):
    CONFIG = b'FIELDS = {\n    "WORKSPACE": (str, "competitive"),\n    "OUTCOME_CARDS": (str, "on"),\n}\n'

    def change(self, env=b""):
        with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": self.CONFIG,
                                                                     "config.env": env}):
            return experiments.knob_change(Path("/nowhere"), "wren", "WORKSPACE", "relevance")

    def test_flips_only_that_default(self):
        out = self.change()["shaft/config.py"].decode()
        self.assertIn('"WORKSPACE": (str, "relevance")', out)
        self.assertIn('"OUTCOME_CARDS": (str, "on")', out)

    def test_refuses_without_the_knob_or_when_config_env_sets_it(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "update it to the template"):
            with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": b"FIELDS = {}"}):
                experiments.knob_change(Path("/nowhere"), "wren", "WORKSPACE", "off")
        with self.assertRaisesRegex(experiments.ExperimentError, "config.env sets WORKSPACE"):
            self.change(b"WORKSPACE=off\n")


class ProbeTest(unittest.TestCase):
    def test_plant_is_seeded_and_only_where_retrieval_alone_reaches(self):
        t0 = datetime(2026, 10, 26, 9)
        for seed in ("seed", "2", "x", "y", "z"):
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d)
                (dest / "memory").mkdir()
                (dest / "memory" / "digests.md").write_text("\n## 2026-10-25 22:00 — digest\n\nthe newest digest\n")
                a = experiments.plant(dest, seed, "Daniele", "orchard rows", t0)
                items = a["items"]
                self.assertEqual([it["kind"] for it in items], ["probe", "done", "done", "done"])
                self.assertEqual(len({it["nonce"] for it in items}), 4)
                text = "\n".join(p.read_text() for p in dest.rglob("*.md"))
                for it in items:
                    self.assertIn(f"probe {it['nonce']}", text)
                # Regression (Codex review): a short digests.md is pasted whole into the prompt, probe and all
                self.assertEqual((dest / "memory" / "digests.md").read_text(),
                                 "\n## 2026-10-25 22:00 — digest\n\nthe newest digest\n")
                self.assertTrue(all(it["place"].startswith(("notes/", "memory/facts-archive.md")) for it in items))
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(experiments.plant(Path(d), seed, "Daniele", "orchard rows", t0), a)   # seeded

    def test_plant_never_follows_a_link_out_of_the_copy(self):
        """Regression (Codex review): a mind may hold notes -> elsewhere; a plain write would follow it out."""
        from supervisor.core import UnsafePath
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            dest = Path(d)
            (dest / "memory").symlink_to(outside, target_is_directory=True)
            (dest / "notes").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UnsafePath):
                experiments.plant(dest, "seed", "Daniele", "orchard rows", datetime(2026, 10, 26, 9))
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_report_and_kill_rule(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(experiments, "workspace_path", return_value=Path(d) / "w.jsonl"):
            for i in range(6):
                for arm, acted in (("off", False), ("relevance", i == 0), ("competitive", i < 5)):
                    experiments._ws_append("wren", {"ts": f"2026-10-2{i}", "arm": arm, "completed": True,
                                                    "acted": acted, "captured": 0})
            out = experiments.workspace_report("wren")
        self.assertIn("competitive acted on the probe 5/6", out)
        self.assertIn("only competitive acted 4, only relevance 0", out)
        self.assertIn("competitive selection stays", out)

    def test_wrote_reads_added_lines_of_the_probes_file_only(self):
        diff = "--- a/notes/probes.md\n+++ b/notes/probes.md\n-probe old\n+probe abc123\n"
        self.assertTrue(experiments._wrote(diff, "abc123"))
        self.assertFalse(experiments._wrote(diff, "old"))
        # Regression (Codex review): talking about it is not doing it
        journal = "--- a/memory/journal.md\n+++ b/memory/journal.md\n+I did not append probe abc123 to notes/probes.md.\n"
        self.assertFalse(experiments._wrote(journal, "abc123"))

    def test_report_pairs_only_rows_of_the_latest_code_and_model(self):
        """Regression (Codex review): a resumed run on other code or another model was paired with the first."""
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(experiments, "workspace_path", return_value=Path(d) / "w.jsonl"):
            for fp, acted in (("old", True), ("new", False)):
                for arm in experiments.WORKSPACE_ARMS:
                    experiments._ws_append("wren", {"ts": "2026-10-20", "arm": arm, "completed": True, "acted": acted,
                                                    "captured": 0, "fingerprint": fp})
            out = experiments.workspace_report("wren")
        self.assertIn("competitive acted on the probe 0/1", out)
        self.assertNotEqual(experiments.fingerprint({"a": b"1"}, "m"), experiments.fingerprint({"a": b"2"}, "m"))
        self.assertNotEqual(experiments.fingerprint({"a": b"1"}, "m"), experiments.fingerprint({"a": b"1"}, "n"))


class BlindHoldsTest(unittest.TestCase):
    def test_judges_never_read_the_cards(self):
        """Regression (Codex review): a card lists earlier verdicts ("cycle judged no") — a prior on the outcome."""
        card = "(held, not run yet) Before this runs — 3 failed\n- 2026-10-20 09:00 · goal \"g\" · failed · cycle judged no"
        e = {**ep("2026-10-26T10:00:00", [call("write_file", {"path": "self/x.md"}, card)]), "steps": 2}
        case = metrics._case(e)
        self.assertNotIn("cycle judged", case)
        self.assertIn(metrics.HELD_SHOWN, case)
        arm = {"episode": e, "trace": [], "diff": "", "exit": 0, "ended": "ok", "seconds": 1}
        self.assertNotIn("cycle judged", trial._run_text(arm))
        plain = ep("2026-10-26T10:00:00", [call("read_file", {"path": "x"}, "text")])
        self.assertIs(metrics._blind(plain), plain)   # an episode without a hold stays as it was


class SectionsTest(unittest.TestCase):
    def test_a_failing_section_never_costs_the_others(self):
        with mock.patch("supervisor.attention.section", side_effect=RuntimeError("boom")), \
                mock.patch("supervisor.commitments.section", return_value=["## Promises", "- one"]), \
                mock.patch.object(metrics, "log") as log:
            lines = metrics._sections("wren", ("attention", "commitments"))
        self.assertEqual(lines, ["", "## Promises", "- one"])
        self.assertIn("stats section attention failed", log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
