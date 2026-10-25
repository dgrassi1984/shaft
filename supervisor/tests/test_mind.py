"""The instance's mind store (supervisor/mind.py, docs/plans/PLAN-mind-out-of-git.md): the past imported exactly, the cutover,
and every reader and creator of a mind going through the store."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, evolution, mind, runner
from supervisor import __main__ as cli
from supervisor.core import MIND_PATHS
from supervisor.tests.mindfix import IGNORE_MIND


def git(cwd, *args, when: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "SHAFT/x", "GIT_AUTHOR_EMAIL": "x@shaft.localhost",
           "GIT_COMMITTER_NAME": "SHAFT/x", "GIT_COMMITTER_EMAIL": "x@shaft.localhost"}
    if when:
        env.update(GIT_AUTHOR_DATE=f"{when} +0200", GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env).stdout


class Project(unittest.TestCase):
    """A project whose instance x has the old layout: its branch tracks its mind (the past to import)."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-mind-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.tpl = self.project / "template"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "template", str(self.tpl))
        self.write(self.tpl, "shaft/agent.py", "v = 1\n")
        git(self.tpl, "add", "-A")
        git(self.tpl, "commit", "-q", "-m", "template: first", when="2026-10-10T08:00:00")
        self.root = self.project / "instances" / "x"
        git(self.project, "worktree", "add", "-q", "-b", "instance/x", str(self.root), "template")

    def write(self, root: Path, rel: str, text: str | bytes) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)
        return p

    def commit(self, msg: str, when: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", msg, when=when)
        return git(self.root, "rev-parse", "HEAD").strip()

    def past(self) -> dict[str, str]:
        """Mind and code commits interleaved, a template merge whose template commit is dated inside the
        branch's own history, a symlink, an executable, CRLF bytes, a folder deleted whole."""
        c = {}
        self.write(self.root, "self/self.md", "I am x.\n")
        self.write(self.root, "memory/journal.md", "# x — journal\n")
        c["created"] = self.commit("x: created from the SHAFT template", "2026-10-10T09:00:00")
        self.write(self.root, "plugins/a.py", "A = 1\n")
        c["code"] = self.commit("x: live 2026-10-10 09:10\n\nCode changes made by the instance (tests passed):\n"
                                "- plugins/a.py", "2026-10-10T09:10:00")
        with (self.root / "memory" / "journal.md").open("a") as f:
            f.write("\n---\n## 2026-10-10 09:20 — a day\n\ntext\n")
        self.write(self.root, "notes/n.md", "a note\n")
        self.write(self.root, "notes/run.sh", "#!/bin/sh\necho hi\n").chmod(0o755)
        self.write(self.root, "memory/crlf.txt", b"one\r\ntwo\r\n")
        (self.root / "memory" / "link.md").symlink_to("journal.md")
        c["mind"] = self.commit("x: reflect 2026-10-10 09:20", "2026-10-10T09:20:00")
        self.write(self.tpl, "shaft/agent.py", "v = 2\n")
        git(self.tpl, "commit", "-q", "-am", "template: v2", when="2026-10-10T09:25:00")
        git(self.root, "merge", "-q", "--no-edit", "-m", "x: merge template (operator update)", "template",
            when="2026-10-10T09:40:00")
        shutil.rmtree(self.root / "notes")
        self.write(self.root, "plugins/b.py", "B = 1\n")
        c["both"] = self.commit("x: live 2026-10-10 09:50", "2026-10-10T09:50:00")
        self.write(self.root, "inbox/20261010-095500-from-y.md", "From: y\n\nhello\n")
        c["inbox"] = self.commit("x: live 2026-10-10 10:00", "2026-10-10T10:00:00")
        return c


class BackfillTest(Project):
    def test_backfill_equivalence(self):
        c = self.past()
        self.assertEqual(mind.backfill("x", self.project), 4)     # created, mind, both, inbox; not code or merge
        self.assertEqual(mind.check_every_commit("x", self.project), [])
        twins = mind.git("x", "rev-list", "--reverse", "mind", project=self.project).stdout.split()
        for key, twin in zip(("created", "mind", "both", "inbox"), twins):
            src = git(self.root, "cat-file", "commit", c[key])
            mine = mind.git("x", "cat-file", "commit", twin, project=self.project).stdout
            strip = lambda raw: [l for l in raw.split("\n") if not l.startswith(("tree ", "parent "))]  # noqa: E731
            self.assertEqual(strip(mine), strip(src), key)          # author, committer, dates and message
            files = mind.files("x", twin, self.project)
            branch = {}
            out = subprocess.run(["git", "-C", str(self.root), "archive", "--format=tar", c[key], "--",
                                  *[p for p in MIND_PATHS if git(self.root, "ls-tree", c[key], "--", p)]],
                                 capture_output=True, check=True).stdout
            branch = mind._tar_files(out)
            self.assertEqual(files, branch, key)                    # byte for byte, modes and links included
        at_mind = mind.files("x", mind.at("x", "2026-10-10T09:45:00", self.project), self.project)
        self.assertEqual(at_mind["memory/crlf.txt"], ("f", b"one\r\ntwo\r\n"))
        self.assertEqual(at_mind["memory/link.md"], ("l", b"journal.md"))
        self.assertEqual(at_mind["notes/run.sh"][0], "x")
        self.assertNotIn("notes/n.md", mind.files("x", mind.at("x", "2026-10-10T09:55:00", self.project),
                                                  self.project))
        self.assertIsNone(mind.at("x", "2026-10-10T08:59:59", self.project))
        self.assertFalse([p for p in mind.git("x", "ls-tree", "-r", "--name-only", "mind", project=self.project)
                          .stdout.split() if p.split("/")[0] not in MIND_PATHS])   # nothing but the mind

    def test_backfill_is_incremental_and_never_writes_into_a_live_store(self):
        self.past()
        self.assertEqual(mind.backfill("x", self.project), 4)
        self.assertEqual(mind.backfill("x", self.project), 0)                       # nothing new
        self.write(self.root, "memory/later.md", "later\n")
        self.commit("x: live 2026-10-10 11:00", "2026-10-10T11:00:00")
        self.assertEqual(mind.backfill("x", self.project), 1)
        self.assertEqual(mind.check_every_commit("x", self.project), [])
        self.write(self.root, "memory/live.md", "after the cutover\n")
        self.assertTrue(mind.commit("x", "x: live", self.root, self.project))
        with self.assertRaisesRegex(mind.MindError, "live store"):
            mind.backfill("x", self.project)

    def test_backfill_refuses_once_the_branch_no_longer_holds_the_mind(self):
        self.past()
        git(self.root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *MIND_PATHS)
        git(self.root, "commit", "-q", "-m", "x: the mind moves to the supervisor's mind store",
            when="2026-10-10T12:00:00")
        with self.assertRaisesRegex(mind.MindError, "no longer holds the mind"):
            mind.backfill("x", self.project)


class CutoverTest(Project):
    """The M3 tooling on a fixture: backfill, both checks, the untrack commit, the mind as it is now."""

    def setUp(self):
        super().setUp()
        self.c = self.past()
        sup = self.project / ".supervisor"
        sup.mkdir()
        (sup / "supervisor.log").write_text("[2026-10-10 09:05:00] x: start live cycle_timeout_s=2400\n"
                                            "[2026-10-10 09:45:00] x: start live cycle_timeout_s=2400\n")
        eps = [{"ts": "2026-10-10T09:10:00", "kind": "live", "intent": {"at": "2026-10-10T09:05:30"}},
               {"ts": "2026-10-10T09:50:00", "kind": "live", "intent": {"at": "2026-10-10T09:45:30"}}]
        self.write(self.root, "memory/episodes/2026-10-10.jsonl", "".join(json.dumps(e) + "\n" for e in eps))
        self.c["episodes"] = self.commit("x: live 2026-10-10 10:10", "2026-10-10T10:10:00")
        self.write(self.root, "memory/journal.md", "# x — journal\n\nwritten after the last commit\n")
        for target, value, to in ((core, "PROJECT", self.project), (core, "INSTANCES", self.project / "instances"),
                                  (core, "SUP_DIR", sup), (runner, "SUP_DIR", sup)):
            p = mock.patch.object(target, value, to)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(runner, "open_conversations", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def test_cutover_untracks_the_mind_and_keeps_its_files(self):
        on_disk = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                   if p.is_file() and not p.is_symlink() and ".git" not in p.parts}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mind.cutover("x"), 0, out.getvalue())
        self.assertIn("2/2 identical", out.getvalue())
        self.assertEqual(git(self.root, "ls-files", "--", *MIND_PATHS), "")             # not tracked any more
        self.assertIn("the mind moves to the supervisor's mind store", git(self.root, "log", "-1", "--format=%s"))
        self.assertEqual({p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                          if p.is_file() and not p.is_symlink() and ".git" not in p.parts}, on_disk)  # all kept
        self.assertIn("written after the last commit", mind.show("x", "mind", "memory/journal.md"))
        # then a run, before the template's .gitignore has reached it: the mind still goes only to the store
        self.write(self.root, "memory/journal.md", "# x — journal\n\nthe first run after\n")
        self.write(self.root, "plugins/c.py", "C = 1\n")
        self.assertTrue(core.commit_state("x", "live", ["plugins/c.py"], root=self.root))
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD").split(), ["plugins/c.py"])
        self.assertEqual(mind.show("x", "mind", "memory/journal.md"), "# x — journal\n\nthe first run after\n")

    def test_cutover_changes_nothing_when_a_check_fails(self):
        head = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(mind, "check_every_commit", return_value=[{"at": "2026-10-10T09:20:00"}]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mind.cutover("x"), 1)
        self.assertIn("not cut over", out.getvalue())
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertNotEqual(git(self.root, "ls-files", "--", "memory"), "")

    def test_cutover_waits_for_an_idle_instance(self):
        head = git(self.root, "rev-parse", "HEAD")
        with runner.run_lock("x", wait=False, kind="live"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mind.cutover("x"), 3)
        self.assertIn("busy", out.getvalue())
        with mock.patch.object(runner, "open_conversations", return_value={"t1": "talk"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mind.cutover("x"), 3)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertFalse(mind.exists("x"))

    def test_cutover_refuses_while_the_api_runs_old_code(self):
        # The API runs conversations in its own process: code from before the mind store would commit the mind
        # back onto the branch right after the cutover.
        from supervisor import version
        with mock.patch.object(version, "service", return_value={"commit": "1e8decc", "stale": True}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mind.cutover("x"), 3)
        self.assertIn("shaft api off && shaft api on", out.getvalue())
        self.assertFalse(mind.exists("x"))

    def test_project_pre_push_hook_refuses_instance_branches(self):
        hook = mind.guard_project(self.project)
        remote = self.project / "remote.git"
        git(self.project, "init", "-q", "--bare", str(remote))
        refused = subprocess.run(["git", "-C", str(self.project), "push", str(remote), "instance/x"],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("never pushed", refused.stderr)
        self.assertEqual(subprocess.run(["git", "-C", str(self.project), "push", "-q", str(remote), "main"],
                                        capture_output=True, text=True).returncode, 0)
        self.assertEqual(git(remote, "for-each-ref", "--format=%(refname)").split(), ["refs/heads/main"])
        # Deleting a remote instance branch is the cleanup D4 asks for: the first hook refused it too, because a
        # delete names refs/heads/instance/* as its remote ref.
        git(self.project, "push", "-q", "--no-verify", str(remote), "instance/x")
        deleted = subprocess.run(["git", "-C", str(self.project), "push", str(remote), "--delete", "instance/x"],
                                 capture_output=True, text=True)
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(git(remote, "for-each-ref", "--format=%(refname)").split(), ["refs/heads/main"])
        hook.write_text("#!/bin/sh\nexit 0\n")                          # someone else's hook is never replaced
        with self.assertRaisesRegex(mind.MindError, "not SHAFT's"):
            mind.guard_project(self.project)


class ReadersTest(unittest.TestCase):
    """drift, status, create, fork and birth read and write the mind through the store."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-mindr-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        tpl = self.project / "template"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "template", str(tpl))
        (tpl / "shaft").mkdir()
        (tpl / "shaft" / "agent.py").write_text("v = 1\n")
        (tpl / "skills").mkdir()
        (tpl / "skills" / "writing-plugins.md").write_text("# a skill\n")
        (tpl / ".gitignore").write_text(IGNORE_MIND)
        git(tpl, "add", "-A")
        git(tpl, "commit", "-q", "-m", "template: first")
        sup = self.project / ".supervisor"
        wt = lambda root, *a, **k: core.git_wt(root, *a, **{**k, "project": self.project})   # noqa: E731
        g = lambda *a, check=True: subprocess.run(["git", "-C", str(self.project), "-c", "user.name=t",  # noqa: E731
                                                   "-c", "user.email=t@t", *a], capture_output=True, text=True,
                                                  check=check)
        for target, value, to in ((core, "PROJECT", self.project), (core, "INSTANCES", self.project / "instances"),
                                  (core, "SUP_DIR", sup), (cli, "INSTANCES", self.project / "instances"),
                                  (cli, "PROJECT", self.project), (cli, "git", g), (cli, "git_wt", wt),
                                  (evolution, "INSTANCES", self.project / "instances"), (evolution, "git", g),
                                  (evolution, "git_wt", wt)):
            p = mock.patch.object(target, value, to)
            p.start()
            self.addCleanup(p.stop)
        self.env = mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def new(self, name: str, source: str | None = None) -> Path:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_new(name, source), 0)
        return self.project / "instances" / name

    def test_create_and_birth_init_mind_store(self):
        root = self.new("a")
        self.assertEqual(sorted(git(root, "ls-files").split()), [".gitignore", "shaft/agent.py",
                                                                 "skills/writing-plugins.md"])
        files = sorted(mind.git("a", "ls-tree", "-r", "--name-only", "mind").stdout.split())
        self.assertEqual(files, ["memory/journal.md", "self/goals.md", "self/self.md"])
        self.assertIn("Created", mind.show("a", "mind", "memory/journal.md"))
        self.assertEqual(mind.git("a", "log", "-1", "--format=%s").stdout.strip(),
                         git(root, "log", "-1", "--format=%s").strip())
        # a fork from a parent whose branch still tracks its mind (before its cutover) inherits none of it
        (root / "memory" / "journal.md").write_text("PARENT'S MEMORY\n")
        git(root, "add", "-f", "memory/journal.md")
        git(root, "commit", "-q", "-m", "a: live (old layout)")
        child = self.new("b", source="a")
        self.assertEqual(git(child, "ls-files", "--", *MIND_PATHS), "")
        self.assertNotIn("PARENT'S MEMORY", mind.show("b", "mind", "memory/journal.md"))
        self.assertEqual(git(child, "ls-tree", "-r", "--name-only", "HEAD", "--", *MIND_PATHS), "")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            shutil.rmtree(child)
            git(self.project, "worktree", "prune")
            git(self.project, "branch", "-D", "instance/b")
            self.assertEqual(cli.cmd_new("b", None), 2)                   # its old mind store is in the way
        self.assertIn("already exists", err.getvalue())
        # birth: two parents, a fresh mind, its own store; nothing of either parent's mind
        self.new("c")
        with mock.patch.object(evolution.gate, "run_tests", return_value=(True, "ok")), \
                mock.patch.object(evolution, "journal_note", core.journal_note):
            born, log = evolution._build_child("a", "c", "d", [], "identical genes")
        self.assertTrue(born, log)
        kid = self.project / "instances" / "d"
        self.assertEqual(git(kid, "ls-tree", "-r", "--name-only", "HEAD", "--", *MIND_PATHS), "")
        self.assertEqual(len(git(kid, "log", "-1", "--format=%P").split()), 2, "the birth lost a parent")
        self.assertIn("born from a and c", mind.show("d", "mind", "memory/journal.md"))
        self.assertNotIn("PARENT'S MEMORY", mind.show("d", "mind", "memory/journal.md"))

    def parent_with_knowledge(self) -> Path:
        """A parent that has lived a little: a people file and facts about its operator, a self it wrote."""
        root = self.new("wren")
        (root / "config.env").write_text('OPERATOR_NAME="the operator"\n')
        (root / "memory" / "people").mkdir(parents=True)
        (root / "memory" / "people" / "the-operator.md").write_text("# You (the operator)\n\n- You created me (wren) "
                                                               "[journal 2026-10-12 17:13].\n")
        (root / "memory" / "facts.md").write_text("# Facts I have learned\n\n- [2026-10-13 05:06] no piped tests\n")
        (root / "memory" / "facts-archive.md").write_text("OLD FACTS\n")
        (root / "self" / "self.md").write_text("I AM WREN\n")
        (root / "memory" / "journal.md").write_text("WREN'S JOURNAL\n")
        mind.commit("wren", "wren: sleep", root)
        (root / "memory" / "facts.md").write_text("UNCOMMITTED, MID-CYCLE\n")   # its worktree; the store is the truth
        return root

    def test_seed_brings_what_the_parent_knows_about_the_operator_and_nothing_of_itself(self):
        self.parent_with_knowledge()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_new("thelos", "wren", seed=True), 0)
        self.assertIn("seeded from wren: memory/people/the-operator.md, memory/facts.md", out.getvalue())
        files = sorted(mind.git("thelos", "ls-tree", "-r", "--name-only", "mind").stdout.split())
        self.assertEqual(files, ["memory/facts.md", "memory/journal.md", "memory/people/the-operator.md",
                                 "self/goals.md", "self/self.md"])
        people = mind.show("thelos", "mind", "memory/people/the-operator.md")
        self.assertTrue(people.startswith("# You (the operator)\n\n> Seeded "), people)   # its heading first, then whose
        self.assertIn("wren's beliefs about the operator, written in wren's voice", people)
        self.assertIn("- You created me (wren) [journal 2026-10-12 17:13].", people)   # the text itself unchanged
        facts = mind.show("thelos", "mind", "memory/facts.md")
        self.assertIn("- [2026-10-13 05:06] no piped tests", facts)
        self.assertNotIn("MID-CYCLE", facts)
        self.assertNotIn("WREN", mind.show("thelos", "mind", "self/self.md"))
        journal = mind.show("thelos", "mind", "memory/journal.md")
        self.assertNotIn("WREN'S JOURNAL", journal)
        self.assertIn("of its memories only what it knows about the operator", journal)
        root = self.project / "instances" / "thelos"
        self.assertEqual(git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", *MIND_PATHS), "")   # none on the branch
        self.assertIn("of its memories only what it knows about the operator", git(root, "log", "-1", "--format=%s"))

    def test_without_seed_a_fork_knows_nothing_of_the_parent_s_operator(self):
        self.parent_with_knowledge()
        child = self.new("kid", source="wren")
        self.assertFalse((child / "memory" / "facts.md").exists())
        self.assertFalse((child / "memory" / "people").exists())
        self.assertIn("code, skills and plugins only, not its memories",
                      mind.show("kid", "mind", "memory/journal.md"))

    def test_seed_refuses_what_it_cannot_do_before_creating_anything(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_new("x", None, seed=True), 2)
        self.assertIn("--seed copies from a parent instance", err.getvalue())
        self.new("plain")                                                    # no OPERATOR_NAME in its config.env
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_new("y", "plain", seed=True), 2)
        self.assertIn("sets no OPERATOR_NAME", err.getvalue())
        (self.project / "instances" / "plain" / "config.env").write_text('OPERATOR_NAME="the operator"\n')
        shutil.rmtree(mind.store("plain"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_new("y", "plain", seed=True), 2)
        self.assertIn("no mind store", err.getvalue())
        self.assertFalse((self.project / "instances" / "x").exists() or (self.project / "instances" / "y").exists())
        self.assertFalse(mind.exists("x") or mind.exists("y"))

    def test_seed_from_a_parent_without_the_files_says_so(self):
        root = self.new("young")
        (root / "config.env").write_text('OPERATOR_NAME="the operator"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_new("z", "young", seed=True), 0)
        self.assertIn("seeded from young: nothing (young has no memory/people/the-operator.md, memory/facts.md)",
                      out.getvalue())
        self.assertIn("none, it had neither file", mind.show("z", "mind", "memory/journal.md"))

    def test_drift_reads_mind_store(self):
        root = self.new("a")
        (root / "self" / "personality.md").write_text("## Core\n- curious\n")
        mind.commit("a", "a: reflect 1", root)
        (root / "self" / "personality.md").write_text("## Core\n- curious\n- patient\n")
        (root / "self" / "self.md").write_text("I am a, changed.\n")
        mind.commit("a", "a: reflect 2", root)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            evolution.drift("a")
        self.assertIn("3 self edits, 2 core-personality changes", out.getvalue())     # created + two reflections
        self.assertEqual(git(root, "log", "--format=%s", "--", "self"), "")             # none of it on the branch

    def test_status_reads_self_model_freshness_from_the_store(self):
        root = self.new("a")
        with mock.patch.object(cli.runner, "open_conversations", return_value={}), \
                mock.patch.object(cli.version, "describe", return_value="-"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_status(["a"])
        self.assertIn("self-model last changed", out.getvalue())
        self.assertNotIn("last changed never", out.getvalue())
        shutil.rmtree(mind.store("a"))
        with mock.patch.object(cli.runner, "open_conversations", return_value={}), \
                mock.patch.object(cli.version, "describe", return_value="-"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_status(["a"])
        self.assertIn("no mind store (shaft mind cutover a)", out.getvalue())
        self.assertTrue(root.exists())


if __name__ == "__main__":
    unittest.main()
