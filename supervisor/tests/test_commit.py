"""Committing an instance's state on its branch."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from supervisor import mind
from supervisor.core import commit_state
from supervisor.tests.mindfix import IGNORE_MIND, commit_at


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class CommitStateTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-commit-")).resolve()
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "x"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/x", str(self.root))
        mind.init("x", self.project)

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def store_files(self) -> list[str]:
        return sorted(mind.git("x", "ls-tree", "-r", "--name-only", "mind", project=self.project).stdout.split())

    def test_commits_with_empty_state_dirs(self):
        # Regression: `git commit -- notes skills` failed because empty dirs are unknown to git,
        # which crashed the supervisor after a real reflection.
        for d in ("self", "memory", "notes", "skills"):
            (self.root / d).mkdir()
        (self.root / "self" / "self.md").write_text("# Self\n")
        (self.root / "memory" / "journal.md").write_text("j\n")
        (self.root / "skills" / "s.md").write_text("a skill\n")
        self.assertTrue(commit_state("x", "reflect", [], root=self.root, project=self.project))
        files = git(self.root, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, ["skills/s.md"])                       # skills stay on the branch (D2)
        self.assertEqual(self.store_files(), ["memory/journal.md", "self/self.md"])
        self.assertIn("SHAFT/x", git(self.root, "log", "-1", "--format=%an"))
        self.assertIn("SHAFT/x", mind.git("x", "log", "-1", "--format=%an", project=self.project).stdout)
        tip = mind.head("x", self.project)
        self.assertTrue(commit_state("x", "reflect", [], root=self.root, project=self.project))  # nothing new: no-op
        self.assertEqual(mind.head("x", self.project), tip)

    def test_commit_never_stages_mind(self):
        # The mind must never reach the project's git (it can be pushed; an instance may read mail): not with an
        # empty .gitignore, not when it is named among the kept code, not when the branch tracked it before.
        (self.root / ".gitignore").write_text("")
        for rel, text in {"self/self.md": "me\n", "memory/journal.md": "j\n", "notes/n.md": "n\n",
                          "inbox/m.md": "hi\n", "plugins/p/main.py": "def run(): return 1\n",
                          "memory/evil.py": "x = 1\n"}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        self.assertTrue(commit_state("x", "live", ["plugins/p/main.py", "memory/evil.py"], root=self.root,
                                     project=self.project))
        tracked = git(self.root, "ls-files").split()
        self.assertEqual(tracked, ["plugins/p/main.py"])
        self.assertFalse([p for p in git(self.root, "log", "--name-only", "--format=").split()
                          if p.split("/")[0] in ("self", "memory", "notes", "inbox")])
        self.assertEqual(self.store_files(), ["inbox/m.md", "memory/evil.py", "memory/journal.md", "notes/n.md",
                                              "self/self.md"])
        self.assertEqual(mind.git("x", "log", "-1", "--format=%B", project=self.project).stdout,
                         git(self.root, "log", "-1", "--format=%B"))          # the same message on both sides
        (self.root / "memory" / "journal.md").write_text("j\nmore\n")
        self.assertTrue(commit_state("x", "live", [], root=self.root, project=self.project))
        self.assertEqual(git(self.root, "ls-files").split(), ["plugins/p/main.py"])
        self.assertEqual(mind.show("x", "mind", "memory/journal.md", self.project), "j\nmore\n")

    def test_a_deleted_mind_folder_is_deleted_in_the_store_too(self):
        for rel in ("notes/a.md", "memory/journal.md"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x\n")
        self.assertTrue(commit_state("x", "live", [], root=self.root, project=self.project))
        shutil.rmtree(self.root / "notes")
        self.assertTrue(commit_state("x", "live", [], root=self.root, project=self.project))
        self.assertEqual(self.store_files(), ["memory/journal.md"])

    def test_mind_store_refuses_remote(self):
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("j\n")
        self.assertTrue(commit_state("x", "live", [], root=self.root, project=self.project))
        tip = mind.head("x", self.project)
        store = mind.store("x", self.project)
        elsewhere = self.project / "elsewhere.git"
        git(self.project, "init", "-q", "--bare", str(elsewhere))
        # the pre-push hook: a push by hand, with the store's own hooks, fails and sends nothing
        r = subprocess.run(["git", f"--git-dir={store}", "push", str(elsewhere), "mind"], capture_output=True,
                           text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never pushed", r.stderr)
        self.assertEqual(git(elsewhere, "for-each-ref"), "")
        # a remote added to the store: the next commit refuses loudly, commits nothing, and leaves the remote be
        subprocess.run(["git", f"--git-dir={store}", "remote", "add", "origin", str(elsewhere)], check=True)
        (self.root / "memory" / "journal.md").write_text("j\nsecret mail text\n")
        from unittest import mock
        from supervisor import core
        with mock.patch.object(core, "log") as logged:
            self.assertFalse(commit_state("x", "live", [], root=self.root, project=self.project))
        self.assertTrue(any("MIND STORE REFUSED" in c.args[0] for c in logged.call_args_list))
        self.assertEqual(mind.head("x", self.project), tip)
        self.assertIn("origin", subprocess.run(["git", f"--git-dir={store}", "remote"], capture_output=True,
                                               text=True).stdout)
        self.assertEqual(git(elsewhere, "for-each-ref"), "")

    def test_mind_store_ignores_instance_gitignore(self):
        # The instance writes .gitignore and .gitattributes files in its own folder: neither may hide part of its
        # mind from the store or change the bytes stored (a `text` attribute would turn CRLF into LF).
        (self.root / "memory").mkdir()
        (self.root / "memory" / ".gitignore").write_text("*\n")
        (self.root / ".gitignore").write_text(IGNORE_MIND + "self/\n")
        (self.root / "memory" / ".gitattributes").write_text("* text eol=lf\n")
        (self.root / "memory" / "journal.md").write_bytes(b"line one\r\nline two\r\n")
        (self.root / "self").mkdir()
        (self.root / "self" / "self.md").write_text("me\n")
        self.assertTrue(commit_state("x", "live", [], root=self.root, project=self.project))
        self.assertEqual(self.store_files(), ["memory/.gitattributes", "memory/.gitignore", "memory/journal.md",
                                              "self/self.md"])
        stored = mind.git("x", "show", "mind:memory/journal.md", project=self.project, binary=True).stdout
        self.assertEqual(stored, b"line one\r\nline two\r\n")

    def test_no_mind_store_is_an_error_not_a_silent_skip(self):
        shutil.rmtree(mind.store("x", self.project))
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("j\n")
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(commit_state("x", "live", [], root=self.root, project=self.project))
        self.assertIn("shaft mind cutover x", err.getvalue())


class IgnoredFileTest(unittest.TestCase):
    def test_an_ignored_file_among_the_kept_code_does_not_sink_the_commit(self):
        # Regression (2026-10-13): shaft's delegated system plugin passed the tests, but the kept files included
        # plugins/.DS_Store (Finder's, ignored by git): `git add` refused it and the whole commit failed, leaving
        # the plugin staged and every later template update blocked.
        project = Path(tempfile.mkdtemp(prefix="shaft-commit-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        git(project, "init", "-q")
        git(project, "commit", "-q", "--allow-empty", "-m", "main")
        root = project / "instances" / "x"
        git(project, "worktree", "add", "-q", "--orphan", "-b", "instance/x", str(root))
        mind.init("x", project)
        (root / ".gitignore").write_text(".DS_Store\n")
        (root / "plugins" / "system").mkdir(parents=True)
        (root / "plugins" / "system" / "main.py").write_text("def run(): return 'ok'\n")
        (root / "plugins" / ".DS_Store").write_bytes(b"finder")
        kept = ["plugins/.DS_Store", "plugins/system/main.py"]
        self.assertTrue(commit_state("x", "talk", kept, root=root, project=project))
        self.assertIn("plugins/system/main.py", git(root, "show", "--name-only", "--format=", "HEAD"))
        self.assertNotIn(".DS_Store", git(root, "show", "--name-only", "--format=", "HEAD"))

    def test_the_gate_does_not_count_finder_files_as_code(self):
        from supervisor import gate
        root = Path(tempfile.mkdtemp(prefix="shaft-snap-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "plugins").mkdir()
        (root / "plugins" / ".DS_Store").write_bytes(b"finder")
        self.assertEqual(gate.snapshot(root), {})


class ConfigMergeTest(unittest.TestCase):
    def test_instance_settings_survive_a_conflicting_template_change(self):
        # Regression: wren (on Haiku) conflicted with every template change near its model lines.
        from unittest import mock
        from supervisor import __main__ as cli, core
        project = Path(tempfile.mkdtemp(prefix="shaft-cfg-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        git(project, "init", "-q")
        git(project, "commit", "-q", "--allow-empty", "-m", "main")
        tpl = project / "template"
        git(project, "worktree", "add", "-q", "--orphan", "-b", "template", str(tpl))
        (tpl / "config.env").write_text('LLM_BASE_URL="local"\nLLM_MODEL="bonsai"\nLLM_API_KEY=""\n')
        git(tpl, "add", "-A")
        git(tpl, "commit", "-q", "-m", "t1")
        root = project / "instances" / "w"
        git(project, "worktree", "add", "-q", "-b", "instance/w", str(root), "template")
        (root / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="claude-haiku-5-5"\nLLM_API_KEY=""\n')
        git(root, "commit", "-q", "-am", "w uses haiku")
        (tpl / "config.env").write_text('LLM_BASE_URL="local:8780"\nLLM_MODEL="bonsai-mlx"\nLLM_API_KEY=""\n'
                                        'LLM_PRESENCE_PENALTY=15\n')
        git(tpl, "commit", "-q", "-am", "t2")
        r = subprocess.run(["git", "-C", str(root), "merge", "--no-edit", "template"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                                   # it really conflicts
        wt = lambda root_, *a, check=True, project=None: core.git_wt(root_, *a, check=check, project=project_)  # noqa: E731
        project_ = project
        with mock.patch.object(cli, "git_wt", wt):
            self.assertTrue(cli._resolve_config_conflict(root))
        merged = (root / "config.env").read_text()
        self.assertIn('LLM_BASE_URL="broker"', merged)
        self.assertIn('LLM_MODEL="claude-haiku-5-5"', merged)
        self.assertIn("LLM_PRESENCE_PENALTY=15", merged)
        self.assertNotIn("<<<<<<<", merged)


if __name__ == "__main__":
    unittest.main()


class HistoryToolTest(unittest.TestCase):
    """`git` cannot run in an instance's sandbox (its repository lives outside its folder); shaft retried
    `git log` for 15 minutes. The broker's history tool shows its own branch — and only that."""

    def setUp(self):
        from supervisor.broker import Broker
        self.project = Path(tempfile.mkdtemp(prefix="shaft-hist-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main: supervisor secret")
        self.main = git(self.project, "rev-parse", "--short", "HEAD").strip()
        self.others = []
        for name in ("x", "y"):
            root = self.project / "instances" / name
            git(self.project, "worktree", "add", "-q", "--orphan", "-b", f"instance/{name}", str(root))
            (root / ".gitignore").write_text(IGNORE_MIND)
            (root / "plugins").mkdir()
            (root / "plugins" / "tool.py").write_text(f"NAME = {name!r}\n")
            git(root, "add", "-A")
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m",
                            f"{name}: code"], check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2026-10-10T09:00:00",
                                                               "GIT_COMMITTER_DATE": "2026-10-10T09:00:00"})
            (root / "self").mkdir()
            (root / "self" / "self.md").write_text(f"I am {name}\n")
            commit_at(name, self.project, root, "2026-10-10T10:00:00", f"{name}: reflect")
            self.others += [git(root, "rev-parse", "--short", "HEAD").strip(), mind.head(name, self.project)[:10]] \
                if name == "y" else []
        self.broker = Broker("x", self.project, {}, self.project / "broker.log")

    def test_own_log_and_changes(self):
        log = self.broker.call("history", {"path": "self/self.md"})
        self.assertTrue(log["ok"], log)
        self.assertIn("x: reflect", log["result"])
        self.assertIn("[mind]", log["result"])
        commit = log["result"].split()[0]
        self.assertIn("+I am x", self.broker.call("history", {"commit": commit})["result"])

    def test_history_spans_code_and_mind(self):
        log = self.broker.call("history", {})
        self.assertTrue(log["ok"], log)
        lines = log["result"].splitlines()
        self.assertEqual([l.split()[1] for l in lines], ["[mind]", "[code]"])          # newest first, both repos
        self.assertIn("x: reflect", lines[0])
        self.assertIn("x: code", lines[1])
        code, mine = lines[1].split()[0], lines[0].split()[0]
        self.assertIn("+NAME = 'x'", self.broker.call("history", {"commit": code})["result"])
        self.assertIn("+I am x", self.broker.call("history", {"commit": mine})["result"])
        only_code = self.broker.call("history", {"path": "plugins"})["result"]
        self.assertIn("[code]", only_code)
        self.assertNotIn("[mind]", only_code)

    def test_nothing_outside_its_branch(self):
        for args in ({"commit": self.main}, *({"commit": c} for c in self.others), {"path": "../y/self/self.md"},
                     {"commit": "--output=/tmp/pwn"}, {"path": ":(top)../y"}):
            with self.subTest(args=args):
                self.assertFalse(self.broker.call("history", args)["ok"])

    def test_save_as_brings_back_a_truncated_mind_file(self):
        """Regression, 2026-10-16: a bad write cut shaft's notes/ai-misalignment-trend.md from 545,936 B to 2,744 B.
        Its mind store held the whole file, but outside its folder: only the operator could restore it."""
        root = self.project / "instances" / "x"
        (root / "notes").mkdir()
        whole = "".join(f"- 2026-10-{i % 28 + 1:02d} entry {i} — sources\n" for i in range(5000)).encode()
        (root / "notes" / "trend.md").write_bytes(whole)
        commit_at("x", self.project, root, "2026-10-11T10:00:00", "x: watch")
        good = mind.head("x", self.project)[:10]
        (root / "notes" / "trend.md").write_bytes(whole[:1600])
        commit_at("x", self.project, root, "2026-10-11T11:00:00", "x: bad write")
        r = self.broker.call("history", {"commit": good, "path": "notes/trend.md", "save_as": "notes/trend.restored"})
        self.assertTrue(r["ok"], r)
        self.assertIn(f"{len(whole)} bytes", r["result"])
        self.assertEqual((root / "notes" / "trend.restored").read_bytes(), whole)
        self.assertEqual(len((root / "notes" / "trend.md").read_bytes()), 1600)   # the damaged file is left to it

    def test_save_as_brings_back_a_code_file(self):
        code = self.broker.call("history", {"path": "plugins"})["result"].split()[0]
        r = self.broker.call("history", {"commit": code, "path": "plugins/tool.py", "save_as": "plugins/old.py"})
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.project / "instances" / "x" / "plugins" / "old.py").read_text(), "NAME = 'x'\n")

    def test_save_as_never_overwrites_and_stays_in_its_history(self):
        mine = self.broker.call("history", {"path": "self/self.md"})["result"].split()[0]
        root = self.project / "instances" / "x"
        (root / "elsewhere").symlink_to(self.project / "outside")
        refused = [{"commit": mine, "path": "self/self.md", "save_as": "self/self.md"},          # an existing file
                   {"commit": mine, "path": "self/self.md", "save_as": "elsewhere"},             # a link, even dangling
                   {"commit": mine, "path": "self/self.md", "save_as": "../y/self/self.md"},
                   {"commit": mine, "path": "self/self.md", "save_as": "/tmp/pwn"},
                   {"commit": mine, "save_as": "copy.md"},                                       # no path
                   {"commit": mine, "path": "self", "save_as": "copy.md"},                       # a folder
                   {"commit": mine, "path": "self/nope.md", "save_as": "copy.md"},
                   {"commit": self.others[1], "path": "self/self.md", "save_as": "copy.md"},     # y's mind
                   {"commit": self.others[0], "path": "plugins/tool.py", "save_as": "copy.md"},  # y's branch
                   {"commit": self.main, "path": "plugins/tool.py", "save_as": "copy.md"}]
        for args in refused:
            with self.subTest(args=args):
                self.assertFalse(self.broker.call("history", args)["ok"])
        self.assertEqual((root / "self" / "self.md").read_text(), "I am x\n")
        self.assertFalse((root / "copy.md").exists())
        self.assertFalse((self.project / "outside").exists())


