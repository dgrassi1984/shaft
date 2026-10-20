"""M0: stored document comments, message blocks, final-answer parsing and the instance's copy."""
from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, doc_comments as comments, docs


class CommentsTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-comments-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "mind" / "wren"
        (self.root / "notes/docs").mkdir(parents=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        for p in (mock.patch.object(docs, "instance_root", lambda n: self.root),
                  mock.patch.object(core, "instance_root", lambda n: self.root),
                  mock.patch.object(core, "SUP_DIR", self.base / "sup"), mock.patch.object(docs, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.serial = 0

    def pid(self):
        self.serial += 1
        return f"p-{self.serial:016x}"

    def add(self, text="Question", doc="plan.md", anchor=None, sent=True, name="wren"):
        pid = self.pid()
        tid = comments.add_thread(name, pid, doc, anchor, "old-sha", text)
        if sent:
            comments.mark_sent(name, pid, "web-x#4")
        return tid, pid

    def thread(self, tid="c1"):
        return next(t for t in comments.threads("wren") if t["id"] == tid)

    def doc(self, text):
        (self.root / "notes/docs/plan.md").write_text(text)

    def test_cli_lists_all_or_one_doc_and_syncs_sent_posts(self):
        self.add(text="First line\nSecond line", doc="plan.md")
        self.add(text="Private", doc="sub/other.md", sent=False)
        with mock.patch("builtins.print") as out:
            self.assertEqual(docs.command("wren", "comments", None, None, None), 0)
        self.assertEqual([c.args[0] for c in out.call_args_list],
                         ["c1  open  plan.md  First line", "c2  open  sub/other.md  Private"])
        with mock.patch("builtins.print") as out:
            self.assertEqual(docs.command("wren", "comments", "plan.md", None, None), 0)
        out.assert_called_once_with("c1  open  plan.md  First line")
        with mock.patch("builtins.print") as out:
            self.assertEqual(docs.command("wren", "comments", "plan.md", None, None, sync=True), 0)
        out.assert_called_once_with("wren: notes/comments/plan.md rewritten")
        copy = self.root / "notes/comments/plan.md"
        self.assertIn("First line\nSecond line", copy.read_text())
        copy.write_text("stale")
        with mock.patch("builtins.print"):
            self.assertEqual(docs.command("wren", "comments", "plan.md", None, None, sync=True), 0)
        self.assertNotIn("stale", copy.read_text())
        for path, sync in (("../bad.md", False), ("../bad.md", True), (None, True)):
            with mock.patch("builtins.print") as out:
                self.assertEqual(docs.command("wren", "comments", path, None, None, sync=sync), 1)
            self.assertTrue(out.call_args.args[0].startswith("refused: "))

    def test_add_reply_edit_delete_and_sent(self):
        tid, pid = self.add(sent=False)
        comments.edit("wren", pid, "Edited")
        self.assertEqual(comments.get_post("wren", pid)["text"], "Edited")
        reply = self.pid()
        self.assertEqual(comments.reply("wren", reply, tid, "Reply", "operator"), tid)
        comments.mark_sent("wren", pid, "web-x#4")
        comments.mark_sent("wren", pid, "web-other#8")
        self.assertEqual(comments.get_post("wren", pid)["sent"], "web-x#4")
        for op in (lambda: comments.edit("wren", pid, "No"), lambda: comments.delete("wren", pid)):
            with self.assertRaises(comments.Refused):
                op()
        comments.delete("wren", reply)
        self.assertEqual(len(self.thread()["posts"]), 1)
        other, only = self.add(sent=False)
        comments.delete("wren", only)
        self.assertEqual([t["id"] for t in comments.threads("wren")], [tid])
        self.assertEqual(comments.get_post("wren", pid)["thread"], tid)
        with self.assertRaises(comments.Refused):
            comments.get_post("wren", only)

    def test_ids_per_instance_and_deleted_ids_not_reused(self):
        tid, pid = self.add(sent=False)
        comments.delete("wren", pid)
        self.assertEqual(self.add()[0], "c2")
        self.assertEqual(self.add(name="aria")[0], "c1")

    def test_add_and_reply_are_idempotent_by_pid(self):
        tid, pid = self.add()
        before = comments.threads("wren")
        self.assertEqual(comments.add_thread("wren", pid, "bad", {}, "new", "changed"), tid)
        self.assertEqual(comments.threads("wren"), before)
        reply = self.pid()
        comments.reply("wren", reply, tid, "reply", "operator")
        comments.reply("wren", reply, tid, "changed", "operator")
        self.assertEqual([p["text"] for p in self.thread()["posts"]], ["Question", "reply"])

    def test_validation(self):
        anchor = {"exact": "x", "prefix": "", "suffix": "", "start": 0}
        for bad in ("p-1", "p-" + "A" * 16, "p-" + "a" * 16 + "\n", None):
            with self.assertRaises(comments.Refused):
                comments.add_thread("wren", bad, "plan.md", None, "", "ok")
        for bad in ("", "  ", "x" * 20001, None):
            with self.assertRaises(comments.Refused):
                self.add(text=bad)
        for bad in ("../x.md", "/x.md", "x.txt", "a/b/c/d/e.md"):
            with self.assertRaises(comments.Refused):
                self.add(doc=bad)
        for bad in ([], {}, {**anchor, "exact": ""}, {**anchor, "exact": "x" * 2001},
                    {**anchor, "prefix": "x" * 33}, {**anchor, "suffix": "x" * 33},
                    {**anchor, "start": -1}, {**anchor, "start": True}, {**anchor, "start": 1.0}):
            with self.assertRaises(comments.Refused):
                self.add(anchor=bad)
        self.add(anchor={**anchor, "exact": "x" * 2000, "prefix": "x" * 32, "suffix": "x" * 32})
        for bad in ("c0", "c01", "c1\n", "../c1", None):
            with self.assertRaises(comments.Refused):
                comments.set_state("wren", bad, "open")
        with self.assertRaises(comments.Refused):
            comments.set_state("wren", "c1", "closed")
        with self.assertRaises(comments.Refused):
            comments.threads("../escape")

    def test_notices_take_put_back_and_state_idempotency(self):
        self.add()
        comments.set_state("wren", "c1", "resolved")
        comments.set_state("wren", "c1", "resolved")
        taken = comments.take_notices("wren")
        self.assertEqual(taken, ["[The operator resolved c1 on notes/docs/plan.md]"])
        self.assertEqual(comments.take_notices("wren"), [])
        comments.set_state("wren", "c1", "open")
        comments.put_back_notices("wren", taken)
        self.assertEqual(comments.take_notices("wren"), taken + ["[The operator reopened c1 on notes/docs/plan.md]"])

    def test_counts_order_and_interface(self):
        self.add()
        self.add(doc="sub/other.md", sent=False)
        self.add()
        comments.set_state("wren", "c1", "resolved")
        self.assertEqual(comments.open_counts("wren"), {"sub/other.md": 1, "plan.md": 1})
        self.assertEqual([t["id"] for t in comments.threads("wren")], ["c2", "c3", "c1"])
        self.assertEqual([t["id"] for t in comments.threads("wren", "notes/docs/plan.md")], ["c3", "c1"])
        self.assertEqual(set(self.thread()), {"id", "doc", "by", "state", "sha", "anchor", "created",
                                             "posts", "unsectioned"})
        self.assertEqual(comments.open_counts("empty"), {})

    def test_store_mode_atomic_replace_and_dynamic_sup_dir(self):
        self.add()
        path = core.SUP_DIR / "web/comments/wren.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(comments.os, "replace", wraps=os.replace) as replace:
            comments.edit("wren", self.add(sent=False)[1], "changed")
            self.assertEqual(replace.call_count, 2)
        self.assertEqual(json.loads(path.read_text())["next"], 3)
        with mock.patch.object(core, "SUP_DIR", self.base / "other-sup"):
            self.assertEqual(comments.threads("wren"), [])
            self.assertEqual(self.add()[0], "c1")
        self.assertEqual(len(comments.threads("wren")), 2)

    def test_concurrent_adds_do_not_lose_posts(self):
        def add(i):
            return comments.add_thread("wren", f"p-{i:016x}", "plan.md", None, "", str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(add, range(40)))
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual(len(comments.threads("wren")), 40)

    def test_process_writers_share_file_lock(self):
        # fork inherits the temporary SUP_DIR; each child has its own thread-lock registry.
        ctx = multiprocessing.get_context("fork")
        jobs = [ctx.Process(target=comments.add_thread,
                            args=("wren", f"p-{i:016x}", "plan.md", None, "", str(i))) for i in range(12)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(5)
            if job.is_alive():
                job.terminate()
                job.join()
                self.fail("comment writer did not finish")
            self.assertEqual(job.exitcode, 0)
        self.assertEqual(len(comments.threads("wren")), 12)

    def test_failed_rename_preserves_store_and_cleans_temporary_file(self):
        _, pid = self.add(sent=False)
        path = core.SUP_DIR / "web/comments/wren.json"
        before = path.read_bytes()
        with mock.patch.object(comments.os, "replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                comments.edit("wren", pid, "not saved")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["wren.json"])

    def test_instance_opening_inside_fence_is_ignored_and_missing_quote_is_kept(self):
        answer = ('~~~\n### comment on notes/docs/plan.md: "hidden"\nfake\n~~~\n'
                  '### comment on notes/docs/plan.md: "missing"\nreal')
        result = comments.parse_answer("wren", answer, [])
        self.assertEqual(result["opened"], [{"id": "c1", "doc": "plan.md"}])
        self.assertEqual(self.thread()["anchor"]["exact"], "missing")
        self.assertEqual(self.thread()["sha"], "none")
        self.assertEqual(self.thread()["posts"][0]["text"], "real")

    def test_blocks_passage_whole_document_and_reply(self):
        self.doc("selected text")
        anchor = {"exact": "selected text", "prefix": "", "suffix": "", "start": 0}
        tid, pid = self.add(anchor=anchor)
        block = comments.block("wren", tid, pid)
        self.assertIn('[The operator commented on notes/docs/plan.md (c1), on the text "selected text":]', block)
        self.assertIn("> Question", block)
        self.assertNotIn("occurs more than once", block)
        self.assertNotIn("on screen", block)
        whole, post = self.add()
        self.assertIn("on the whole document", comments.block("wren", whole, post))
        comments.parse_answer("wren", "### c1\nAnswer\n```example```", [tid])
        reply = self.pid()
        comments.reply("wren", reply, tid, "Follow up\n```", "operator")
        block = comments.block("wren", tid, reply)
        self.assertIn("[Last answer:]", block)
        self.assertIn("````\nAnswer\n```example```\n````", block)
        self.assertIn("````\nFollow up\n```\n````", block)
        for t, p in ((tid, pid), (whole, post), (tid, reply)):
            self.assertIn(f'under a line "### {t}"', comments.block("wren", t, p))
            self.assertIn('### comment on notes/docs/<doc>.md:', comments.block("wren", t, p))
        with self.assertRaises(comments.Refused):
            comments.block("wren", whole, pid)

    def test_block_ambiguous_and_on_screen_use_current_raw_document(self):
        anchor = {"exact": "aa", "prefix": "before", "suffix": "after", "start": 3}
        tid, pid = self.add(anchor=anchor)
        self.doc("aaa")                         # overlapping occurrences still ambiguous
        block = comments.block("wren", tid, pid)
        self.assertIn('[It occurs more than once; this one is between "before" and "after".]', block)
        # Neighbours that span blocks stay on one line (JSON-escaped), not two more fenced quotes (2026-10-20)
        tid2, pid2 = self.add(anchor={"exact": "aa", "prefix": "end.\nNext", "suffix": "x\ny", "start": 0})
        self.assertIn('between "end.\\nNext" and "x\\ny".]', comments.block("wren", tid2, pid2))
        self.assertNotIn("```", comments.block("wren", tid2, pid2))
        self.doc("**a**a")
        self.assertIn("text as shown on screen", comments.block("wren", tid, pid))

    def test_parser_one_two_none_unknown_and_unsent(self):
        self.add()
        self.add()
        self.add(sent=False)
        result = comments.parse_answer("wren", "### c1\n first \n### c2   \nsecond\n### c3\nunsent\n### c99\nno",
                                       ["c1", "c2", "c99"])
        # c3 was never sent but is in the instance's copy: its answer lands too (bug 2026-10-20: it was dropped).
        self.assertEqual(result, {"answered": [{"id": "c1", "doc": "plan.md"}, {"id": "c2", "doc": "plan.md"},
                                               {"id": "c3", "doc": "plan.md"}],
                                  "opened": [], "unsectioned": []})
        self.assertEqual(self.thread()["posts"][-1]["text"], "first")
        self.assertEqual(self.thread("c3")["posts"][-1]["text"], "unsent")
        result = comments.parse_answer("wren", "No sections", ["c1", "c2"], chat_turn_ref="web-x#7")
        self.assertEqual(len(result["unsectioned"]), 2)
        self.assertEqual(self.thread()["unsectioned"], ["web-x#7"])
        result = comments.parse_answer("wren", "### c1\nOnly one", ["c1"], chat_turn_ref="web-x#9")
        self.assertEqual(result["answered"], [{"id": "c1", "doc": "plan.md"}])

    def test_parser_sections_for_open_threads_sent_or_not(self):
        self.add()
        self.add(sent=False)
        self.add()
        self.add()
        comments.set_state("wren", "c3", "resolved")
        result = comments.parse_answer("wren", "### c1\nanswer\n### c2\nunsent\n### c3\nresolved", [])
        self.assertEqual(result, {"answered": [{"id": "c1", "doc": "plan.md"}, {"id": "c2", "doc": "plan.md"}],
                                  "opened": [], "unsectioned": []})
        self.assertEqual(self.thread()["posts"][-1]["text"], "answer")
        self.assertEqual(self.thread("c2")["posts"][-1]["text"], "unsent")
        self.assertEqual(self.thread("c2")["unsectioned"], [])
        for tid in ("c3", "c4"):
            self.assertEqual(len(self.thread(tid)["posts"]), 1)
            self.assertEqual(self.thread(tid)["unsectioned"], [])

    def test_parser_fences_any_length_and_matching_closers(self):
        for fence in ("```", "`````", "~~~", "~~~~~~"):
            with self.subTest(fence=fence):
                tid, _ = self.add()
                wrong = "~~~" if fence[0] == "`" else "```"
                answer = f"{fence}python\n### {tid}\nfake\n{wrong}\n### {tid}\nfake too\n{fence}\n### {tid}\nreal"
                comments.parse_answer("wren", answer, [tid], chat_turn_ref=f"web-x#{tid}")
                self.assertEqual(self.thread(tid)["posts"][-1]["text"], "real")
        tid, _ = self.add()
        comments.parse_answer("wren", f"````\n### {tid}\nno\n```\n### {tid}\nstill no", [tid])
        self.assertEqual(len(self.thread(tid)["posts"]), 1)

    def test_parser_duplicate_first_wins_and_heading_boundaries(self):
        for boundary in ("# Next", "## Next", "### Next", "  ## Next"):
            tid, _ = self.add()
            answer = f"### {tid}\nfirst\n#### Included\nbody\n{boundary}\nexcluded\n### {tid}\nsecond"
            comments.parse_answer("wren", answer, [tid])
            self.assertEqual(self.thread(tid)["posts"][-1]["text"], "first\n#### Included\nbody")

    def test_parser_heading_must_be_exact(self):
        for heading in (" ### c1", "### c1 extra", "### c1 ###", "###c1", "### c01"):
            tid, _ = self.add()
            comments.parse_answer("wren", heading.replace("c1", tid) + "\ntext", [tid], chat_turn_ref=tid)
            self.assertEqual(len(self.thread(tid)["posts"]), 1)

    def test_parser_opens_instance_thread_and_replay_does_not_reopen(self):
        self.doc("exact")
        answer = '### comment on notes/docs/plan.md: "exact"\nInstance question\n## Next\nnot part'
        result = comments.parse_answer("wren", answer, [])
        self.assertEqual(result["opened"], [{"id": "c1", "doc": "plan.md"}])
        thread = self.thread()
        self.assertNotIn("source", thread)
        self.assertEqual(thread["by"], "wren")
        self.assertEqual(thread["anchor"], {"exact": "exact", "prefix": "", "suffix": "", "start": 0})
        self.assertEqual(thread["posts"][0]["text"], "Instance question")
        self.assertEqual(thread["posts"][0]["turn"], "web-x#5")
        self.assertIn("Instance question", comments.copy_text("wren", "plan.md"))
        comments.set_state("wren", "c1", "resolved")
        self.assertEqual(comments.parse_answer("wren", answer, [])["opened"], [])
        self.assertEqual(len(comments.threads("wren")), 1)
        for bad in ('../bad.md: "x"', 'plan.md: ""'):
            self.assertEqual(comments.parse_answer("wren", '### comment on notes/docs/' + bad + '\ntext', [])
                             ["opened"], [])

    def test_parser_fallback_only_for_single_comment(self):
        self.add()
        self.add()
        comments.parse_answer("wren", "whole answer", ["c1"], started_by="c1")
        self.assertEqual(self.thread()["posts"][-1]["text"], "whole answer")
        comments.parse_answer("wren", "mixed reply", ["c1", "c2"], started_by="c2", chat_turn_ref="web-x#7")
        self.assertEqual(len(self.thread("c2")["posts"]), 1)
        self.assertEqual(self.thread("c2")["unsectioned"], ["web-x#7"])
        comments.parse_answer("wren", "preface\n### c2\nsection", ["c2"], started_by="c2", chat_turn_ref="web-x#9")
        self.assertEqual(self.thread("c2")["posts"][-1]["text"], "section")

    def test_parser_resolved_and_replay_and_no_section_replay(self):
        self.add()
        self.add()
        self.add()
        comments.set_state("wren", "c3", "resolved")
        answer = "### c1\nanswer\n### c3\nresolved"
        comments.parse_answer("wren", answer, ["c1", "c2", "c3"])
        before = comments.threads("wren")
        self.assertEqual(comments.parse_answer("wren", answer, ["c1", "c2", "c3"]),
                         {"answered": [], "opened": [], "unsectioned": []})
        self.assertEqual(comments.threads("wren"), before)
        self.assertEqual(len(self.thread("c3")["posts"]), 1)
        self.assertEqual(self.thread("c3")["unsectioned"], [])
        with self.assertRaises(comments.Refused):
            comments.delete("wren", self.thread()["posts"][-1]["pid"])

    def test_copy_holds_every_open_comment_and_rebuilds_after_overwrite(self):
        # Bug 2026-10-20: unsent comments were left out, so the operator could not leave several and ask for a review.
        tid, _ = self.add(text="Public", doc="sub/plan.md")
        self.add(text="Unsent thread", doc="sub/plan.md", sent=False)
        comments.reply("wren", self.pid(), tid, "Unsent reply", "operator")
        done, _ = self.add(text="Closed and sent", doc="sub/plan.md")
        comments.reply("wren", self.pid(), done, "Left unsent on a resolved thread", "operator")
        comments.set_state("wren", done, "resolved")
        text = comments.copy_text("wren", "sub/plan.md")
        self.assertIn("Public", text)
        self.assertIn("## c2 (open)", text)
        self.assertIn("Unsent thread", text)
        self.assertIn("Unsent reply", text)
        self.assertIn("· not sent as a message", text)
        self.assertIn('under a line "### c<n>"', text)
        self.assertIn("Closed and sent", text)
        self.assertNotIn("Left unsent on a resolved thread", text)
        comments.parse_answer("wren", "### c1\nAnswer", [tid])
        text = comments.copy_text("wren", "sub/plan.md")
        self.assertIn("Answer", text)
        comments.sync_copy("wren", "notes/docs/sub/plan.md")
        path = self.root / "notes/comments/sub/plan.md"
        self.assertEqual(path.read_text(), text)
        path.write_text("instance replaced it")
        other = path.parent / "other.md"
        other.write_text("keep")
        comments.sync_copy("wren", "sub/plan.md")
        self.assertEqual(path.read_text(), text)
        self.assertEqual(other.read_text(), "keep")

    def test_copy_refuses_path_escape_and_links(self):
        self.add()
        for path in ("../x.md", "x.txt"):
            with self.assertRaises(comments.Refused):
                comments.sync_copy("wren", path)
        (self.root / "notes/comments").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(comments.Refused):
            comments.sync_copy("wren", "plan.md")
        (self.root / "notes/comments").unlink()
        (self.root / "notes/comments").mkdir()
        (self.outside / "file.md").write_text("keep")
        (self.root / "notes/comments/plan.md").symlink_to(self.outside / "file.md")
        with self.assertRaises(comments.Refused):
            comments.sync_copy("wren", "plan.md")
        self.assertEqual((self.outside / "file.md").read_text(), "keep")

    def test_copy_refuses_link_swapped_after_check(self):
        self.add(doc="sub/plan.md")
        sub = self.root / "notes/comments/sub"
        sub.mkdir(parents=True)
        real = core.contained

        def check_then_swap(root, *parts):
            path = real(root, *parts)
            if sub.is_dir() and not sub.is_symlink():
                sub.rmdir()
                sub.symlink_to(self.outside, target_is_directory=True)
            return path

        with mock.patch.object(core, "contained", side_effect=check_then_swap):
            with self.assertRaises(comments.Refused):
                comments.sync_copy("wren", "sub/plan.md")
        self.assertEqual(list(self.outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
