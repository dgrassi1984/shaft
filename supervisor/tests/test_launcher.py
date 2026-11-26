"""Terminal bin/shaft ran CLT Python 3.9 and crashed with SyntaxError."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "shaft checkout"
        self.launcher = self.root / "bin/shaft"
        self.launcher.parent.mkdir(parents=True)
        shutil.copy2(Path(__file__).resolve().parents[2] / "bin/shaft", self.launcher)
        self.path = Path(temp.name) / "path"
        self.env = {"PATH": f"{self.path}:/usr/bin:/bin"}
        self.clt_python = self.fake_python(self.path / "python3", "clt", "3.9.6", 1)

    def fake_python(self, path, marker, version="", probe_status=99):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/bin/bash\n"
            'if [[ "$3" == -c ]]; then\n'
            f"    printf '%s\\n' '{version}'\n"
            f"    exit {probe_status}\n"
            "fi\n"
            f"""printf '%s\\n' '{marker}' "$@"\n"""
        )
        path.chmod(0o755)
        return path

    def run_launcher(self):
        return subprocess.run(
            [self.launcher, "status", "two words"],
            env=self.env, capture_output=True, text=True, timeout=5,
        )

    def assert_launched(self, marker):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, f"{marker}\n-E\n-s\n-m\nsupervisor\nstatus\ntwo words\n")

    def test_newest_executable_runtime_python_wins(self):
        self.env["SHAFT_PYTHON"] = ""
        for patch in (9, 12, 13):
            path = self.root / f".runtime/python/cpython-3.12.{patch}-macos-arm64-none/bin/python3.12"
            self.fake_python(path, f"runtime {patch}")
        path.chmod(0o644)
        self.fake_python(self.path / "python3.12", "path")
        self.assert_launched("runtime 12")

    def test_versioned_python_on_path_wins_without_runtime(self):
        self.fake_python(self.path / "python3.12", "path")
        self.assert_launched("path")

    def test_rejects_clt_python_3_9_before_running_supervisor(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        for detail in (str(self.clt_python), "3.9.6", "SHAFT needs Python 3.12",
                       "SHAFT_PYTHON", "install Python 3.12"):
            self.assertIn(detail, result.stderr)

    def test_explicit_python_wins_over_runtime(self):
        self.fake_python(self.root / ".runtime/python/cpython-3.12.12-macos-arm64-none/bin/python3.12", "runtime")
        override = self.fake_python(self.path / "custom python", "override")
        self.env["SHAFT_PYTHON"] = str(override)
        self.assert_launched("override")