class RecordToolTest(unittest.TestCase):
    """shaft asked for the supervisor's record of what it did (to check its own claims) — its own only."""

    def test_own_record_only(self):
        import json
        from unittest import mock
        from supervisor import core
        from supervisor.broker import Broker
        sup = Path(tempfile.mkdtemp(prefix="shaft-rec-"))
        self.addCleanup(shutil.rmtree, sup, True)
        (sup / "judgments").mkdir()
        (sup / "judgments" / "x.jsonl").write_text(json.dumps(
            {"episode_ts": "2026-10-12T20:34:45", "goal": "g", "claimed": "yes", "confidence": 75,
             "criterion_met": "partial", "reasoning": "only half done"}) + "\n")
        (sup / "judgments" / "y.jsonl").write_text(json.dumps({"reasoning": "Y SECRET"}) + "\n")
        (sup / "supervisor.log").write_text("[t] x: start live\n[t] y: start live Y SECRET\n[t] tick: y busy\n")
        log = sup / "broker.log"
        log.write_text("[t] x run {} -> ok\n[t] y fetch_page {Y SECRET} -> ok\n")
        with mock.patch.object(core, "SUP_DIR", sup):
            b = Broker("x", sup, {}, log)
            out = {k: b.call("record", {"kind": k})["result"] for k in ("judgments", "runs", "tools")}
        self.assertIn("only half done", out["judgments"])
        self.assertIn("x: start live", out["runs"])
        self.assertIn("x run", out["tools"])
        self.assertNotIn("SECRET", "".join(out.values()))
        self.assertNotIn("tick: y", out["runs"])


