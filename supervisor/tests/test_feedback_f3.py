"""Feedback cuts training conversations and remains outside grading inputs."""
import ast
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import core, feedback, finetune, messages, web_chat


class FeedbackF3Test(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-feedback-f3-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.root = self.project / "test-roots" / "wisp"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / ".git").touch()
        self.episodes = self.root / "memory" / "episodes"
        self.episodes.mkdir(parents=True)
        self.sup = self.project / "supervisor-state"
        for patch in (
            mock.patch.object(core, "INSTANCES", self.root.parent),
            mock.patch.object(finetune, "PROJECT", self.project),
            mock.patch.object(finetune, "SUP_DIR", self.sup),
            mock.patch.object(feedback, "FILE", self.sup / "feedback.jsonl"),
            mock.patch.object(web_chat, "SUP_DIR", self.sup),
            mock.patch.object(messages, "SUP_DIR", self.sup),
            mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
            mock.patch.object(messages, "INSTANCES", self.root.parent),
            mock.patch.dict(messages._cache, key=None, rows=[]),
            mock.patch.object(finetune.metrics, "judgments", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def talk(self, tid, *contents, ts="2026-10-29T12:00:05"):
        return [{"kind": "talk", "talk_id": tid, "role": "user" if k % 2 == 0 else "assistant",
                 "content": text, "ts": ts} for k, text in enumerate(contents)]

    def write_episodes(self, rows):
        (self.episodes / "test.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def rate(self, text, thumb="down", cid="web-abc", k=1, name="wisp", reason="wrong"):
        return feedback.rate(name, "operator", {"kind": "chat", "chat": cid, "k": k}, thumb,
                             reason, "2026-10-29T12:00:00", text)

    def dataset(self):
        out = io.StringIO()
        with redirect_stdout(out):
            path, count = finetune.build_dataset("wisp")
        rows = [json.loads(line)["messages"][1:] for fname in ("train.jsonl", "valid.jsonl")
                for line in (path / fname).read_text().splitlines()]
        self.assertEqual(count, len(rows))
        return rows, out.getvalue()

    def test_dataset_same_text_elsewhere_is_not_cut(self):
        # A 👎 on a short reply ("Done.") must not cut every other conversation that happens to say the same.
        self.write_episodes(self.talk("rated", "do it", "Done.")
                            + self.talk("days-later", "and this", "Done.", ts="2026-11-02T09:00:00"))
        self.rate("Done.")
        rows, out = self.dataset()
        self.assertEqual([[t["content"] for t in turns] for turns in rows], [["and this", "Done."]])
        self.assertIn("1 conversations cut", out)

    def test_dataset_cuts_at_thumbs_down(self):
        self.write_episodes(self.talk("kept", "one", "good", "two", "bad", "three", "later")
                            + self.talk("lost", "first", "bad first", "last", "lost too")
                            + [{"kind": "reflect", "content": "bad"},
                               {"kind": "sleep", "content": "bad first"},
                               {"kind": "live", "ts": "live", "intent": {"goal": "g", "success_criterion": "c"},
                                "finish": {"what_happened": "bad", "learned": "lesson"}}])
        self.rate("bad", k=3)
        sent = self.rate("bad first", cid="web-other")
        feedback.mark_sent(sent["id"], "inbox", "sent.md")
        with mock.patch.object(finetune.metrics, "judgments", return_value=[
                {"episode_ts": "live", "criterion_met": "yes"}]):
            rows, output = self.dataset()
        self.assertEqual(len(rows), 4)
        self.assertIn([{"role": "user", "content": "one"}, {"role": "assistant", "content": "good"},
                       {"role": "user", "content": "two"}], rows)
        texts = [t["content"] for turns in rows for t in turns]
        self.assertNotIn("later", texts)
        self.assertNotIn("bad first", [t["content"] for turns in rows for t in turns if t["role"] == "user"])
        self.assertNotIn("lost too", texts)
        self.assertIn("bad", texts)  # reflect remains, despite identical text
        self.assertIn("bad first", texts)  # sleep remains
        self.assertIn("bad\n\nLearned: lesson", texts)
        self.assertIn("2 conversations cut, 6 turns dropped", output)

    def test_dataset_thumbs_up_changes_nothing(self):
        self.write_episodes(self.talk("talk", "one", "good", "two", "later"))
        before, _ = self.dataset()
        rating = self.rate("good", "up")
        self.assertEqual(self.dataset()[0], before)
        feedback.mark_sent(rating["id"], "inbox", "sent.md")
        self.assertEqual(self.dataset()[0], before)
        self.assertIn("0 conversations cut, 0 turns dropped", self.dataset()[1])

    def test_dataset_cut_matches_web_chat_text(self):
        for streamed, episode in [("  answer \n", "  answer \n"),
                                  ("leaked thought</think>  answer \n", "answer \n")]:
            with self.subTest(streamed=streamed):
                chat = {"id": "web-abc", "instance": "wisp", "state": "replying", "turns": [],
                        "reply": {"interjections": 0}}
                web_chat.save(chat)
                relay = web_chat.Relay("web-abc")
                # Stop suppresses unrelated notifications; _finish still performs the actual content save.
                relay.stopped = True
                with mock.patch.object(web_chat, "_docs_now", return_value={}), \
                        mock.patch("supervisor.docs.changed", return_value=[]), \
                        mock.patch.object(web_chat, "log"):
                    web_chat._finish(relay, streamed, [], None, 0)
                stored = web_chat.load("web-abc")["turns"][0]["content"]
                self.assertEqual(stored, streamed.strip())
                self.assertEqual(feedback.reply_sha(finetune._feedback_text(stored)),
                                 feedback.reply_sha(finetune._feedback_text(episode)))
                self.rate(stored, k=0)
                self.write_episodes(self.talk("talk", "one", "earlier", "two", episode, "three", "later"))
                self.assertEqual(self.dataset()[0], [[{"role": "user", "content": "one"},
                    {"role": "assistant", "content": "earlier"}, {"role": "user", "content": "two"}]])

    def test_dataset_uses_current_ratings_for_this_instance(self):
        self.write_episodes(self.talk("talk", "one", "answer", "two", "later"))
        before, _ = self.dataset()
        self.rate("answer", name="other")
        self.assertEqual(self.dataset()[0], before)
        rated = self.rate("answer")
        self.assertEqual(self.dataset()[0], [])
        feedback.clear(rated["id"], "operator")
        self.assertEqual(self.dataset()[0], before)
        self.rate("answer")
        self.rate("answer", "up")
        self.assertEqual(self.dataset()[0], before)

    def test_dataset_does_not_guess_from_partial_or_synthetic_replies(self):
        self.write_episodes(self.talk("talk", "one", "complete answer", "two", "later"))
        before, _ = self.dataset()
        self.rate("complete", cid="web-partial")
        self.rate("(no answer: the reply stopped after 3 steps — steps)", cid="web-synthetic")
        self.assertEqual(self.dataset()[0], before)

    def test_dataset_and_finetune_print_cut_counts(self):
        self.write_episodes(self.talk("talk", "one", "bad"))
        self.rate("bad")
        for run in (lambda: finetune.cmd_dataset("wisp"), lambda: finetune.cmd_finetune("wisp")):
            with mock.patch.object(finetune, "_prereqs", return_value=[]), redirect_stdout(io.StringIO()) as out:
                run()
            self.assertIn("1 conversations cut, 1 turns dropped", out.getvalue())

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            result = cli.main(["feedback", *args])
        return result, out.getvalue(), err.getvalue()

    def test_cli_table_rows_kept_sent_and_gone(self):
        web_chat.save({"id": "web-abc", "instance": "wisp", "turns": [
            {"role": "user", "content": "question"}, {"role": "assistant", "content": "First line\nSecond line"}]})
        self.rate("First line\nSecond line", reason="bad facts\nplease fix")
        sent = feedback.rate("wisp", "operator", {"kind": "message", "file": "note.md"}, "up", "useful", "", "A note")
        feedback.mark_sent(sent["id"], "inbox", "feedback.md")
        (self.sup / "messages.jsonl").write_text(json.dumps({"from": "wisp", "to": "operator", "file": "note.md",
                                                            "ts": "2026-10-29T12:00:00", "text": "A note\nHidden"}) + "\n")
        self.rate("missing", cid="web-gone")
        result, output, error = self.run_cli("wisp")
        self.assertEqual((result, error), (0, ""))
        self.assertIn("When | Instance | Thumb | Sent | Reply | Reason", output)
        self.assertIn("| wisp | 👎 | kept | First line | bad facts please fix", output)
        self.assertIn("| wisp | 👍 | inbox ", output)
        self.assertIn("| A note | useful", output)
        self.assertNotIn("Second line", output)
        self.assertNotIn("Hidden", output)
        self.assertLess(output.index("(gone)"), output.index("A note"))
        self.assertLess(output.index("A note"), output.index("First line"))

    def test_cli_days_instance_filter_and_empty(self):
        with mock.patch.object(feedback, "datetime") as clock:
            clock.now.return_value = datetime.now() - timedelta(days=10)
            self.rate("old", cid="web-old")
        self.rate("recent", reason="recent reason")
        self.rate("other", cid="web-other", name="other", reason="other reason")
        self.assertEqual(self.run_cli()[1].count("| 👎 |"), 3)
        self.assertEqual(self.run_cli("wisp")[1].count("| 👎 |"), 2)
        result, output, _ = self.run_cli("wisp", "--days", "2")
        self.assertEqual(result, 0)
        self.assertEqual(output.count("| 👎 |"), 1)
        self.assertIn("recent reason", output)
        self.assertNotIn("other reason", output)
        self.assertEqual(self.run_cli("--days", "0")[:2], (0, "No feedback.\n"))
        feedback.FILE.unlink()
        self.assertEqual(self.run_cli()[:2], (0, "No feedback.\n"))

    def test_cli_unknown_name(self):
        result, output, error = self.run_cli("missing")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("no instance named 'missing'", error)

    def test_judge_never_sees_feedback(self):
        source_dir = Path(finetune.__file__).parent
        # Include every module directly contributing sections to stats.md, plus judge prompt builders.
        modules = {"metrics", "judge", "pushback", "projects", "metacog", "attention", "commitments",
                   "provenance", "tom", "consultstats", "evolution", "pushes",
                   "surprise", "battery", "trial"}
        for module in modules:
            source = (source_dir / f"{module}.py").read_text()
            with self.subTest(module=module):
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                        self.assertFalse(any("feedback" in n.split(".") for n in names))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn("feedback.jsonl", node.value)
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, {"down_shas", "by_target", "reply_sha"})
