"""Tests for supervisor/cycles.py: day list, filters, paging, skipped lines, and judge verdicts."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import autonomy, commitments, core, cycles, panel


class CyclesDaysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-cycles-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inst = self.tmp / "instances"
        self.sup = self.tmp / "sup"
        self.root = self.inst / "wren"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / ".git").write_text("")
        self.eps = self.root / "memory" / "episodes"
        self.eps.mkdir(parents=True)
        self.sup.mkdir(parents=True)
        for p in (mock.patch.object(core, "INSTANCES", self.inst),
                  mock.patch.object(core, "SUP_DIR", self.sup),
                  mock.patch.object(panel, "SUP_DIR", self.sup)):
            p.start()
            self.addCleanup(p.stop)
        panel._memo.clear()

    def ep(self, day: str, **record) -> None:
        self.eps.mkdir(parents=True, exist_ok=True)
        with (self.eps / f"{day}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    def judge(self, **record) -> None:
        folder = self.sup / "judgments"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "wren.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def journal(self, text: str) -> None:
        self.write("memory/journal.md", text)

    def test_counts_cover_live_cycles_and_claims_only(self):      # sleep has no finish by design
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a", "origin": "operator"},
                finish={"criterion_met": "partial", "delivered_via": "message"})
        self.ep("2026-11-05", kind="sleep", ts="2026-11-05T04:00:00", content="slept")
        d = cycles.days("wren")["days"][0]
        self.assertEqual(d["counts"],
                         {"cycles": 1, "yes": 0, "partial": 1, "no": 0, "none": 0, "judged": 0, "for_you": 1})
        self.assertEqual([r["kind"] for r in d["rows"]], ["cycle", "sleep"])

    def test_fell_short_is_live_cycles_only(self):
        self.ep("2026-11-05", kind="sleep", ts="2026-11-05T04:00:00", content="slept")
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "unfinished"})   # no finish
        self.assertEqual([r["goal"] for r in cycles.days("wren", filt="short")["days"][0]["rows"]], ["unfinished"])

    def test_a_verdict_of_partial_falls_short_though_the_claim_says_yes(self):
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "overclaimed"},
                finish={"criterion_met": "yes"})
        self.ep("2026-11-05", kind="live", ts="2026-11-05T10:00:00", intent={"goal": "solid"},
                finish={"criterion_met": "yes"})
        self.judge(episode_ts="2026-11-05T09:00:00", criterion_met="partial", difficulty=2)
        self.judge(episode_ts="2026-11-05T10:00:00", criterion_met="yes", difficulty=2)
        d = cycles.days("wren", filt="short")["days"][0]
        self.assertEqual([r["goal"] for r in d["rows"]], ["overclaimed"])
        self.assertEqual((d["rows"][0]["claim"], d["rows"][0]["verdict"]), ("yes", "partial"))
        self.assertEqual(d["counts"],
                         {"cycles": 2, "yes": 2, "partial": 0, "no": 0, "none": 0, "judged": 2, "for_you": 0})

    def test_the_for_you_and_delivered_filters(self):   # delivered_via "none" and None are not delivered
        self.ep("2026-11-05", kind="live", ts="2026-11-05T08:00:00",
                intent={"goal": "x" * 350, "origin": "operator"},
                finish={"criterion_met": "yes", "delivered_via": "none"})
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00",
                intent={"goal": "sent", "origin": "self"},
                finish={"criterion_met": "yes", "delivered_via": "message"})
        self.ep("2026-11-05", kind="live", ts="2026-11-05T10:00:00",
                intent={"goal": "quiet", "origin": "self"},
                finish={"criterion_met": "yes", "delivered_via": None})
        self.ep("2026-11-05", kind="talk", ts="2026-11-05T11:00:00", content="chat")
        yours = cycles.days("wren", filt="yours")["days"][0]["rows"]
        self.assertEqual(len(yours), 1)
        self.assertEqual(yours[0]["goal"], "x" * 300)
        self.assertFalse(yours[0]["delivered"])
        delivered = cycles.days("wren", filt="delivered")["days"][0]["rows"]
        self.assertEqual([r["goal"] for r in delivered], ["sent"])
        self.assertTrue(delivered[0]["delivered"])

    def test_paging_runs_over_gaps_between_days(self):
        for day in ("2026-11-01", "2026-11-04", "2026-11-05"):
            self.ep(day, kind="live", ts=f"{day}T09:00:00", intent={"goal": day}, finish={"criterion_met": "yes"})
        first = cycles.days("wren", limit=2)
        self.assertEqual([d["date"] for d in first["days"]], ["2026-11-05", "2026-11-04"])
        self.assertEqual(first["next"], "2026-11-01")
        self.assertEqual(cycles.days("wren", before="2026-11-02", limit=2)["next"], None)

    def test_following_next_covers_every_day_once(self):
        for day in ("2026-11-01", "2026-11-04", "2026-11-05"):
            self.ep(day, kind="live", ts=f"{day}T09:00:00", intent={"goal": day}, finish={"criterion_met": "yes"})
        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = cycles.days("wren", before=cursor, limit=1)
            seen.extend(d["date"] for d in page["days"])
            cursor = page["next"]
            if cursor is None:
                break
        self.assertEqual(seen, ["2026-11-05", "2026-11-04", "2026-11-01"])

    def test_a_half_written_last_line_is_counted_not_fatal(self):  # review focus 4
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"})
        with (self.eps / "2026-11-05.jsonl").open("a") as f:
            f.write('{"kind": "live", "ts"')
        d = cycles.days("wren")["days"][0]
        self.assertEqual((len(d["rows"]), d["skipped_lines"]), (1, 1))
        lines, skipped = cycles._lines("wren", "2026-11-05")
        self.assertEqual((len(lines), lines[0][0], skipped), (1, 1, 1))

    def test_an_unreadable_judge_file_leaves_the_cycles_and_says_so(self):  # review focus 3
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"})
        with mock.patch.object(panel, "judgments", side_effect=ValueError("bad line")):
            out = cycles.days("wren")
        self.assertIn("bad line", out["judge_error"])
        self.assertTrue(out["days"])

    def test_no_tool_log_reaches_a_row(self):
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"},
                tools=[{"tool": "run", "args": "SECRET", "result": "SECRET"}])
        self.assertNotIn("SECRET", json.dumps(cycles.days("wren")))

    def test_an_instance_with_no_episodes_has_no_days(self):
        self.assertEqual(cycles.days("wren")["days"], [])

    def test_now_never_carries_doing(self):
        p = cycles.plan("wren", {"state": "in a cycle", "since": "09:00", "goal": "g", "doing": "run SECRET"})
        self.assertEqual(p["now"], {"state": "in a cycle", "since": "09:00", "goal": "g"})
        self.assertNotIn("SECRET", json.dumps(p))

    def test_a_stale_goal_is_not_shown_while_idle(self):
        self.assertIsNone(cycles.plan("wren", {"state": "idle", "goal": "old"})["now"]["goal"])

    def test_next_step_walks_back_past_a_cycle_without_one(self):
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"},
                finish={"next_action": {"action": "do b", "revise_if": "x"}, "unfinished": ["b"]})
        self.ep("2026-11-05", kind="live", ts="2026-11-05T10:00:00", intent={"goal": "stopped"})
        p = cycles.plan("wren", {})
        self.assertEqual(p["next_step"], {"ts": "2026-11-05T09:00:00", "action": "do b", "revise_if": "x"})
        self.assertEqual(p["unfinished"], ["b"])

    def test_a_next_action_that_is_not_an_object_is_skipped(self):
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"},
                finish={"next_action": {"action": "do b"}, "unfinished": ["b"]})
        self.ep("2026-11-05", kind="live", ts="2026-11-05T10:00:00", intent={"goal": "stopped"},
                finish={"next_action": "do c", "unfinished": ["c"]})
        p = cycles.plan("wren", {})
        self.assertEqual(p["next_step"], {"ts": "2026-11-05T09:00:00", "action": "do b", "revise_if": None})
        self.assertEqual(p["unfinished"], ["b"])

    def test_unfinished_that_is_not_a_list_does_not_break_the_plan(self):
        self.ep("2026-11-05", kind="live", ts="2026-11-05T09:00:00", intent={"goal": "a"},
                finish={"next_action": {"action": "do b"}, "unfinished": 7})
        p = cycles.plan("wren", {})
        self.assertEqual(p["unfinished"], [])
        self.assertEqual(p["next_step"]["action"], "do b")

    def test_no_queue_says_none(self):
        self.assertEqual(cycles.plan("wren", {})["queue"]["state"], "none")

    def test_a_queue_of_another_shape_is_unreadable(self):
        self.write("notes/queue.json", '{"tasks": 3}')
        q = cycles.plan("wren", {})["queue"]
        self.assertEqual(q["state"], "unreadable")
        self.assertTrue(q["error"])

    def test_a_link_in_place_of_the_queue_or_goals_is_not_followed(self):
        secret = self.tmp / "secret.txt"
        secret.write_text("SECRET")
        for d in ("self", "notes"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / "self" / "goals.md").symlink_to(secret)
        (self.root / "notes" / "queue.json").symlink_to(secret)
        p = cycles.plan("wren", {})
        self.assertNotIn("SECRET", json.dumps(p))
        self.assertEqual(p["goals"], "")
        self.assertTrue(p["goals_error"])
        self.assertEqual(p["queue"]["state"], "unreadable")
        self.assertTrue(p["queue"]["error"])

    def test_queue_rows_order_and_cap(self):
        now = datetime(2026, 11, 5, 12, 0, 0)
        rows = [
            {"id": 1, "task": "t1", "due_at": "2026-11-05T11:00:00"},
            {"id": 2, "task": "t2", "due_at": ""},
            {"id": 3, "task": "t3", "due_at": "2026-11-05T10:00:00"},
            {"id": 4, "task": "t4", "due_at": "2026-11-06T09:00:00"},
            {"id": 5, "task": "t5", "due_at": "2026-11-05T13:00:00"},
            {"id": 6, "task": "t6", "due_at": "2026-11-05T18:00:00"},
            {"id": 7, "task": "t7", "due_at": "2026-11-06T08:00:00"},
            {"id": 8, "task": "t8", "due_at": "not a date"},
            {"id": 9, "task": "t9", "due_at": "2026-11-05T09:00:00"},
            {"id": 10, "task": "t10", "due_at": "2026-11-06T10:00:00"},
            {"id": 11, "task": "t11", "due_at": "2026-11-04T10:00:00"},
            {"id": 12, "task": "t12", "due_at": "2026-11-07T10:00:00"},
            {"id": 13, "task": "t13", "due_at": "", "done": True},
            {"id": 14, "task": "t14", "due_at": "", "done": True},
        ]
        self.write("notes/queue.json", json.dumps(rows))
        q = cycles.plan("wren", {}, now=now)["queue"]
        self.assertEqual((q["open"], len(q["rows"])), (12, 10))
        self.assertEqual(q["overdue"], 5)
        self.assertEqual([r["id"] for r in q["rows"]], [11, 9, 3, 1, 2, 5, 6, 7, 4, 10])

    def test_a_long_queue_task_is_clipped_at_200(self):
        self.write("notes/queue.json", json.dumps([{"id": 1, "task": "x" * 250}]))
        row = cycles.plan("wren", {})["queue"]["rows"][0]
        self.assertEqual(row["task"], "x" * 200)

    def test_commitments_to_itself_are_left_out(self):
        ledger = {
            "c1": {"id": "c1", "promise": "send notes", "due": "2026-11-05T14:00:00", "status": "open",
                   "made_to": "operator"},
            "c2": {"id": "c2", "promise": "self thing", "due": "2026-11-05T13:00:00", "status": "open",
                   "made_to": "self"},
            "c3": {"id": "c3", "promise": "own thing", "due": "2026-11-05T12:00:00", "status": "overdue",
                   "made_to": "own"},
            "c4": {"id": "c4", "promise": "wren thing", "due": "2026-11-05T11:00:00", "status": "open",
                   "made_to": "wren"},
        }
        with mock.patch.object(commitments, "ledger", return_value=ledger):
            self.assertEqual(cycles.plan("wren", {})["commitments"],
                             [{"id": "c1", "promise": "send notes", "due": "2026-11-05T14:00:00",
                               "status": "open"}])

    def test_the_improvement_plan_stops_at_consolidation(self):
        self.journal("## 2026-11-05 04:00 — Sleep\n\nslept\n\n**How I will improve:** run less\n\n"
                     "*Consolidation actions:* x\n")
        self.assertEqual(cycles.plan("wren", {})["improvement"]["text"], "**How I will improve:** run less")

    def test_paused_is_said(self):
        with mock.patch.object(autonomy, "paused", return_value=True):
            p = cycles.plan("wren", {})
        self.assertEqual(p["check"]["paused"], True)
        self.assertEqual(p["check"]["next"], "paused")

    def test_a_new_instance_has_an_empty_plan_not_an_error(self):
        p = cycles.plan("wren", {})
        self.assertIsNone(p["next_step"])
        self.assertEqual(p["unfinished"], [])
        self.assertEqual(p["commitments"], [])
        self.assertEqual(p["queue"]["state"], "none")
        self.assertEqual(p["goals"], "")
        self.assertEqual(p["goals_error"], "no goals file")
        self.assertIsNone(p["improvement"])
        self.assertEqual(p["now"], {"state": None, "since": None, "goal": None})


if __name__ == "__main__":
    unittest.main()