class UpdateCase(unittest.TestCase):
    """An instance worktree x branched from a template, with `shaft update` run against them."""

    def setUp(self):
        from unittest import mock
        from supervisor import __main__ as cli, core
        self.cli = cli
        self.project = Path(tempfile.mkdtemp(prefix="shaft-upd-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.tpl = self.project / "template"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "template", str(self.tpl))
        (self.tpl / "shaft").mkdir()
        (self.tpl / "shaft" / "agent.py").write_text("v = 1\n")
        (self.tpl / ".gitignore").write_text(IGNORE_MIND)
        git(self.tpl, "add", "-A")
        git(self.tpl, "commit", "-q", "-m", "template: first")
        self.root = self.project / "instances" / "x"
        git(self.project, "worktree", "add", "-q", "-b", "instance/x", str(self.root), "template")
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("# x — journal\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "x: created")
        for target, value, to in ((core, "PROJECT", self.project), (core, "INSTANCES", self.project / "instances"),
                                  (core, "SUP_DIR", self.project / ".supervisor")):
            p = mock.patch.object(target, value, to)
            p.start()
            self.addCleanup(p.stop)
        mind.init("x")
        mind.commit("x", "x: created", self.root)
        self.gate = mock.patch.object(cli.gate, "run_tests", return_value=(True, "ok"))
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def template_change(self, *subjects):
        for i, s in enumerate(subjects):
            (self.tpl / "shaft" / "agent.py").write_text(f"v = {s!r}\n")
            git(self.tpl, "commit", "-q", "-am", s)

    def notes(self):
        return (self.root / "memory" / "journal.md").read_text().count("My code was updated")

    def update(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.cli.cmd_update(["x"])
        return code, out.getvalue()


class UpdateNoteTest(UpdateCase):
    """Regression (2026-10-13): `shaft update` merged template changes without a word to the instance, so it
    never learned its own code had changed — shaft's self-model still said "no embeddings, no vector DB" and
    "wakes me every 2 hours" after hybrid search was merged and its wake interval was 10 minutes."""

    def test_an_update_leaves_one_note_naming_the_merged_commits(self):
        self.template_change("template: hybrid embedding search", "template: wake every 10 minutes")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertEqual(self.notes(), 1, journal)
        self.assertIn("template: hybrid embedding search", journal)
        self.assertIn("template: wake every 10 minutes", journal)
        self.assertNotIn("template: first", journal)                  # already in the instance before
        self.assertNotIn("merge template", journal)                   # the merge commit itself is not news
        self.assertEqual(git(self.root, "status", "--porcelain").strip(), "")   # nothing left on the branch
        self.assertIn("template: hybrid embedding search", mind.show("x", "mind", "memory/journal.md"))  # committed
        self.assertTrue(mind.git("x", "log", "-1", "--format=%s").stdout.startswith("x: update"))

    def test_no_note_when_already_up_to_date(self):
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("already up to date", out)
        self.assertEqual(self.notes(), 0)

    def test_no_note_when_the_merge_fails_the_gate(self):
        self.template_change("template: broken change")
        head = git(self.root, "rev-parse", "HEAD")
        self.gate.stop()
        from unittest import mock
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(False, "boom")):
            code, out = self.update()
        self.gate.start()
        self.assertEqual(code, 1, out)
        self.assertIn("rolled back", out)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(self.notes(), 0)


class QueuedUpdateTest(UpdateCase):
    """Regression (2026-10-25): `shaft update` on a busy instance was refused ("update it when it is idle"), and
    each retry lost the race to the next cycle, which starts as soon as the last one ends — wren, shaft and thelos
    all refused the M1 update in turn. A busy instance's update is now queued, and the scheduler runs it once idle."""

    def busy(self):
        from supervisor import runner
        return runner.run_lock("x", wait=False, kind="live")

    def test_a_busy_instance_queues_its_update_instead_of_refusing(self):
        from unittest import mock
        from supervisor import core
        self.template_change("template: surprises")
        head = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(self.cli.autonomy, "scheduler_installed", return_value=True), self.busy():
            code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("update queued", out)
        self.assertNotIn("update it when it is idle", out)
        self.assertIsNotNone(core.update_queued("x"))
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)   # nothing merged while it ran

    def test_without_the_scheduler_nothing_would_run_it_so_it_is_refused(self):
        from unittest import mock
        from supervisor import core
        with mock.patch.object(self.cli.autonomy, "scheduler_installed", return_value=False), self.busy():
            code, out = self.update()
        self.assertEqual(code, 3, out)
        self.assertIn("update it when it is idle", out)
        self.assertIsNone(core.update_queued("x"))

    def test_the_queued_update_runs_once_idle_and_is_then_dequeued(self):
        from supervisor import core
        self.template_change("template: surprises")
        core.set_update_queued("x", True)
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("updated from template", out)
        self.assertIn("template: surprises", (self.root / "memory" / "journal.md").read_text())
        self.assertIsNone(core.update_queued("x"))

    def test_status_shows_a_queued_update_and_still_works_without_one(self):
        # Regression (Codex on 784982a): the flag's text was built before the filter, so `shaft status` raised
        # TypeError on every instance without a queued update.
        import contextlib
        import io
        from supervisor import core
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.cli.cmd_status(["x"]), 0)
        self.assertNotIn("update queued", out.getvalue())
        core.set_update_queued("x", True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.cli.cmd_status(["x"])
        self.assertIn("update queued since", out.getvalue())

    def test_a_queued_update_that_fails_the_gate_is_not_retried_in_a_loop(self):
        from unittest import mock
        from supervisor import core
        self.template_change("template: broken change")
        core.set_update_queued("x", True)
        self.gate.stop()
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(False, "boom")):
            code, out = self.update()
        self.gate.start()
        self.assertEqual(code, 1, out)
        self.assertIsNone(core.update_queued("x"))


class UpdateKeepsUncommittedWorkTest(UpdateCase):
    """Regression (2026-10-13): a merged template that failed the gate was rolled back with `reset --hard`,
    which also threw away every uncommitted edit in the instance's worktree — wren and kant both had an
    uncommitted config.env while updates ran (` M config.env`, WAKE_INTERVAL_MINUTES 120 -> 10)."""

    def setUp(self):
        super().setUp()
        (self.root / "config.env").write_text("WAKE_INTERVAL_MINUTES=120\n")
        (self.root / "skills").mkdir()
        (self.root / "skills" / "todo.md").write_text("- one\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "x: config and skills")
        (self.root / "config.env").write_text("WAKE_INTERVAL_MINUTES=10\n")      # the operator's uncommitted edit

    def failing_update(self, side_effect=None):
        from unittest import mock
        self.gate.stop()
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(False, "boom"),
                               side_effect=side_effect) as run:
            code, out = self.update()
        self.gate.start()
        return code, out, run

    def test_an_uncommitted_config_edit_survives_a_template_change_that_fails_the_gate(self):
        self.template_change("template: broken change")
        code, out, run = self.failing_update()
        self.assertEqual((self.root / "config.env").read_text(), "WAKE_INTERVAL_MINUTES=10\n", out)
        self.assertEqual(code, 1, out)
        self.assertIn("rolled back", out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 1\n")    # the merge never landed
        # committed on its own as the operator's, exactly as a run commits it — and kept by the rollback
        self.assertIn("config.env changed from outside the instance", git(self.root, "log", "-1", "--format=%s"))
        self.assertEqual(git(self.root, "show", "HEAD:config.env"), "WAKE_INTERVAL_MINUTES=10\n")
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=no").strip(), "")

    def test_an_uncommitted_config_edit_does_not_block_an_update(self):
        self.template_change("template: fine change")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'template: fine change'\n")
        self.assertEqual(git(self.root, "show", "HEAD:config.env"), "WAKE_INTERVAL_MINUTES=10\n")

    def test_update_refuses_and_names_other_uncommitted_files(self):
        (self.root / "skills" / "todo.md").write_text("- one\n- two, not committed\n")
        self.template_change("template: fine change")
        before = git(self.root, "rev-parse", "HEAD")
        self.gate.stop()
        from unittest import mock
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(True, "ok")) as run:
            code, out = self.update()
        self.gate.start()
        self.assertEqual(code, 1, out)
        self.assertIn("skills/todo.md", out)
        self.assertIn("uncommitted", out)
        run.assert_not_called()                                    # nothing merged, nothing tested
        self.assertNotEqual(git(self.root, "rev-parse", "HEAD"), before)          # only the config commit
        self.assertIn("config.env changed from outside", git(self.root, "log", "-1", "--format=%s"))
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 1\n")
        self.assertEqual((self.root / "skills" / "todo.md").read_text(), "- one\n- two, not committed\n")

    def test_an_uncommitted_mind_does_not_block_an_update(self):
        # The mind is not tracked on the branch any more: a journal line written since the last run is no reason
        # to refuse an update (it is committed to the mind store with the update's note).
        with (self.root / "memory" / "journal.md").open("a") as f:
            f.write("written after the last commit\n")
        self.template_change("template: fine change")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("written after the last commit", mind.show("x", "mind", "memory/journal.md"))

    def test_untracked_files_do_not_block_an_update(self):
        # An untracked file can be lost by neither the merge (git refuses to overwrite it) nor the rollback.
        git(self.root, "checkout", "--", "config.env")
        (self.root / "scratch.txt").write_text("mine\n")
        self.template_change("template: fine change")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'template: fine change'\n")
        self.assertEqual((self.root / "scratch.txt").read_text(), "mine\n")

    def test_rollback_keeps_a_file_written_while_the_gate_ran(self):
        # The rollback must undo only the merge: an edit made to a merged file during the gate is kept (git
        # refuses `reset --keep`) and the operator is told, rather than silently discarded by `reset --hard`.
        git(self.root, "checkout", "--", "config.env")
        self.template_change("template: broken change")

        def edit_during_gate(*_):
            (self.root / "shaft" / "agent.py").write_text("v = 'edited during the gate'\n")
            return False, "boom"
        code, out, _ = self.failing_update(edit_during_gate)
        self.assertEqual(code, 1, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'edited during the gate'\n")
        self.assertIn("could not roll back", out)
