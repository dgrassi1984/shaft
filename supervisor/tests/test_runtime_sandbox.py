"""The Mac app's runtime (core.RUNTIME, ~/SHAFT/.runtime: uv in bin/, the Python 3.12 that runs SHAFT in python/) is in
the home folder, which every instance profile closes. Without a rule its Python could not start in any sandbox, so
nothing of an app install ran (final review C1). Every profile reads bin/ and python/ — never writes them, never reads
uv's cache/ — and a Homebrew install without the folder keeps its profiles as they were. Run under the real
sandbox-exec, with a temporary home and instance."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, sandbox

SANDBOX = sys.platform == "darwin" and Path(sandbox.SANDBOX_EXEC).exists()
UV_PYTHONS = Path.home() / ".local" / "share" / "uv" / "python"


def standalone_python() -> Path | None:
    """A relocatable CPython (a uv-managed python-build-standalone) to copy, or None: the system and Homebrew
    interpreters are not relocatable."""
    if not UV_PYTHONS.is_dir():
        return None
    for folder in sorted(UV_PYTHONS.glob("cpython-3.*-macos-*")):
        if folder.is_symlink() or not folder.is_dir():
            continue
        for exe in sorted((folder / "bin").glob("python3.[0-9]*")):
            if exe.is_file() and not exe.is_symlink():
                return exe
    return None


class Case(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="shaft-rt-home-")).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.root = self.home / "SHAFT" / "instances" / "ada"
        (self.root / ".tmp").mkdir(parents=True)
        self.runtime = self.home / "SHAFT" / ".runtime"
        for d in ("bin", "python", "cache"):
            (self.runtime / d).mkdir(parents=True)
        (self.runtime / "cache" / "wheel.txt").write_text("uv's cache")
        self.enterContext(mock.patch.object(core, "RUNTIME", self.runtime, create=True))   # create: RED on aa5a7b51

    def profiles(self) -> dict[str, str]:
        return {"agent": sandbox.profile(self.root, self.home, []), "run": sandbox.run_profile(self.root, self.home)}

    def run_in(self, profile: str, argv: list[str]) -> subprocess.CompletedProcess:
        prof = self.root / ".tmp" / "p.sb"
        prof.write_text(profile)
        return subprocess.run([sandbox.SANDBOX_EXEC, "-f", str(prof), *argv], cwd=self.root, capture_output=True,
                              text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(self.root / ".tmp"),
                                                           "TMPDIR": str(self.root / ".tmp")})


class RuleTest(Case):
    def test_the_rule_reads_bin_and_python_only_and_is_absent_without_a_runtime(self):
        for name, prof in self.profiles().items():
            with self.subTest(profile=name):
                self.assertIn(f'(allow file-read* (subpath "{self.runtime / "bin"}"))', prof)
                self.assertIn(f'(allow file-read* (subpath "{self.runtime / "python"}"))', prof)
                self.assertNotIn(str(self.runtime / "cache"), prof)
                self.assertNotIn(f'(allow file-write* (subpath "{self.runtime}', prof)
        # An install without the folder (Homebrew): no rule, the profile as it was.
        shutil.rmtree(self.runtime)
        for name, prof in self.profiles().items():
            with self.subTest(profile=name, runtime="absent"):
                self.assertNotIn(".runtime", prof)


class LinkedLayoutTest(Case):
    """Re-review residual 1 (bug on cb1de77d): the rule granted a recursive read on whatever .runtime/bin or
    .runtime/python resolved to, so a link `.runtime/python -> ~` opened the whole home and `.runtime/bin ->
    .runtime/cache` uv's cache. Only real folders directly inside a real (unlinked) .runtime get a rule; anything else
    gets none, and the scheduler's PATH and pyenv refuse it, saying why."""

    def relink(self, name: str, target: Path) -> None:
        shutil.rmtree(self.runtime / name)
        (self.runtime / name).symlink_to(target, target_is_directory=True)

    def assert_no_rule_for(self, name: str, target: Path) -> None:
        with mock.patch.object(core, "log") as said:
            for kind, prof in self.profiles().items():
                with self.subTest(link=name, target=str(target), profile=kind):
                    self.assertNotIn(f'(allow file-read* (subpath "{target.resolve()}"))', prof)
                    self.assertNotIn(f'(allow file-read* (subpath "{self.runtime / name}"))', prof)
        self.assertTrue(any("is a link" in c.args[0] for c in said.call_args_list), said.call_args_list)

    def test_a_link_out_of_the_runtime_gets_no_read_rule(self):
        outside = Path(tempfile.mkdtemp(prefix="shaft-rt-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        for name, target in (("python", self.home), ("bin", self.runtime / "cache"), ("python", outside)):
            with self.subTest(name=name, target=str(target)):
                self.relink(name, target)
                self.assert_no_rule_for(name, target)
                with self.assertRaisesRegex(core.RuntimeLayoutError, "is a link"):
                    core.runtime_dir(name)
                shutil.rmtree(self.runtime / name, ignore_errors=True)
                (self.runtime / name).unlink(missing_ok=True)
                (self.runtime / name).mkdir()
        # The real layout: its rules as before.
        prof = sandbox.profile(self.root, self.home, [])
        self.assertIn(f'(allow file-read* (subpath "{self.runtime / "python"}"))', prof)

    def test_a_linked_runtime_folder_is_refused_whole(self):
        real = Path(tempfile.mkdtemp(prefix="shaft-rt-real-")).resolve()
        self.addCleanup(shutil.rmtree, real, True)
        for d in ("bin", "python"):
            (real / d).mkdir()
        linked = self.home / "SHAFT" / ".runtime-link"
        linked.symlink_to(real, target_is_directory=True)
        with mock.patch.object(core, "RUNTIME", linked), mock.patch.object(core, "log") as said:
            prof = sandbox.profile(self.root, self.home, [])
            self.assertNotIn(str(real), prof)
            self.assertNotIn(str(linked), prof)
            with self.assertRaisesRegex(core.RuntimeLayoutError, "is a link"):
                core.runtime_dir("python")
        self.assertTrue(said.called)

    def test_pyenv_and_the_service_path_refuse_a_linked_bin_with_the_reason(self):
        from supervisor import autonomy, pyenv
        self.relink("bin", self.runtime / "cache")
        (self.runtime / "cache" / "uv").write_text("#!/bin/sh\n")
        (self.runtime / "cache" / "uv").chmod(0o755)
        with mock.patch("shutil.which", return_value="/opt/homebrew/bin/uv"):
            with self.assertRaisesRegex(RuntimeError, "is a link"):
                pyenv.uv_binary()                      # never a silent fallback to another uv
        with mock.patch.object(autonomy, "log") as said:
            plist = autonomy._plist("scheduler")
        self.assertNotIn(str(self.runtime), plist["EnvironmentVariables"]["PATH"])
        self.assertIn("left off PATH", said.call_args.args[0])


class UvTest(Case):
    """pyenv finds the app's own uv first; a missing uv is said with the fix for the kind of install."""

    def test_the_runtime_uv_is_found_before_path(self):
        from supervisor import pyenv
        uv = self.runtime / "bin" / "uv"
        uv.write_text("#!/bin/sh\n")
        uv.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertEqual(pyenv.uv_binary(), str(uv))
            self.assertIn(str(self.runtime / "bin"), pyenv.path_dirs(self.root, self.root))

    def test_a_missing_uv_names_the_fix_for_each_kind_of_install(self):
        from supervisor import pyenv
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as app:
                pyenv.uv_binary()
            shutil.rmtree(self.runtime)
            with self.assertRaises(RuntimeError) as brew:
                pyenv.uv_binary()
        self.assertIn("run Install again", str(app.exception))
        self.assertNotIn("brew", str(app.exception))
        self.assertNotIn("shaft ", str(app.exception))
        self.assertIn("brew install uv", str(brew.exception))


@unittest.skipUnless(SANDBOX, "needs macOS sandbox-exec")
class ScriptInterpreterTest(Case):
    """An interpreter must read its own files: a script under .runtime/python stands for it everywhere."""

    def setUp(self):
        super().setUp()
        self.exe = self.runtime / "python" / "bin" / "fake-python"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text("#!/bin/sh\necho 42\n")
        self.exe.chmod(0o755)

    def test_without_the_rule_it_cannot_start_with_it_it_runs(self):
        with mock.patch.object(core, "RUNTIME", self.home / "elsewhere", create=True):   # no rule: as on aa5a7b51
            for name, prof in self.profiles().items():
                with self.subTest(profile=name, rule=False):
                    r = self.run_in(prof, [str(self.exe)])
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn("Operation not permitted", r.stderr)
        for name, prof in self.profiles().items():
            with self.subTest(profile=name, rule=True):
                r = self.run_in(prof, [str(self.exe)])
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "42"), r.stderr)

    def test_the_runtime_is_never_written_and_its_cache_never_read(self):
        for name, prof in self.profiles().items():
            with self.subTest(profile=name):
                w = self.run_in(prof, ["/bin/sh", "-c", f"echo x > '{self.runtime / 'python' / 'planted'}'"])
                self.assertNotEqual(w.returncode, 0)
                self.assertFalse((self.runtime / "python" / "planted").exists())
                r = self.run_in(prof, ["/bin/cat", str(self.runtime / "cache" / "wheel.txt")])
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("uv's cache", r.stdout)


