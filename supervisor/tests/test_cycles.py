"""Tests for supervisor/cycles.py: day list, filters, paging, skipped lines, and judge verdicts."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, cycles, panel


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


if __name__ == "__main__":
    unittest.main()
