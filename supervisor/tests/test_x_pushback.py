"""#141: disagreement needs cited evidence; later outcomes and annoyance stay apart."""
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import core, judge, messages, metrics, panel, pushback, pushes, web_actions
from supervisor.tests.test_surprise_judge import VERDICT

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import prompts

NOW = datetime(2026, 10, 28, 12)


class PushbackTest(unittest.TestCase):
    """#141: exercise real temporary records and mocked paid judgments."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-x-pushback-")))
        self.sup = self.root / "sup"
        self.sup.mkdir()
        for module in (core, messages, metrics, panel):
            self.enterContext(mock.patch.object(module, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "INSTANCES", self.root / "agents"))
        self.enterContext(mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"))
        for module in (messages, metrics, pushback):
            clock = self.enterContext(mock.patch.object(module, "datetime", wraps=datetime))
            clock.now.return_value = NOW
            clock.min = datetime.min
        # panel.dismiss forgets ids of days older than DISMISS_KEEP_DAYS by date.today(): unpinned, the 10-28 id below
        # was dropped as soon as the real date passed 2026-10-31, and the test failed by the calendar alone.
        self.enterContext(mock.patch.object(panel, "date", wraps=date)).today.return_value = NOW.date()
        self.ask = self.enterContext(mock.patch.object(judge, "ask", return_value={
            "borne_out": "yes", "reason": "The later test confirmed the cited error."}))
        self.enterContext(mock.patch.object(judge, "pick", return_value="test-judge"))
        self.enterContext(mock.patch.object(metrics, "own_models", return_value=[]))
        self.enterContext(mock.patch.object(metrics, "_provenance_evidence", return_value="operator request"))

    def store(self, rows, episodes=()):
        folder = self.sup / "judgments"
        folder.mkdir(exist_ok=True)
        path = folder / "m.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        folder = self.root / "agents/m/memory/episodes"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "day.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_prompt_rule_in_talk_api_live(self):
        """#141: every acting mode requires evidence-based disagreement without a quota."""
        for prompt in (prompts.TALK_MODE, prompts.API_MODE, prompts.LIVE):
            for text in ("Disagreement with the operator is allowed", "must state the disagreement",
                         "cite the tool call and what it showed", "a week without disagreement is fine",
                         "never winning an argument"):
                self.assertIn(text, prompt)

    def test_fresh_required_old_loads_uncited_schema_and_instructions(self):
        """#141: refuse holes in fresh judgments; historical records remain valid and uncited is unsupported."""
        self.assertEqual(metrics.PUSHBACK_PROPS["pushback"]["enum"], ["none", "cited", "uncited"])
        for schema in (metrics.JUDGE_SCHEMA, metrics.CONTROL_SCHEMA):
            self.assertIn("pushback", schema["required"])
        self.assertIn("A pushback with no cited tool result scores as unsupported", metrics.JUDGE_SYSTEM)
        self.assertIn("grade it uncited", metrics.CONTROL_SYSTEM)
        self.assertEqual(set(metrics.CONTROL_SCHEMA["properties"]) | set(metrics.SURPRISES),
                         set(metrics.JUDGE_SCHEMA["properties"]))
        for value in ("none", "cited", "uncited"):
            metrics._check_ratings({**VERDICT, "pushback": value})
        for value in (None, True, "unsupported"):
            with self.assertRaises(judge.JudgeError):
                metrics._check_ratings({**VERDICT, "pushback": value})
        old = {k: v for k, v in VERDICT.items() if k != "pushback"}
        with self.assertRaisesRegex(judge.JudgeError, "pushback"):
            metrics._check_ratings(old)
        old.update(episode_ts=NOW.isoformat(), judged_at="2026-10-01")
        self.store([old])
        self.assertEqual(metrics.judgments("m"), [old])
        text = "\n".join(pushback.lines("m", [old, {**old, "pushback": "uncited"}], NOW))
        self.assertIn("cited pushbacks per week (last 7 days): 0; uncited 1, ungraded 1", text)
        with mock.patch.object(metrics, "live_episodes", return_value=[{"ts": NOW.isoformat(), "intent": {"goal": "g"}}]), \
             mock.patch.object(metrics, "judgments", return_value=[]), \
             mock.patch.object(metrics, "unsampled", return_value=set()), \
             mock.patch.object(metrics, "judge_sampled", return_value=True), \
             mock.patch.object(metrics, "second_sampled", return_value=False), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_followed_surprise", return_value=""), \
             mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(metrics, "log"):
            self.ask.return_value = {k: v for k, v in old.items() if k != "pushback"}
            before = self.snapshot()
            self.assertEqual(metrics.judge_pending("m"), 0)
            self.assertEqual(self.snapshot(), before)
            self.ask.return_value = {**VERDICT, "pushback": "uncited"}
            self.assertEqual(metrics.judge_pending("m"), 1)
            stored = json.loads((self.sup / "judgments/m.jsonl").read_text().splitlines()[-1])
            self.assertEqual((stored["pushback"], stored["criterion_met"]), ("uncited", "yes"))
            self.assertIn("uncited 1", "\n".join(pushback.lines("m", [stored], NOW)))

    def test_cli_dry_run_unchanged_write_separate_and_resume(self):
        """#141: dry run changes no bytes; --write stores later evidence apart and never repays scored cycles."""
        stamp = (NOW - timedelta(days=4)).isoformat()
        row = {"episode_ts": stamp, "judged_at": "2026-10-01", "pushback": "cited"}
        ep = {"ts": stamp, "kind": "live", "tools": []}
        later = {"ts": (NOW - timedelta(days=1)).isoformat(), "kind": "live", "finish": {"what_happened": "TEST-CONFIRMED"}}
        talk = {"ts": NOW.isoformat(), "kind": "talk", "role": "user", "content": "LATER-CONVERSATION"}
        path = self.store([row, {**row, "episode_ts": NOW.isoformat()},
                           {**row, "episode_ts": (NOW - timedelta(days=5)).isoformat(), "pushback": "uncited"}],
                          [ep, later, talk])
        messages.record(self.sup, Path("reply.md"), "operator", "m", "message", NOW, "LATER-MESSAGE")
        before = self.snapshot()
        with mock.patch.object(cli, "instance_root"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(["judge", "pushback-outcome", "m", "--since", "2026-10-23"]), 0)
        self.assertIn("1 eligible cycle(s)", out.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.ask.assert_not_called()
        original = path.read_bytes()
        with mock.patch.object(cli, "instance_root"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(["judge", "pushback-outcome", "m", "--write"]), 0)
        self.assertIn("| yes |", out.getvalue())
        self.assertEqual(path.read_bytes(), original)
        stored = json.loads((self.sup / "judgments/m-pushback-outcomes.jsonl").read_text())
        self.assertEqual((stored["episode_ts"], stored["borne_out"]), (stamp, "yes"))
        self.ask.assert_called_once()
        case = self.ask.call_args.args[1]
        for text in ("TEST-CONFIRMED", "LATER-CONVERSATION", "LATER-MESSAGE"):
            self.assertIn(text, case)
        pushback.outcome("m", write=True)
        self.ask.assert_called_once()
        for kw in ({"days": -1}, {"since": "bad date"}):
            with self.assertRaises(ValueError):
                pushback.outcome("m", **kw)

    def test_archive_and_panel_dismiss_timestamp_preserve_behavior(self):
        """#141: inbox clear moves notes only; terminal and web alert dismissals keep their original ids."""
        inbox = messages.OPERATOR_INBOX
        inbox.mkdir()
        for kind in ("note", "question"):
            (inbox / f"{kind}.md").write_text(f"From: m\nKind: {kind}\n\nbody")
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(messages.clear_notes(), 0)
        self.assertFalse((inbox / "note.md").exists())
        self.assertTrue((inbox / "read/note.md").exists())
        self.assertTrue((inbox / "question.md").exists())
        panel.dismiss(["cap:m:cloud:2026-10-28"])
        self.assertIn("cap:m:cloud:2026-10-28", panel.dismissed())
        events = [json.loads(l) for l in (self.sup / "dismissals.jsonl").read_text().splitlines()]
        self.assertEqual([(r["action"], r["ref"], r["ts"]) for r in events],
                         [("archive", "note.md", NOW.isoformat()),
                          ("dismiss", "cap:m:cloud:2026-10-28", NOW.isoformat())])
        self.assertEqual(events[1]["name"], "m")
        ref = "correction:m:test"
        def run_cli(args, **kwargs):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                return 200 if cli.main(args) == 0 else 409, "dismissed"
        with mock.patch.object(web_actions, "_ran", side_effect=run_cli):
            status, _ = web_actions.run("dismiss", {"ids": json.dumps([ref])},
                                        {"alerts": [{"id": ref}]}, ["m"], [])
        self.assertEqual(status, 200)
        self.assertIn(ref, panel.dismissed())
        event = json.loads((self.sup / "dismissals.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["ref"], event["ts"], event["action"]), (ref, NOW.isoformat(), "dismiss"))
        with mock.patch.object(messages.fcntl, "flock", side_effect=OSError("measurement unavailable")):
            messages._archive(inbox / "question.md")
            panel.dismiss(["log:test"])
        self.assertTrue((inbox / "read/question.md").exists())
        self.assertIn("log:test", panel.dismissed())

    def test_a_reply_that_archives_the_message_is_not_annoyance(self):
        """#141: answering a message archives it too; the first version counted that as a dismissal, so every quick
        reply to a pushback looked like annoyance. Only an unanswered clear counts."""
        sent = NOW - timedelta(hours=1)
        end = sent + timedelta(seconds=5)
        messages.record(self.sup, Path("push-r.md"), "m", "operator", "note", sent, "disagree-r")
        episodes = [{"ts": end.isoformat(), "kind": "live", "run_started": (sent - timedelta(seconds=1)).isoformat(),
                     "tools": [{"tool": "send_message", "args": json.dumps({"to": "operator", "text": "disagree-r"}),
                                "result": "delivered to the operator"}]}]
        rows = [{"episode_ts": end.isoformat(), "judged_at": "2026-10-01", "pushback": "cited"}]
        with mock.patch.object(messages.datetime, "now", return_value=sent + timedelta(seconds=20)):
            messages.dismissal(self.sup, "reply", "push-r.md")
        self.store(rows, episodes)
        self.assertEqual(pushback.annoyance("m", rows, NOW), 0)

    def test_annoyance_60_seconds_not_61_and_stats_apart(self):
        """#141: count a pushback cycle once at 60 s, exclude 61 s and unrelated message archives."""
        episodes, rows = [], []
        for i, delay in enumerate((60, 61)):
            sent = NOW - timedelta(hours=i + 1)
            end = sent + timedelta(seconds=5)
            text = f"disagree-{i}"
            messages.record(self.sup, Path(f"push-{i}.md"), "m", "operator", "note", sent, text)
            episodes.append({"ts": end.isoformat(), "kind": "live", "run_started": (sent - timedelta(seconds=1)).isoformat(),
                             "tools": [{"tool": "send_message", "args": json.dumps({"to": "operator", "text": text}),
                                        "result": "delivered to the operator"}]})
            rows.append({"episode_ts": end.isoformat(), "judged_at": "2026-10-01", "pushback": "cited"})
            with mock.patch.object(messages.datetime, "now", return_value=sent + timedelta(seconds=delay)):
                messages.dismissal(self.sup, "archive", f"push-{i}.md")
        self.store(rows, episodes)
        self.assertEqual(pushback.annoyance("m", rows, NOW), 1)
        self.assertEqual(pushback.annoyance("other", rows, NOW), 0)
        (self.sup / "judgments/m-pushback-outcomes.jsonl").write_text(json.dumps({
            "episode_ts": rows[0]["episode_ts"], "borne_out": "yes"}) + "\n")
        text = "\n".join(pushback.lines("m", rows, NOW))
        self.assertIn("cited pushbacks per week (last 7 days): 2", text)
        self.assertIn("pushbacks borne out (of scored, all time): 1/1 (100%)", text)
        self.assertIn("pushback annoyance (last 7 days, message archived within 60 s): 1", text)
        with mock.patch.object(metrics, "judgments", return_value=[]):
            for key in ("_battery_section", "_code_section", "_traits_section", "_trials_section", "_sections"):
                self.enterContext(mock.patch.object(metrics, key, return_value=[]))
            for report in (metrics.stats_text("m"), metrics.review_text(["m"], now=NOW)):
                for label in ("cited pushbacks per week", "pushbacks borne out", "pushback annoyance"):
                    self.assertIn(label, report)
        self.assertNotIn("annoyance", metrics.JUDGE_SCHEMA["properties"])
        self.assertNotIn("dismissals", self.ask.call_args.args[1] if self.ask.called else "")


if __name__ == "__main__":
    unittest.main()