@unittest.skipUnless(SANDBOX and standalone_python(),
                     "needs macOS sandbox-exec and a uv-managed standalone CPython to copy (~/.local/share/uv/python)")
class RealPythonTest(Case):
    """A real relocatable CPython placed where the Mac app's install puts it."""

    def setUp(self):
        super().setUp()
        exe = standalone_python()
        dest = self.runtime / "python" / exe.parent.parent.name
        r = subprocess.run(["/bin/cp", "-cR", str(exe.parent.parent), str(dest)], capture_output=True)   # a clone
        if r.returncode:
            shutil.copytree(exe.parent.parent, dest, symlinks=True)
        self.python = dest / "bin" / exe.name

    def code(self, text: str) -> list[str]:
        return [str(self.python), "-I", "-c", text]

    def test_the_runtime_python_starts_only_with_the_rule(self):
        with mock.patch.object(core, "RUNTIME", self.home / "elsewhere", create=True):
            r = self.run_in(sandbox.profile(self.root, self.home, []), self.code("print(6 * 7)"))
        self.assertNotEqual(r.returncode, 0)                             # cannot read its own standard library
        for name, prof in self.profiles().items():
            with self.subTest(profile=name):
                r = self.run_in(prof, self.code("print(6 * 7)"))
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "42"), r.stderr)

    def test_from_inside_it_cannot_write_the_runtime_or_read_the_cache(self):
        probe = ("import sys\n"
                 "for path, mode in ((sys.argv[1], 'w'), (sys.argv[2], 'r')):\n"
                 "    try:\n"
                 "        open(path, mode).close(); print('opened', mode)\n"
                 "    except PermissionError:\n"
                 "        print('denied', mode)\n")
        for name, prof in self.profiles().items():
            with self.subTest(profile=name):
                r = self.run_in(prof, [str(self.python), "-I", "-c", probe,
                                       str(self.python.parent / "planted"), str(self.runtime / "cache" / "wheel.txt")])
                self.assertEqual(r.stdout.split("\n")[:2], ["denied w", "denied r"], r.stderr)
                self.assertFalse((self.python.parent / "planted").exists())


if __name__ == "__main__":
    unittest.main()
