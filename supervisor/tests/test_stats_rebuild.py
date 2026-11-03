"""Review C1-C3/C14: rebuild work is shared without changing the stats text."""
from __future__ import annotations

import contextlib
import importlib
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import core, judge, metacog, metrics, provenance, tom
from supervisor.tests.test_metacog import row

NOW = datetime(2026, 11, 3, 12)
TEXT = "The orchard survey found eight healthy trees beside the eastern gate."


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@contextlib.contextmanager
def fixture(count=20, padding=0):
    with tempfile.TemporaryDirectory(prefix="shaft-stats-rebuild-") as tmp, contextlib.ExitStack() as stack:
        base = Path(tmp)
        roots, sup = base / "birds", base / "supervisor"
        root = roots / "bird"
        (root / "memory/episodes").mkdir(parents=True)
        (sup / "judgments").mkdir(parents=True)
        for module in ("core", "metrics", "provenance", "metacog", "attention", "commitments", "consultstats",
                       "projects", "tom", "pushes", "pushback", "surprise", "battery", "trial"):
            mod = importlib.import_module("supervisor." + module)
            for key, value in (("INSTANCES", roots), ("SUP_DIR", sup), ("datetime", Clock)):
                if hasattr(mod, key):
                    stack.enter_context(mock.patch.object(mod, key, value))
        stack.enter_context(mock.patch.object(judge, "JUDGE_MODELS", ["fixture-judge"]))
        stack.enter_context(mock.patch.object(judge, "JUDGE_EVERY", 2))
        stack.enter_context(mock.patch.object(metrics, "log", side_effect=AssertionError))
        episodes, judgments = [], []
        for i in range(count):
            ts = (NOW - timedelta(hours=(count - i) * 6)).isoformat()
            episodes.append({"kind": "live", "ts": ts, "run_started": ts, "intent": {"at": ts, "project": "p2026-1103-1",
                             "goal": "Inspect trees", "success_criterion": "Trees counted", "confidence": 70},
                             "finish": {"what_happened": "Inspected trees", "learned": "Counted eight trees"},
                             "tools": [{"tool": "fetch_page", "args": '{"url":"https://example.org/trees"}',
                                        "result": TEXT + f" Result {i}. " + "orchard survey tree " * padding}]})
            judgments.append({**row(80 if i % 2 else 20, bool(i % 2)), "episode_ts": ts,
                              "judge": "fixture-judge", "goal": "Inspect trees", "reasoning": "Count checked",
                              "claimed": "yes", "claim_supported": True, "criterion_quality": "checkable",
                              "failure_mode": "", "difficulty": 2, "origin": "self"})
        path = root / "memory/episodes/log.jsonl"
        path.write_text("\n".join(map(json.dumps, episodes)) + "\n")
        (sup / "judgments/bird.jsonl").write_text("\n".join(map(json.dumps, judgments)) + "\n")
        (root / "memory/facts.md").write_text("\n".join(
            f"- [2026-11-03 12:00] {TEXT} (src: log:{ep['ts']}#1)" for ep in episodes[:120]))
        (root / "memory/projects.jsonl").write_text(json.dumps({"event": "opened", "id": "p2026-1103-1",
            "at": "2026-10-01T00:00:00", "name": "Tree report", "deadline": "2026-11-10T12:00:00",
            "success": "Trees counted"}) + "\n")
        (sup / "consult_usage.jsonl").write_text(json.dumps({"ts": episodes[-1]["ts"], "instance": "bird",
            "model": "fixture-model", "run": "live", "run_started": episodes[-1]["ts"], "usd": .01}) + "\n")
        yield root, path, episodes


