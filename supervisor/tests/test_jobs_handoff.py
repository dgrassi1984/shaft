"""Jobs deliver, publish and may run long (docs/plans/PLAN-thelos-review-2.md M1: S1, S6, S8). Measured on the real
record, 2026-10-31: Thelos's `morning-brief` job could not write notes/ (EPERM in the job sandbox); its fallback put a
20 KB brief in its output, which was cut at 4,000 characters with the rest nowhere it was told; instances' briefing
jobs routed around the write limit through the broker's `run`; and a job had 120 s and 30 calls, nothing longer.
Real runs under sandbox-exec, with a real broker, as in test_jobs."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker as broker_mod
from supervisor import jobs
from supervisor.tests.test_jobs import JobCase


class HandoffTest(JobCase):
    def test_a_job_writing_notes_directly_still_gets_eperm(self):
        # The write limit stays (operator, 2026-10-31): a job hands over or publishes; it never writes notes/ itself.
        self.plugin("brief", """
            import errno, pathlib
            def run():
                try:
                    pathlib.Path("notes/x.md").write_text("x")
                    return "WROTE"
                except OSError as e:
                    return errno.errorcode[e.errno]
            """, {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertIn("EPERM", self.inbox()[0])
        self.assertFalse((self.root / "notes" / "x.md").exists())

    def test_returned_files_are_delivered_whole_beside_the_message(self):
        # Bug guarded: the brief survived only in the job's state file, unnamed; the woken cycle never saw it.
        self.plugin("brief", """
            import os
            def run():
                state = os.environ["SHAFT_JOB_STATE"]
                open(f"{state}/brief.md", "w").write("# Brief\\n" + "line of the brief\\n" * 1200)
                open(f"{state}/small.txt", "w").write("small")
                return {"message": "the brief is ready", "files": ["brief.md", f"{state}/small.txt"]}
            """, {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "brief"), 0)
        [message] = self.inbox()
        [md] = sorted((self.root / "inbox").glob("*.md"))
        folder = md.with_suffix(".files")
        brief = (self.root / ".jobs" / "brief" / "brief.md").read_bytes()
        self.assertGreater(len(brief), 20000)
        self.assertEqual((folder / "brief.md").read_bytes(), brief)                 # whole, not cut
        self.assertEqual((folder / "small.txt").read_text(), "small")
        self.assertIn("the brief is ready", message)
        self.assertIn(f"- inbox/{folder.name}/brief.md (21 KB)", message)            # listed with its size
        self.assertIn(f"- inbox/{folder.name}/small.txt (5 bytes)", message)
        self.assertTrue(self.rows()[-1]["delivered"])

    def test_a_file_outside_its_folder_over_the_cap_or_a_link_is_refused_and_named(self):
        state = self.root / ".jobs" / "brief"
        state.mkdir(parents=True)
        (state / "big.bin").write_bytes(b"x" * (jobs.MAX_FILE_BYTES + 1))
        (state / "link.md").symlink_to(self.root / "config.env")
        for i in range(4):
            (state / f"ok{i}.txt").write_text(f"ok {i}")
        (self.root / "notes" / "secret.md").write_text("not the job's")
        files = ["../../notes/secret.md", "/etc/hosts", "big.bin", "link.md", "missing.md",
                 "ok0.txt", "ok1.txt", "ok2.txt", "ok3.txt"]
        self.plugin("brief", f"def run(): return {{'message': 'x', 'files': {files!r}}}", {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "brief"), 0)
        [message] = self.inbox()
        refused = message.split("not delivered:\n")[1]
        self.assertIn("- ../../notes/secret.md: not a path inside my job's own folder .jobs/brief/", refused)
        self.assertIn("- /etc/hosts: not a path inside", refused)
        self.assertIn(f"- big.bin: over {jobs.MAX_FILE_BYTES // 1024} KB", refused)
        self.assertIn("- link.md: a link, or not a plain file", refused)
        self.assertIn("- missing.md: No such file or directory", refused)
        [folder] = (self.root / "inbox").glob("*.files")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["ok0.txt", "ok1.txt", "ok2.txt", "ok3.txt"])
        self.assertNotIn("more than", refused)                           # refused paths do not use up the five
        self.assertNotIn('OPERATOR_NAME', "".join(p.read_text() for p in folder.iterdir()))

    def test_at_most_five_files_are_handed_over_and_the_rest_are_named(self):
        state = self.root / ".jobs" / "brief"
        state.mkdir(parents=True)
        for i in range(jobs.MAX_FILES + 1):
            (state / f"f{i}.txt").write_text(str(i))
        loaded, refused = jobs._handoff(self.root, "brief", [f"f{i}.txt" for i in range(jobs.MAX_FILES + 1)])
        self.assertEqual([n for n, _ in loaded], [f"f{i}.txt" for i in range(jobs.MAX_FILES)])
        self.assertEqual(refused, [f"f{jobs.MAX_FILES}.txt: more than {jobs.MAX_FILES} files in one run"])
        self.assertEqual(jobs._handoff(self.root, "brief", "f0.txt")[1],
                         ["`files` must be a list of paths in .jobs/brief/, not str"])
        self.assertEqual(jobs._handoff(self.root, "brief", ["x"] * 100)[1][-1], "and 80 more paths, not read")

    def test_a_20kb_output_is_cut_with_a_marker_naming_a_file_that_holds_all_of_it(self):
        # Bug guarded: a 20 KB output was cut at 4,000 characters with no pointer to the rest.
        self.plugin("brief", "def run(): return 'head ' + 'b' * 19987 + ' THE END'", {"every_minutes": 10})
        self.assertEqual(jobs.run("wren", "brief"), 0)
        [message] = self.inbox()
        [folder] = (self.root / "inbox").glob("*.files")
        whole = (folder / "output.txt").read_text()
        self.assertEqual(len(whole), 20000)
        self.assertTrue(whole.startswith("head ") and whole.endswith(" THE END"))
        self.assertIn(f"[… cut at {jobs.MAX_OUTPUT} of 20000 characters; the whole text: inbox/{folder.name}/output.txt]",
                      message)
        self.assertNotIn("THE END", message)

    def test_handed_files_of_an_archived_message_are_removed_at_the_jobs_next_delivery(self):
        # Beyond the plan: the template archives only the .md, so each hand-off folder would stay in the inbox (and
        # the mind store) for good.
        state = self.root / ".jobs" / "brief"
        state.mkdir(parents=True)
        (state / "a.txt").write_text("a")
        self.assertTrue(jobs._deliver_job("wren", "brief", "one", "", ["a.txt"]))
        [first] = (self.root / "inbox").glob("*.files")
        other = self.root / "inbox" / "20260101-000000-000000-from-job-other.files"
        other.mkdir()                                                    # another job's: never touched
        self.assertTrue(jobs._deliver_job("wren", "brief", "two", "", ["a.txt"]))
        self.assertTrue(first.is_dir())                                  # its message is still unread: kept
        (self.root / "memory" / "messages").mkdir(parents=True)
        md = first.with_suffix(".md")
        md.rename(self.root / "memory" / "messages" / md.name)             # the cycle read it (live.archive_inbox)
        self.assertTrue(jobs._deliver_job("wren", "brief", "three", "", ["a.txt"]))
        self.assertFalse(first.exists())
        self.assertTrue(other.is_dir())
        self.assertEqual(len(list((self.root / "inbox").glob("*-from-job-brief.files"))), 2)
        self.assertFalse(jobs._deliver_job("wren", "brief", "", "", None))   # nothing to say: nothing delivered


class PublishTest(JobCase):
    BODY = """
        import os, pathlib
        def run():
            state = pathlib.Path(os.environ["SHAFT_JOB_STATE"])
            (state / "briefing.md").write_text((state / "content").read_text())
            if (state / "fail").exists():
                raise RuntimeError("upstream down")
            return ""
        """

    def test_a_declared_publish_copies_onto_notes_after_a_successful_run_and_not_after_a_failed_one(self):
        self.plugin("brief", self.BODY, {"every_minutes": 10, "publishes": {"notes/briefing.md": "briefing.md"}})
        state, target = self.root / ".jobs" / "brief", self.root / "notes" / "briefing.md"
        state.mkdir(parents=True)
        (state / "content").write_text("A")
        self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertEqual(target.read_text(), "A")
        self.assertEqual(self.rows()[-1]["published"], ["notes/briefing.md"])
        target.write_text("the instance's own edit")                     # an unchanged source does not overwrite it
        self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertEqual(target.read_text(), "the instance's own edit")
        self.assertNotIn("published", self.rows()[-1])
        (state / "content").write_text("B")
        (state / "fail").write_text("")
        self.assertEqual(jobs.run("wren", "brief"), 1)                     # failed: its half-done source stays put
        self.assertEqual(target.read_text(), "the instance's own edit")
        (state / "fail").unlink()
        self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertEqual(target.read_text(), "B")
        self.assertEqual(list((self.root / "notes").glob(".*shaft-publish*")), [])   # no temp file left
        self.assertIn("publishes notes/briefing.md", jobs.describe("wren"))
        self.assertIn("published notes/briefing.md", jobs.describe("wren"))

    def test_a_publish_target_outside_notes_is_refused_at_validation(self):
        base = {"every_minutes": 10}
        for target in ("plugins/x/main.py", "notes/../plugins/x.py", "/notes/x.md", "memory/x.md", "self/core.md",
                       "notes", "notes/", "shaft/agent.py", "skills/x.md", "tests/test_x.py"):
            with self.subTest(target=target):
                self.assertIn("is not under notes/", jobs._invalid({**base, "publishes": {target: "x.md"}}))
        for source in ("../../notes/x.md", "/etc/hosts", "", "a/../b"):
            with self.subTest(source=source):
                self.assertIn("its source must be a path inside", jobs._invalid({**base,
                                                                                 "publishes": {"notes/x.md": source}}))
        self.assertIn("publishes must be an object", jobs._invalid({**base, "publishes": ["notes/x.md"]}))
        self.assertIn("at most", jobs._invalid({**base, "publishes": {f"notes/{i}.md": "x" for i in range(6)}}))
        self.assertEqual(jobs._invalid({**base, "publishes": {"notes/docs/briefing.md": "out/briefing.md"}}), "")
        self.plugin("brief", "def run(): return ''", {**base, "publishes": {"plugins/brief/main.py": "x.py"}})
        jobs.tick({}, datetime.now(), lambda args: self.fail("an invalid schedule must not run"), ["wren"], lambda n: "")
        self.assertIn("plugins/brief: schedule not run — publishes target 'plugins/brief/main.py' is not under notes/",
                      self.inbox()[0])

    def test_a_publish_never_follows_a_link_and_the_problem_is_reported_once(self):
        outside = self.project / "outside"
        outside.mkdir()
        (self.root / "notes").rmdir()
        (self.root / "notes").symlink_to(outside, target_is_directory=True)
        state = self.root / ".jobs" / "brief"
        state.mkdir(parents=True)
        (state / "briefing.md").write_text("brief")
        job = {"plugin": "brief", "publishes": {"notes/briefing.md": "briefing.md"}}
        for _ in range(2):
            self.assertEqual(jobs._publish("wren", self.root, job), [])
        self.assertEqual(list(outside.iterdir()), [])
        [message] = self.inbox()
        self.assertIn("did not publish", message)
        self.assertIn("notes/briefing.md: not written", message)


class ConfinedRunTest(JobCase):
    BODY = """
        from shaft import broker
        def run():
            return broker.call("run", command="echo x > notes/from-run.md && echo WROTE")
        """

    def test_with_the_knob_on_run_inside_a_job_cannot_write_notes_and_with_it_off_it_can(self):
        # Bug guarded: instances' briefing jobs reached notes/ through the broker's `run` (RUN_WRITABLE), so the job
        # write limit protected nothing.
        self.plugin("brief", self.BODY, {"every_minutes": 10})
        written = self.root / "notes" / "from-run.md"
        with mock.patch.object(jobs, "RUN_CONFINED", True):
            self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertFalse(written.exists())
        self.assertIn("operation not permitted", self.inbox()[-1].lower())
        with mock.patch.object(jobs, "RUN_CONFINED", False):             # the default this release: unchanged
            self.assertEqual(jobs.run("wren", "brief"), 0)
        self.assertTrue(written.exists())
        self.assertIn("WROTE", self.inbox()[-1])

    def test_the_knob_is_off_by_default_and_takes_only_0_or_1(self):
        env = {k: v for k, v in os.environ.items() if k != "SHAFT_JOB_RUN_CONFINED"}
        env["SHAFT_SUPERVISOR_DIR"] = str(self.project / "settings")
        out = subprocess.run([jobs.PYTHON, "-c", "from supervisor import jobs; print(jobs.RUN_CONFINED)"],
                             capture_output=True, text=True, check=True, env=env)
        self.assertEqual(out.stdout.strip(), "False")
        self.assertIs(jobs._switch("1"), True)
        with self.assertRaises(ValueError):
            jobs._switch("yes")


class RunStreamCutTest(JobCase):
    def test_a_long_run_stream_says_its_length_and_how_to_read_it_all(self):
        # C8 / row 24: keep both streams for the template to save without rerunning the command.
        b = broker_mod.Broker("wren", self.project, {}, self.sup / "broker.log")
        b.egress = mock.Mock(port=0)
        with mock.patch.object(b, "sandbox_env", return_value={}), mock.patch.object(b, "mail_env", return_value=""), \
                mock.patch.object(b, "calendar_env", return_value=""), \
                mock.patch.object(b, "gdrive_env", return_value=""):
            out = b._run("python3 -c \"import sys; print('a' * 10000); print('e' * 7000, file=sys.stderr)\"")
        # Saved by the broker, so an instance on an older shaft/tools.py (6,000-character cut) keeps it too.
        self.assertLessEqual(len(out), broker_mod.RUN_SHOWN_CHARS)
        self.assertIn("[... stdout was 10,001 characters:", out)
        self.assertIn("[... stderr was 7,001 characters:", out)
        saved = re.search(r"saved to (\.tmp/outputs/run-[^\]]+\.txt)\]", out).group(1)
        text = (b.root / saved).read_text()
        self.assertIn("--- stdout ---\n" + "a" * 10000 + "\n", text)
        self.assertIn("--- stderr ---\n" + "e" * 7000 + "\n", text)


class LongJobTest(JobCase):
    BODY = """
        import os, pathlib, time
        def run(**args):
            state = pathlib.Path(os.environ["SHAFT_JOB_STATE"])
            with open(state / "seen.txt", "a") as f:
                f.write(os.environ.get("SHAFT_JOB_RESUME", "-") + " " + os.environ.get("SHAFT_JOB_CHECKPOINT", "-") + "\\n")
            if (state / "checkpointing").exists():
                pathlib.Path(os.environ["SHAFT_JOB_CHECKPOINT"]).write_text('{"done": 1}')
            time.sleep(60)   # stopped by the timeout under test
            return "late"
        """

    def setUp(self):
        super().setUp()
        self.state = self.root / ".jobs" / "long"
        self.state.mkdir(parents=True)
        for p in (mock.patch.object(jobs, "LONG_TIMEOUT_S", 2), mock.patch.object(jobs, "LONG_RESUMES", 1)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_long_job_that_checkpoints_and_times_out_is_resumed_with_resume_set_and_gives_up_after_n(self):
        self.plugin("long", self.BODY, {"every_minutes": 60, "long": True, "args": {"who": "wren"}})
        (self.state / "checkpointing").write_text("")
        job = jobs.declared("wren")[0][0]
        self.assertEqual(jobs.run("wren", "long"), 1)
        self.assertTrue(self.rows()[-1]["resuming"])
        self.assertEqual(self.rows()[-1]["resumed"], 0)
        self.assertTrue(jobs.due("wren", job, datetime.now()))            # at the next tick, not in 60 minutes
        self.assertNotIn("fails", {k for k, v in jobs.entry("wren", "long").items() if v})   # saving its place: no failure
        record = jobs.describe("wren")
        self.assertIn('long: every 60 min · long · ', record)
        self.assertIn("· resuming 1 of 1 · args {\"who\": \"wren\"}", record)
        self.assertEqual(jobs.run("wren", "long"), 1)
        self.assertEqual(self.rows()[-1]["resumed"], 1)
        self.assertTrue(self.rows()[-1]["gave_up"])
        self.assertFalse(jobs.due("wren", job, datetime.now()))           # gave up: its next interval
        self.assertEqual((self.state / "seen.txt").read_text().splitlines(),
                         ["0 .jobs/long/checkpoint.json", "1 .jobs/long/checkpoint.json"])
        [gave_up] = [m for m in self.inbox() if "gave up" in m]
        self.assertIn("My long job long gave up: it was stopped after 2 s 2 time(s) in a row (1 resume(s)", gave_up)
        self.assertIn("· gave up · args", jobs.describe("wren"))

    def test_a_long_job_that_times_out_without_a_new_checkpoint_is_not_resumed(self):
        self.plugin("long", self.BODY, {"every_minutes": 60, "long": True})
        (self.state / "checkpoint.json").write_text("{}")                  # an old one, from before this run
        os.utime(self.state / "checkpoint.json", (0, 0))
        self.assertEqual(jobs.run("wren", "long"), 1)
        self.assertNotIn("resuming", self.rows()[-1])
        self.assertEqual(jobs.entry("wren", "long")["fails"], 1)
        self.assertEqual([m for m in self.inbox() if "gave up" in m], [])

    def test_one_long_job_at_a_time_per_instance(self):
        for name in ("a", "b"):
            self.plugin(name, "def run(): return ''", {"every_minutes": 10, "long": True})
        self.plugin("short", "def run(): return ''", {"every_minutes": 10})
        started, children = [], {}

        def start(args):
            started.append(args[-1])
            return object()
        now = datetime.now()
        jobs.tick(children, now, start, ["wren"], lambda n: "")
        self.assertEqual(started, ["a", "short"])                           # b waits, still due
        jobs._update("wren", "short", last_start=now.isoformat())
        children = {"job wren a": (object(), 0)}                            # a is still going
        jobs.tick(children, now, start, ["wren"], lambda n: "")
        self.assertEqual(started, ["a", "short"])
        jobs.tick({}, now, start, ["wren"], lambda n: "")                   # a ended: b starts at the next tick
        self.assertEqual(started, ["a", "short", "b"])
        with jobs._long_slot("wren") as free:                              # `shaft job run` holds to it as well
            self.assertTrue(free)
            self.assertEqual(jobs.run("wren", "b"), 3)
        self.assertEqual(self.rows()[-1]["skipped"], "another long job of mine is running")

    def test_a_non_long_job_keeps_120_s_and_30_calls_and_a_long_one_gets_the_knobs(self):
        self.plugin("short", "import os\ndef run(): return os.environ.get('SHAFT_JOB_CHECKPOINT', 'none')",
                    {"every_minutes": 10})
        self.plugin("big", "import os\ndef run(): return os.environ['SHAFT_JOB_CHECKPOINT']",
                    {"every_minutes": 10, "long": True})
        seen = []
        real_init, real_communicate = broker_mod.Broker.__init__, subprocess.Popen.communicate

        def init(b, *a, **kw):
            seen.append(("calls", kw.get("max_calls")))
            real_init(b, *a, **kw)

        def communicate(proc, *a, **kw):
            seen.append(("timeout", kw.get("timeout")))
            return real_communicate(proc, *a, **kw)
        with mock.patch.object(broker_mod.Broker, "__init__", init), \
                mock.patch.object(subprocess.Popen, "communicate", communicate), \
                mock.patch.object(jobs, "LONG_TIMEOUT_S", 900):
            self.assertEqual(jobs.run("wren", "short"), 0)
            self.assertEqual(jobs.run("wren", "big"), 0)
        self.assertEqual([s for s in seen if s[0] == "calls"], [("calls", 30), ("calls", jobs.LONG_CALLS)])
        self.assertIn(("timeout", 120), seen)
        self.assertIn(("timeout", 900), seen)
        self.assertEqual(jobs.LONG_CALLS, 200)
        self.assertIn("none", self.inbox()[0])
        self.assertIn(".jobs/big/checkpoint.json", self.inbox()[1])


class LongReplayTest(JobCase):
    def test_a_replay_shows_a_long_jobs_status_from_its_runs(self):
        self.plugin("long", "def run(): return ''", {"every_minutes": 60, "long": True})
        h = jobs.declared("wren")[0][0]["hash"]
        rows = [{"ts": "2026-10-20T09:00:00", "plugin": "long", "seconds": 900, "exit": None, "timed_out": True,
                 "resumed": 0, "resuming": True, "hash": h}]
        e = jobs._entry_then(rows, "long", h)
        self.assertEqual((e["status"], e["fails"]), ("resuming 1 of 4", 0))
        rows.append({**rows[0], "ts": "2026-10-20T09:20:00", "resumed": 1, "resuming": None, "gave_up": True})
        self.assertEqual(jobs._entry_then(rows, "long", h)["status"], "gave up")


if __name__ == "__main__":
    import unittest
    unittest.main()
