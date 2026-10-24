"""What an exam leaves behind, and what it may reach (Codex review of 151088d, 2026-10-24): a folder per item that is
never reused, a copy written through descriptors, no process of the exam outliving its item, no live-state tool,
links that stay inside the instance kept, and a battery killed mid-exam leaving the instance untouched."""
from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import battery, core

# A stand-in for the instance's own `shaft exam`: it answers at once, leaving behind the process mode.txt asks for.
FAKE_EXAM = '''import json, os, subprocess, sys
root = os.environ["SHAFT_ROOT"]
sys.stdin.read()
mode = open(os.path.join(root, "mode.txt")).read().strip()
if mode == "chain":   # a parent in a session of its own that leaves the copy, its child still working in it
    chain = ("import os, subprocess, time\\n"
             "c = subprocess.Popen(['/bin/sleep', '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\\n"
             "open('chain.pid', 'w').write(str(c.pid))\\nos.chdir('/')\\ntime.sleep(300)\\n")
    p = subprocess.Popen([sys.executable, "-c", chain], cwd=root, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not os.path.exists(os.path.join(root, "chain.pid")):
        pass
    import time; time.sleep(0.3)
    answer = f"{p.pid},{open(os.path.join(root, 'chain.pid')).read()}"
else:
    p = subprocess.Popen(["/bin/sleep", "300"], cwd=root, start_new_session=(mode == "session"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    answer = str(p.pid)
print(json.dumps({"answer": answer, "tools": [], "steps": 1, "ended_by": "submit"}))
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            rel = p.relative_to(root).as_posix()
            out[rel] = f"-> {os.readlink(p)}" if p.is_symlink() else (p.read_bytes() if p.is_file() else "dir")
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-examiso-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "instances" / "x"
        for d in ("shaft", "plugins/a", "plugins/common", "memory", "skills", "notes"):
            (self.root / d).mkdir(parents=True)
        (self.root / "shaft" / "__init__.py").write_text("")
        (self.root / "shaft" / "__main__.py").write_text(FAKE_EXAM)
        (self.root / "plugins" / "common" / "util.py").write_text("U = 1\n")
        (self.root / "plugins" / "a" / "main.py").write_text("A = 1\n")
        (self.root / "memory" / "journal.md").write_text("# j\n")
        (self.root / "config.env").write_text('LLM_MODEL="m"\n')
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("THE OPERATOR'S FILE")
        self.sup = self.base / "sup"
        for p in (mock.patch.object(battery, "SUP_DIR", self.sup), mock.patch.object(battery, "log")):
            p.start()
            self.addCleanup(p.stop)


class ExamFolderTest(Case):
    def test_each_item_gets_a_new_folder_never_reused(self):
        # Codex: every item reused .supervisor/battery/exam/<name>/, so a process left by one exam could wait for the
        # next copy at a path it already knew.
        a = battery.make_exam_copy("x", self.root)
        b = battery.make_exam_copy("x", self.root)
        self.assertNotEqual(a, b)
        for c in (a, b):
            self.assertEqual((c.parent.parent, c.name), (battery.exam_dir("x"), "x"))   # its code reads its name there

    def test_a_destination_folder_swapped_for_a_link_mid_copy_takes_no_write(self):
        # Codex: the copy made its folders and files by path name, so a folder of the copy swapped for a link while
        # it was being filled sent the unsandboxed supervisor's writes outside.
        dest = self.base / "dest"

        def swap(rel: str) -> bool:
            if rel == "plugins/a/main.py" and not (dest / "plugins" / "a").is_symlink():
                shutil.rmtree(dest / "plugins" / "a")
                (dest / "plugins" / "a").symlink_to(self.outside, target_is_directory=True)
            return False
        with self.assertRaises(core.UnsafePath):
            core.copy_beneath(self.root, dest, swap)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])

    def test_a_copy_interrupted_midway_leaves_nothing(self):
        # Codex: the copy was made outside the item's cleanup, with no deadline or interrupt check.
        seen = []

        def check():
            seen.append(1)
            if len(seen) > 3:
                raise battery.Interrupted("the operator opened a conversation")
        with self.assertRaises(battery.Interrupted):
            battery.make_exam_copy("x", self.root, check=check)
        self.assertEqual(list(battery.exam_dir("x").iterdir()) if battery.exam_dir("x").exists() else [], [])
        self.assertEqual(len(seen), 4)                                     # it stopped at once

    def test_links_that_stay_inside_the_instance_are_kept_as_links(self):
        # Codex: the copy dropped every link, so an exam measured different code than the instance runs.
        (self.root / "plugins" / "a" / "util.py").symlink_to("../common/util.py")         # a sibling plugin's file
        (self.root / "skills" / "same.md").symlink_to("../plugins/a/main.py")
        (self.root / "notes" / "out.txt").symlink_to(self.outside / "secret.txt")          # absolute: out
        (self.root / "notes" / "up.txt").symlink_to("../../../outside/secret.txt")         # climbs out
        (self.root / "notes" / "dot").symlink_to(".")
        (self.root / "notes" / "via.txt").symlink_to("dot/../../outside/secret.txt")      # ".." after a link: out
        got = tree(battery.make_exam_copy("x", self.root))
        self.assertEqual(got["plugins/a/util.py"], "-> ../common/util.py")
        self.assertEqual(got["skills/same.md"], "-> ../plugins/a/main.py")
        self.assertEqual(got["notes/dot"], "-> .")
        for rel in ("notes/out.txt", "notes/up.txt", "notes/via.txt"):
            self.assertNotIn(rel, got)

    def test_an_exam_cannot_start_a_trial(self):
        # Codex: `trial` queues work on the live instance, spends its daily allowance and answers in its live inbox.
        self.assertIn("trial", battery.EXAM_OFF)


@unittest.skipUnless(os.path.exists("/usr/bin/sandbox-exec"), "macOS sandbox")
class NoProcessOutlivesItsItemTest(Case):
    """Codex: `_exam` killed the exam's process group only when it interrupted it."""

    def run_item(self, mode: str) -> int:
        (self.root / "mode.txt").write_text(mode)
        item = {"id": "research/q", "category": "research", "prompt": "q?", "key": "k", "max_steps": 1}
        grade = {"correct": "yes", "effectiveness": 9, "unsupported_claims": 0, "reasoning": "ok"}
        t0 = time.monotonic()
        with mock.patch.object(battery, "grade", return_value=grade), mock.patch.object(battery, "secrets_env", dict), \
             mock.patch.object(battery, "LLM_PORTS", []):
            row = battery._one("x", item, "instance", self.root, {"version": "v1"}, None, lambda: None, "g")
        self.assertLess(time.monotonic() - t0, 60)                 # never waits for what the exam left running
        self.assertEqual(row["status"], "scored", row)
        if mode == "chain":
            return row["answer"]
        pid = int(row["answer"])
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        return pid

    def gone(self, pid: int) -> bool:
        for _ in range(50):
            if not alive(pid):
                return True
            time.sleep(0.1)
        return False

    def test_a_child_left_in_the_exams_process_group_is_killed(self):
        self.assertTrue(self.gone(self.run_item("group")))

    def test_a_child_in_its_own_session_working_in_the_copy_is_killed(self):
        self.assertTrue(self.gone(self.run_item("session")))

    def test_a_chain_whose_parent_left_the_copy_is_killed_whole(self):
        """Codex, final review of the integration: kill.end_left_in's orphan rule missed a parent in a session of its
        own that chdir'ed out of the copy (not in the folder) while its child stayed in it (its parent alive)."""
        parent, child = (int(p) for p in self.run_item("chain").split(","))
        for pid in (parent, child):
            self.addCleanup(lambda pid=pid: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertTrue(self.gone(child), "the child working in the copy outlived its item")
        self.assertTrue(self.gone(parent), "its parent, out of the copy, outlived the item")


if __name__ == "__main__":
    unittest.main()