class StatsRebuildTest(unittest.TestCase):
    def test_c3_stats_bytes_match_before_optimization(self):
        for count in (6, 20):
            with self.subTest(count=count), fixture(count):
                expected = Path(__file__).with_name(f"stats_rebuild_expected_{count}.txt").read_bytes()
                self.assertEqual(metrics.stats_text("bird").encode(), expected)
                self.assertEqual(metrics.stats_text("bird").encode(), expected)

    def test_c3_each_episode_file_parsed_once_per_rebuild_and_nothing_kept_after(self):
        """C3: one parse per rebuild. Integration review 2026-11-03: a module-wide cache kept every instance's parsed
        history in the long-running web service, which imports metrics."""
        with fixture() as (root, path, episodes), mock.patch.object(metrics, "safe_read", wraps=metrics.safe_read) as read:
            metrics.stats_text("bird")
            reads = [call for call in read.call_args_list if call.args[1].startswith("memory/episodes/")]
            self.assertEqual(len(reads), 1)
            self.assertIsNone(getattr(metrics._REBUILD, "episodes", None))
            self.assertEqual(metrics.live_episodes("bird"), episodes)
            self.assertEqual(read.call_count, 2)          # outside a rebuild: read fresh, never a stale copy
            with path.open("a") as stream:
                stream.write(json.dumps({"kind": "live", "ts": "new"}) + "\n")
            self.assertEqual(metrics.live_episodes("bird")[-1]["ts"], "new")
            path.unlink()
            self.assertEqual(metrics.live_episodes("bird"), [])

    def test_c1_dates_and_urls_indexed_once_for_many_facts(self):
        with fixture(), mock.patch.object(provenance, "_date", wraps=provenance._date) as dates, \
                mock.patch.object(provenance, "_urls", wraps=provenance._urls) as urls:
            pairs = provenance.checked("bird")
            self.assertEqual([status for _, status in pairs], ["matched"] * 20)
            self.assertLessEqual(dates.call_count, 80)
            self.assertEqual(urls.call_count, 20)

    def test_c1_result_shingled_once_even_after_a_different_citation_misses(self):
        episode = {"ts": "2026-11-01T00:00:00", "tools": [
            {"tool": "fetch_page", "args": "https://example.org/trees", "result": "Result: " + TEXT}]}
        evidence = provenance._Evidence([episode], "")
        fact = {"date": "2026-11-03", "text": TEXT, "src": "web:https://example.org/trees"}
        with mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            self.assertEqual(evidence.check(fact), "matched")
            self.assertEqual(evidence.check({**fact, "src": "web:https://example.org/other"}), "unmatched")
            self.assertEqual(evidence.check(fact), "matched")
            self.assertEqual(sum(call.args[0] == "Result: " + TEXT for call in shingles.call_args_list), 1)

    def test_c1_malformed_later_episode_does_not_break_an_earlier_match(self):
        # Grok's review of C1: the index walks every episode before any fact is checked, so a later row with a
        # non-dict intent/finish or non-list tools raised where the old per-fact scan had already returned "matched".
        good = {"ts": "2026-11-01T00:00:00", "intent": {"at": "2026-11-01T00:00:00"}, "tools": [
            {"tool": "fetch_page", "args": "https://example.org/trees", "result": "Result: " + TEXT}]}
        fact = {"date": "2026-11-03", "text": TEXT, "src": "web:https://example.org/trees"}
        for bad in ({"ts": "2026-11-02T00:00:00", "finish": "x"}, {"ts": "2026-11-02T00:00:00", "tools": 5},
                    {"ts": "2026-11-02T00:00:00", "intent": "x"}, {"ts": 5, "tools": [5, "x"], "finish": ["x"]}):
            with self.subTest(bad=bad):
                self.assertEqual(provenance.check("bird", fact, [good, bad], ""), "matched")
                self.assertEqual(provenance.check("bird", {**fact, "src": f"log:{good['ts']}#1"}, [good, bad], ""),
                                 "matched")

    def test_c2_ineligible_section_does_not_bootstrap_or_read_episodes(self):
        rows = [row(80, True, day) for day in range(1, 7)]
        with mock.patch.object(metrics, "judgments", return_value=rows), \
                mock.patch.object(metrics, "live_episodes") as episodes, \
                mock.patch.object(metacog, "cluster_ci", return_value=None) as bootstrap:
            self.assertEqual(metacog.section("bird"), [])
            bootstrap.assert_not_called()
            episodes.assert_not_called()

    def test_c2_intervals_cached_by_content_including_in_place_edits(self):
        rows = [row(80 if met else 20, met, day) for day in range(1, 7) for met in (True, False)]
        metacog._cached_ci.cache_clear()
        with mock.patch.object(metacog, "cluster_ci", wraps=metacog.cluster_ci) as bootstrap:
            first = metacog.summary(rows)
            self.assertEqual(metacog.summary(json.loads(json.dumps(rows))), first)
            self.assertEqual(bootstrap.call_count, 2)
            rows[0]["confidence"] = 0
            self.assertNotEqual(metacog.summary(rows)["mean_confidence"], first["mean_confidence"])
            self.assertEqual(bootstrap.call_count, 3)

    def test_c14_pending_receipts_list_each_folder_once_and_final_scores_skip_listing(self):
        from supervisor.tests.test_tom import TomTest
        case = TomTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        for i in range(3):
            tom.record("a", case.receipt(id=str(i), to="operator" if i < 2 else "b", gist=""))
        case.reply()
        case.reply(sender="operator")
        with mock.patch.object(tom, "list_beneath", wraps=tom.list_beneath) as listing, \
                mock.patch.object(tom, "_rows", wraps=tom._rows) as rows:
            self.assertEqual(tom.score("a", case.start + timedelta(hours=3)), 3)
            self.assertEqual([call.args[1] for call in listing.call_args_list], ["inbox", "memory/messages"])
            listing.reset_mock()
            self.assertEqual(tom.score("a", case.start + timedelta(hours=3)), 0)
            listing.assert_not_called()
            self.assertEqual(sum(call.args == ("a", "-operator-said") for call in rows.call_args_list), 1)


def benchmark(baseline=None):
    if baseline:
        for mod in (metrics, provenance, metacog, tom):
            path = Path(baseline) / (mod.__name__.rsplit(".", 1)[-1] + ".py")
            exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    with fixture(600, 6500) as (_, path, _):
        start = time.perf_counter()
        text = metrics.stats_text("bird")
        cold = time.perf_counter() - start
        start = time.perf_counter()
        assert metrics.stats_text("bird") == text
        unchanged = time.perf_counter() - start
        episode = json.loads(path.read_text().splitlines()[-1])
        episode["ts"] = (NOW - timedelta(minutes=30)).isoformat()
        with path.open("a") as stream:
            stream.write(json.dumps(episode) + "\n")
        start = time.perf_counter()
        appended = metrics.stats_text("bird")
        after_append = time.perf_counter() - start
        print(f"episode bytes={path.stat().st_size}; cold={cold:.3f}s; unchanged={unchanged:.3f}s; "
              f"after append={after_append:.3f}s")
        print(f"stats sha256={hashlib.sha256(text.encode()).hexdigest()}; "
              f"after append sha256={hashlib.sha256(appended.encode()).hexdigest()}")


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        benchmark(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        unittest.main()
