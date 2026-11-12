"""`shaft new`: it refuses before creating anything when git has no identity, reports one JSON line with --json, and can
leave the instance paused until the operator's first reply. Git is isolated from this Mac's own config."""
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

from supervisor import __main__ as cli
from supervisor import core, mind

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout


class NewInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-new-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project, self.sup = self.tmp / "p", self.tmp / "p" / ".supervisor"
        (self.tmp / "gitconfig").write_text("[user]\n\tuseConfigOnly = true\n")   # no identity is guessed from the host
        (self.tmp / "tpl").mkdir()
        env = {"GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TEMPLATE_DIR": str(self.tmp / "tpl"), **IDENTITY}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.project.mkdir()
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        tpl = self.project / "template"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "template", str(tpl))
        (tpl / "shaft").mkdir()
        (tpl / "shaft" / "agent.py").write_text("v = 1\n")
        (tpl / ".gitignore").write_text("self/\nmemory/\nnotes/\n")
        git(tpl, "add", "-A")
        git(tpl, "commit", "-q", "-m", "template: first")
        for target, value in ((core, "PROJECT"), (core, "INSTANCES"), (core, "SUP_DIR"), (cli, "PROJECT"),
                              (cli, "INSTANCES"), (cli, "SUP_DIR")):
            p = mock.patch.object(target, value, {"PROJECT": self.project, "INSTANCES": self.project / "instances",
                                                  "SUP_DIR": self.sup}[value])
            p.start()
            self.addCleanup(p.stop)

    def run_new(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["new", *args])
        return code, out.getvalue(), err.getvalue()

    def drop_identity(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for k in IDENTITY:
            os.environ.pop(k, None)

    def test_new_without_a_git_identity_refuses_before_touching_anything(self):
        # Bug: the branch commit failed after the worktree existed, leaving a half-made instance.
        self.drop_identity()
        code, out, err = self.run_new("ada")
        self.assertEqual(code, 2)
        self.assertIn("shaft install", err)
        self.assertIn("user.email", err)
        self.assertFalse((self.project / "instances").exists())
        self.assertEqual(git(self.project, "branch", "--list", "instance/ada").strip(), "")
        self.assertEqual(git(self.project, "worktree", "list").count("\n"), 2)   # main and template only
        self.assertFalse(mind.exists("ada"))
        code, out, err = self.run_new("ada", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out.count("\n"), 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("user.name", json.loads(out)["error"])

    def test_new_with_a_repo_local_identity_succeeds(self):
        self.drop_identity()
        git(self.project, "config", "user.name", "x")
        git(self.project, "config", "user.email", "x@x")
        self.assertEqual(self.run_new("ada")[0], 0)

    def test_new_json_reports_name_branch_worktree(self):
        code, out, err = self.run_new("ada", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out), {"ok": True, "name": "ada", "branch": "instance/ada",
                                           "worktree": str(self.project / "instances" / "ada"), "paused": False})
        self.assertTrue((self.project / "instances" / "ada" / "self" / "self.md").exists())
        code, out, _ = self.run_new("ada", "--json")          # a refusal keeps the exit code and says why
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["ok"], False)
        self.assertIn("already exists", json.loads(out)["error"])

    def test_new_without_json_keeps_the_human_line(self):
        code, out, _ = self.run_new("ada")
        self.assertEqual(code, 0)
        self.assertEqual(out, "created ada (branch instance/ada, worktree instances/ada)\n")

    def test_new_paused_creates_the_pause_flag_with_a_reason(self):
        for then in ("resume", "stay"):
            name = f"ada-{then}"
            code, out, _ = self.run_new(name, "--json", "--paused-until-first-reply", then)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["paused"])
            self.assertTrue(core.paused(name))
            marker = self.sup / f"first-reply-{name}"
            data = json.loads(marker.read_text())
            self.assertEqual(data["then"], then)
            self.assertTrue(data["created"])
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertIn("waiting for the operator's first reply",
                          (self.project / "instances" / name / "memory" / "journal.md").read_text())

    def test_new_paused_is_in_force_before_the_worktree_exists(self):
        # Bug class: pausing after creation let a scheduler tick wake the new instance first (it finds one by `shaft/`).
        seen = []
        real = cli.git

        def spy(*a, **k):
            if a[:2] == ("worktree", "add"):
                seen.append((core.paused("ada"), (self.sup / "first-reply-ada").exists()))
            return real(*a, **k)
        with mock.patch.object(cli, "git", spy):
            self.assertEqual(self.run_new("ada", "--paused-until-first-reply", "stay")[0], 0)
        self.assertEqual(seen, [(True, True)])

    def test_failed_creation_leaves_no_pause_flag_or_marker(self):
        git(self.project, "branch", "-m", "template", "gone")        # no template branch: the worktree add fails
        code, out, _ = self.run_new("ada", "--json", "--paused-until-first-reply", "resume")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("template", json.loads(out)["error"])
        self.assertFalse(core.paused("ada"))
        self.assertEqual([p.name for p in self.sup.iterdir()], [])

    def test_failed_creation_keeps_a_flag_it_did_not_make(self):
        self.sup.mkdir()
        (self.sup / "paused-ada").write_text("2026-01-01T00:00:00")
        git(self.project, "branch", "-m", "template", "gone")
        self.assertEqual(self.run_new("ada", "--paused-until-first-reply", "stay")[0], 1)
        self.assertTrue((self.sup / "paused-ada").exists())

    def test_missing_template_with_json_is_a_json_failure_not_a_traceback(self):
        git(self.project, "branch", "-m", "template", "gone")
        code, out, err = self.run_new("ada", "--json")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("template", json.loads(out)["error"])

    def test_unknown_parent_with_json_is_a_usage_refusal(self):
        code, out, err = self.run_new("ada", "--from", "nobody", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assertIn("no instance named 'nobody'", json.loads(out)["error"])

    def test_the_marker_temp_is_private_even_beside_a_loose_file(self):
        self.sup.mkdir()
        (self.sup / "first-reply-ada.tmp").write_text("x")
        (self.sup / "first-reply-ada.tmp").chmod(0o666)
        self.assertEqual(self.run_new("ada", "--paused-until-first-reply", "stay")[0], 0)
        self.assertEqual((self.sup / "first-reply-ada").stat().st_mode & 0o777, 0o600)

    def test_new_without_the_flag_is_not_paused(self):
        self.run_new("ada")
        self.assertFalse(core.paused("ada"))
        self.assertFalse((self.sup / "first-reply-ada").exists())

    def test_new_paused_rejects_an_unknown_choice(self):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            self.run_new("ada", "--paused-until-first-reply", "maybe")
        self.assertEqual(e.exception.code, 2)

    def test_new_reports_a_failed_marker_write_and_cleans_up(self):
        with mock.patch.object(cli, "_write_first_reply", side_effect=OSError("disk full")):
            code, out, _ = self.run_new("ada", "--json", "--paused-until-first-reply", "stay")
        self.assertEqual(code, 1)
        self.assertIn("disk full", json.loads(out)["error"])
        self.assertFalse(core.paused("ada"))      # and nothing half-made was left


if __name__ == "__main__":
    unittest.main()
