"""PLAN-consciousness in the template, wired together: the workspace (B2), the attention schema (B3), outcome cards
held in a live cycle (B1), commitments in cycles, conversations and sleep (C2), the forecast at declare (D1), and every
knob off. Black-box through FakeLLM where the loop is involved; the modules' own tests are test_cards, test_commitments,
test_forecast and test_provenance."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import config, live, sleep
try:
    from shaft import cards, commitments, workspace
except ImportError:
    raise unittest.SkipTest("template/ predates PLAN-consciousness: merge claude/consciousness-template into template")

from shaft.llm import Client
from shaft.state import State
from shaft.tools import Toolbox
from supervisor.tests.fakellm import FakeLLM

NOW = "2026-10-26T10:00:00"
DECLARE = {"goal": "Fix the orchard survey notes", "success_criterion": "notes/orchard.md lists every row",
           "confidence": 60, "origin": "self", "predictions": ["the file has 12 rows"]}
FINISH = {"what_happened": "wrote the notes", "criterion_met": "yes", "learned": "rows are in the survey"}


class Fixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-consciousness-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("self", "memory", "inbox", "notes"):
            (self.root / folder).mkdir()
        self.state = State(self.root)
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "fake",
                                                 "NAME": "wren", "MAX_STEPS_LIVE": "8", "CURIOSITY_EVERY_CYCLES": "0"})
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root), "SHAFT_NOW": NOW}, clear=True))
        self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def model(self, replies):
        fake = FakeLLM(replies)
        self.addCleanup(fake.close)
        return fake, Client(fake.url, "m", "", 30, self.root / "usage.jsonl")

    def live(self, replies):
        fake, client = self.model(replies)
        live.run(self.cfg, self.state, client)
        return fake

    def episode(self, ts, goal, tools, ended_by="control", finish=True):
        self.state.append_episode({"ts": ts, "kind": "live", "ended_by": ended_by, "steps": 4,
                                   "intent": {"goal": goal, "origin": "self", "at": ts[:11] + "08:00:00"},
                                   "finish": dict(FINISH) if finish else None, "tools": tools})

    def last(self):
        return self.state.last_episode()


def failing_write(path="self/stats.md"):
    return {"tool": "write_file", "args": json.dumps({"path": path, "content": "x"}),
            "result": f"ERROR: PermissionError: {path}: written by the supervisor from my measured results"}


class WorkspaceUnitTest(Fixture, unittest.TestCase):
    def build(self, mode="competitive", inbox=(), plan="", goals="", k=3):
        return workspace.build(self.root, mode, about="orchard survey rows", shown="", now=datetime(2026, 10, 26, 10),
                               inbox=list(inbox), plan=plan, goals=goals, k=k)

    def test_competition_caps_each_source_and_fits_the_budget(self):
        inbox = [(f"20261026-0{i}0000-000000-from-operator.md", f"From: operator\n\nplease check orchard row {i}")
                 for i in range(4)]
        ws = self.build(inbox=inbox, plan="From my sleep of x:\n\n- survey the orchard rows again with care")
        self.assertEqual(len(ws.winners), 3)
        self.assertLessEqual(sum(1 for w in ws.winners if w.source == "inbox"), workspace.PER_SOURCE)
        self.assertTrue(all(len(w.text) <= workspace.BUDGET // 3 for w in ws.winners))
        rec = ws.record()
        self.assertEqual(rec["mode"], "competitive")
        self.assertEqual(len(rec["candidates"]), len(ws.pool))
        self.assertIn("## In focus now", ws.block())
        self.assertTrue(ws.broadcast().startswith("[runtime] In focus: "))

    def test_relevance_ranks_by_relevance_alone(self):
        inbox = [(f"20261026-0{i}0000-000000-from-operator.md", f"From: operator\n\northogonal note {i}")
                 for i in range(4)]
        ws = self.build("relevance", inbox=inbox, plan="From my sleep of x:\n\n- survey the orchard rows again")
        self.assertEqual(ws.winners[0].source, "plan")   # the only item sharing words with the cycle
        self.assertEqual(ws.winners, sorted(ws.winners, key=lambda it: -it.relevance))

    def test_habituation_lowers_an_item_that_kept_winning(self):
        plan = "From my sleep of x:\n\n- survey the orchard rows again with care"
        first = self.build(plan=plan)
        top = first.winners[0]
        for i in range(3):
            self.episode(f"2026-10-2{i}T09:00:00", "g", [], )
            self.state.append_episode({"ts": f"2026-10-2{i}T09:30:00", "kind": "live", "intent": None, "finish": None,
                                       "tools": [], "workspace": {"winners": [top.key()]}})
        again = self.build(plan=plan)
        same = next(it for it in again.pool if it.key() == top.key())
        self.assertAlmostEqual(same.habituation, 0.9)
        self.assertLess(same.salience, top.salience)

    def test_a_codelet_that_fails_is_recorded_and_the_rest_compete(self):
        with mock.patch.object(workspace, "_goals", side_effect=OSError("unreadable")):
            ws = self.build(plan="From my sleep of x:\n\n- survey the orchard rows again with care")
        self.assertEqual(ws.record()["errors"], ["goal: OSError: unreadable"])
        self.assertTrue(ws.winners)

    def test_refocus_competes_for_the_declared_goal_and_attention_in_competition_only(self):
        inbox = [("20261026-010000-000000-from-wren.md", "From: wren\n\nthe greenhouse sensor log is ready"),
                 ("20261026-010000-000001-from-thelos.md", "From: thelos\n\nthe kiln temperature chart is ready")]
        ws = self.build(inbox=inbox, k=1)
        ws.refocus("tidy my notes", [{"source": "kiln temperature chart", "why": "thelos's message"}])
        self.assertEqual(ws.winners[0].ref, inbox[1][0])            # attention broke the tie between two messages
        self.assertEqual(ws.record()["refocused"], [ws.winners[0].key()])
        rel = self.build("relevance", inbox=inbox, k=1)
        before = [w.key() for w in rel.winners]
        rel.refocus("tidy my notes", [{"source": "kiln temperature chart", "why": ""}])
        self.assertEqual([w.key() for w in rel.winners], before)   # the control has no top-down prior
        rel.refocus("read the greenhouse sensor log", [])
        self.assertEqual(rel.winners[0].ref, inbox[0][0])           # but it knows the declared goal, as competition does

    def test_off_builds_nothing(self):
        ws = self.build("off")
        self.assertEqual((ws.pool, ws.winners, ws.block(), ws.broadcast()), ([], [], "", ""))

    def test_attention_is_trimmed_never_an_error(self):
        self.assertEqual(workspace.attention(None), [])
        self.assertEqual(workspace.attention("web"), [{"source": "web", "why": ""}])
        self.assertEqual(len(workspace.attention([{"source": f"s{i}"} for i in range(9)])), 4)
        self.assertEqual(workspace.attention([{"why": "no source"}, 7, {"source": "memory", "why": "w"}]),
                         [{"source": "memory", "why": "w"}])


class LiveCycleTest(Fixture, unittest.TestCase):
    def test_every_mechanism_in_one_cycle(self):
        # a kind of action that failed two days ago and has not worked since; three comparable cycles for the forecast
        self.episode("2026-10-24T09:00:00", "Fix the orchard survey stats", [failing_write()])
        for d in ("21", "22", "23"):
            self.episode(f"2026-10-{d}T09:00:00", "Fix the orchard survey notes", [{"tool": "read_file",
                                                                                   "args": "{}", "result": "ok"}])
        attention = [{"source": "notes/", "why": "the survey notes"}, {"source": "memory", "why": "old rows"}]
        promise = {"promise": "send the operator the row count", "due": "tomorrow", "criterion": "a message with it",
                   "made_to": "operator"}
        write = ("write_file", {"path": "self/stats.md", "content": "rows"})
        fake = self.live([
            {"tool_calls": [("declare_intent", {**DECLARE, "attention": attention})]},
            {"tool_calls": [write]},
            {"tool_calls": [write]},
            {"tool_calls": [("commit_to", promise)]},
            {"tool_calls": [("finish_cycle", {**FINISH, "commitments": [dict(promise, promise="file the survey")]})]},
        ])
        ep = self.last()
        results = [(t["tool"], t["result"]) for t in ep["tools"]]
        # D1: the forecast follows the declare's own answer
        self.assertIn("Forecast from my 4 most comparable past cycles", results[0][1])
        self.assertNotIn("met", results[0][1].split("Forecast", 1)[1])
        # B1: held once with the record, then run when called again (not refused as a repeat)
        self.assertTrue(results[1][1].startswith(cards.HELD))
        self.assertIn("1 failed, 0 succeeded", results[1][1])
        self.assertTrue(results[2][1].startswith("ERROR: PermissionError"))
        self.assertEqual(ep["cards"]["held"][0]["family"], "write_file self/")
        # B3: the attention schema is recorded and re-focused the workspace
        self.assertEqual(ep["intent"]["attention"], attention)
        # B2: the workspace replaced recall, its winners are recorded and broadcast after the steps
        self.assertEqual(ep["workspace"]["mode"], "competitive")
        self.assertTrue(ep["workspace"]["winners"])
        first_user = fake.requests[0]["messages"][1]["content"]
        self.assertIn("## In focus now", first_user if isinstance(first_user, str) else json.dumps(first_user))
        later = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertTrue(any("[runtime] In focus:" in m["content"] for m in later))
        # C2: commit_to and finish_cycle's commitments, both recorded
        made = [json.loads(line) for line in (self.root / commitments.FILE).read_text().splitlines()]
        self.assertEqual([m["promise"] for m in made], ["send the operator the row count", "file the survey"])
        self.assertEqual({m["source"] for m in made}, {"cycle"})
        # the replay clock (SHAFT_NOW) advances with the process: the minute in an id is not pinned
        self.assertRegex(results[3][1], r"^commitment c1026-10\d\d-1 recorded")
        self.assertRegex(results[4][1], r"commitment c1026-10\d\d-[12] recorded")
        # the open commitments are in the prompt of the next run
        fake = self.live([{"content": "nothing"}])
        self.assertIn("## My open commitments", fake.requests[0]["messages"][0]["content"])
        self.assertIn("send the operator the row count", fake.requests[0]["messages"][0]["content"])

    def test_every_knob_off_is_the_cycle_as_before(self):
        for knob, value in (("OUTCOME_CARDS", "off"), ("WORKSPACE", "off"), ("ATTENTION_SCHEMA", "off"),
                            ("COMMITMENTS", "off"), ("FORECAST", "off")):
            self.cfg.values[knob] = value
        self.episode("2026-10-24T09:00:00", "Fix the orchard survey stats", [failing_write()])
        write = ("write_file", {"path": "self/stats.md", "content": "rows"})
        fake = self.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"tool_calls": [write]},
                          {"tool_calls": [("finish_cycle", FINISH)]}])
        ep = self.last()
        self.assertEqual(ep["tools"][0]["result"], "intent recorded — now act")
        self.assertTrue(ep["tools"][1]["result"].startswith("ERROR: PermissionError"))   # not held
        self.assertEqual(ep["cards"], {"mode": "off", "held": []})
        self.assertNotIn("workspace", ep)
        self.assertNotIn("attention", ep["intent"])
        offered = {t["function"]["name"]: t for t in fake.requests[0]["tools"]}
        self.assertNotIn("commit_to", offered)
        self.assertNotIn("attention", offered["declare_intent"]["function"]["parameters"]["properties"])
        self.assertNotIn("commitments", offered["finish_cycle"]["function"]["parameters"]["properties"])
        self.assertNotIn("## In focus now", fake.requests[0]["messages"][1]["content"])
        self.assertNotIn("## My open commitments", fake.requests[0]["messages"][0]["content"])

    def test_a_broken_workspace_falls_back_to_recall(self):
        with mock.patch.object(workspace, "build", side_effect=RuntimeError("boom")):
            self.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"tool_calls": [("finish_cycle", FINISH)]}])
        ep = self.last()
        self.assertEqual(ep["workspace"]["mode"], "off")
        self.assertEqual(ep["workspace"]["errors"], ["RuntimeError: boom"])
        self.assertIsNotNone(ep["finish"])


class CommitmentsElsewhereTest(Fixture, unittest.TestCase):
    def test_sleep_settles_an_overdue_promise_only_with_evidence(self):
        made = commitments.add(self.root, {"promise": "count rows", "due": "2026-10-25", "criterion": "a note",
                                           "made_to": "operator"}, "cycle", datetime(2026, 10, 24, 9))
        cid = made.split()[1]
        self.episode("2026-10-24T12:00:00", "count rows", [{"tool": "write_file", "args": "{}", "result": "wrote 4"}])
        fake, client = self.model([
            {"tool_calls": [("settle_commitment", {"id": cid, "status": "kept", "evidence": "2026-10-23T12:00:00"})]},
            {"tool_calls": [("settle_commitment", {"id": cid, "status": "kept", "evidence": "2026-10-24T12:00:00#1"})]},
            {"content": "Slept."}])
        self.cfg.values["MAX_STEPS_SLEEP"] = 4
        sleep.run(self.cfg, self.state, client)
        prompt = fake.requests[0]["messages"][1]["content"]
        self.assertIn("8. Settle every overdue commitment", prompt)
        self.assertIn(cid, prompt.split("## My overdue commitments (step 8)")[1])
        log = self.last()["tools"]
        self.assertTrue(log[0]["result"].startswith("ERROR: no such episode"))
        self.assertTrue(log[1]["result"].startswith(f"settled {cid} as kept"))
        self.assertEqual(commitments.fold(self.root, datetime(2026, 10, 26))[cid]["status"], "kept")

    def test_conversations_offer_commit_to_and_the_file_is_protected(self):
        box = Toolbox(self.root)
        self.assertNotIn("commit_to", box.describe())
        box.commit_source = "talk"
        self.assertIn("commit_to", box.describe())
        out = box.run("commit_to", json.dumps({"promise": "p", "due": "tomorrow", "criterion": "c", "made_to": "self"}))
        self.assertTrue(out.startswith("commitment c"), out)
        self.assertEqual(json.loads((self.root / commitments.FILE).read_text())["source"], "talk")
        refused = box.run("write_file", json.dumps({"path": commitments.FILE, "content": "{}"}))
        self.assertIn("use commit_to", refused)


class ReviewRegressionTest(Fixture, unittest.TestCase):
    """Codex's review of the integration, 2026-10-26."""

    def test_a_ledger_that_cannot_be_read_never_stops_a_cycle_or_a_sleep(self):
        (self.root / commitments.FILE).mkdir(parents=True)   # reading or appending it raises IsADirectoryError
        promise = {"promise": "p", "due": "tomorrow", "criterion": "c", "made_to": "self"}
        self.live([{"tool_calls": [("declare_intent", DECLARE)]},
                   {"tool_calls": [("finish_cycle", {**FINISH, "commitments": [promise]})]}])
        ep = self.last()
        self.assertEqual(ep["finish"]["criterion_met"], "yes")
        self.assertTrue(ep["finish"]["commitments"][0].startswith("ERROR: commitment not recorded"))
        fake, client = self.model([{"tool_calls": [("settle_commitment", {"id": "c1", "status": "void",
                                                                          "evidence": "x"})]}, {"content": "Slept."}])
        self.cfg.values["MAX_STEPS_SLEEP"] = 3
        sleep.run(self.cfg, self.state, client)
        self.assertIn("could not be read", fake.requests[0]["messages"][1]["content"])
        self.assertTrue(self.last()["tools"][0]["result"].startswith("ERROR"))   # refused, not raised
        self.assertIn("Slept.", self.state.read(self.state.journal))

    def test_web_source_needs_a_successful_fetch_of_that_url(self):
        box = Toolbox(self.root)
        box.log += [{"tool": "fetch_page", "args": json.dumps({"url": "https://a.org/x"}), "result": "ERROR: timeout"},
                    {"tool": "write_file", "args": json.dumps({"path": "notes/x", "content": "https://b.org/y"}),
                     "result": "wrote"},
                    {"tool": "http_request", "args": json.dumps({"url": "https://c.org/z"}), "result": "HTTP 404 nope"},
                    {"tool": "fetch_page", "args": json.dumps({"url": "https://d.org/w/"}), "result": "Title\ntext"}]
        for url, ok in (("https://a.org/x", False), ("https://b.org/y", False), ("https://c.org/z", False),
                        ("https://d.org/w", True)):
            with self.subTest(url=url):
                out = box.run("remember", json.dumps({"fact": "f", "source": f"web:{url}"}))
                self.assertEqual(out == "remembered", ok, out)

    def test_the_rendered_block_fits_recalls_budget(self):
        ws = workspace.Workspace("competitive", k=3)
        ws.pool = [workspace.Item("memory", "journal " + "a very long heading " * 10 + str(i), "word " * 400,
                                  relevance=1.0) for i in range(3)]
        ws.score()
        ws.winners = ws.select()
        rows = ws.block().split("\n", 3)[3]   # after the separator and the heading
        self.assertLessEqual(len(rows), workspace.BUDGET)
        self.assertLessEqual(len(ws.broadcast()), 3 * (workspace.BROADCAST_CHARS + 60) + 30)


class KnobTest(unittest.TestCase):
    def test_a_bad_knob_value_keeps_the_default(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            cfg = config.load(Path(d), environ={"LLM_BASE_URL": "x", "LLM_MODEL": "m", "WORKSPACE": "global"})
        self.assertEqual((cfg.WORKSPACE, cfg.sources["WORKSPACE"]), ("competitive", "default"))
        self.assertIn("must be one of competitive, relevance, off", err.getvalue())


if __name__ == "__main__":
    unittest.main()
