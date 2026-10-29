"""The gate's supervisor tests split in two (docs/plans/PLAN-gate-cache.md, M0): the modules that read the instance
under test, run at every gate, and the shared rest, whose result depends only on the commit. The split is read from
the code, and a module that reaches the instance by a route the scan misses fails the shared part, never passes it.

Regression (2026-10-27): a split by grepping for the variable name would have left test_vision.py, which reaches the
instance only through test_invariants.InstanceCase, in the shared part, tested against the template instead.
The variable's name is spelled through gate_suite.UNDER_TEST here: as a literal it would put this module in the
instance part."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supervisor import gate_suite

PROJECT = Path(__file__).resolve().parents[2]
VAR = gate_suite.UNDER_TEST

# test_invariants as the real one reads the instance: at import, through the variable, falling back to the template.
INVARIANTS = (
    "import os, unittest\nfrom pathlib import Path\n"
    f"TARGET = Path(os.environ.get({VAR!r}) or 'template')\n"
    "CONFIG = (TARGET / 'config.env').read_text()\n\n"
    "class InstanceCase(unittest.TestCase):\n    pass\n\n"
    "class Inv(unittest.TestCase):\n    def test_inv(self):\n        pass\n")


def plain(name: str = "P") -> str:
    return f"import unittest\n\nclass {name}(unittest.TestCase):\n    def test_ok(self):\n        pass\n"


class Tree:
    """A scratch project: supervisor/ with the real gate_suite.py and the given test modules, and an instance
    folder holding a config.env."""

    def __init__(self, modules: dict[str, str]):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-gate-suite-"))
        tests = self.root / "supervisor" / "tests"
        tests.mkdir(parents=True)
        (self.root / "supervisor" / "__init__.py").write_text("")
        shutil.copy(PROJECT / "supervisor" / "gate_suite.py", self.root / "supervisor" / "gate_suite.py")
        (tests / "__init__.py").write_text("")
        for name, text in modules.items():
            (tests / name).parent.mkdir(parents=True, exist_ok=True)
            (tests / name).write_text(text)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        (self.instance / "config.env").write_text("OPERATOR_NAME=x\n")

    def tests(self) -> Path:
        return self.root / "supervisor" / "tests"

    def run(self, part: str, under_test: Path | None = None, nonce: str | None = None, workers: int | None = None,
            env: dict | None = None) -> subprocess.CompletedProcess:
        import os
        env = {k: v for k, v in (env or os.environ).items() if k != VAR}
        if under_test:
            env[VAR] = str(under_test)
        extra = (["--nonce-stdin"] if nonce is not None else []) + (["--workers", str(workers)] if workers else [])
        return subprocess.run([sys.executable, "-E", "-s", "-m", "supervisor.gate_suite", part, *extra],
                              cwd=self.root, env=env, input=f"{nonce}\n" if nonce is not None else "",
                              capture_output=True, text=True, timeout=120)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Partition(unittest.TestCase):
    def split(self, modules: dict[str, str]) -> tuple[list[str], list[str]]:
        t = Tree(modules)
        self.addCleanup(t.close)
        inst, shared = gate_suite.partition(t.tests(), t.root)
        return [m.rsplit(".", 1)[1] for m in inst], [m.rsplit(".", 1)[1] for m in shared]

    def test_every_route_to_test_invariants_is_the_instance_part(self):
        inst, shared = self.split({
            "test_invariants.py": INVARIANTS,
            "test_abs_from.py": "from supervisor.tests.test_invariants import InstanceCase\n" + plain(),
            "test_abs_import.py": "import supervisor.tests.test_invariants\n" + plain(),
            "test_pkg_from.py": "from supervisor.tests import test_invariants\n" + plain(),
            "test_rel_from.py": "from .test_invariants import InstanceCase\n" + plain(),
            "test_rel_pkg.py": "from . import test_invariants\n" + plain(),
            "test_names_var.py": f"X = {VAR!r}\n" + plain(),
            "test_unrelated.py": plain(),
            "test_other_import.py": "from supervisor.tests import fakehelper\n" + plain(),
            "fakehelper.py": "Y = 1\n",
        })
        self.assertEqual(sorted(inst), ["test_abs_from", "test_abs_import", "test_invariants", "test_names_var",
                                        "test_pkg_from", "test_rel_from", "test_rel_pkg"])
        self.assertEqual(sorted(shared), ["test_other_import", "test_unrelated"])

    def test_a_helper_that_imports_test_invariants_carries_it_to_its_importers(self):
        inst, shared = self.split({
            "test_invariants.py": INVARIANTS,
            "harness.py": "from supervisor.tests.test_invariants import InstanceCase\n",
            "relay.py": "from .harness import InstanceCase\n",
            "test_via_helpers.py": "from supervisor.tests.relay import InstanceCase\n" + plain(),
            "test_alone.py": plain(),
        })
        self.assertIn("test_via_helpers", inst)
        self.assertEqual(shared, ["test_alone"])

    def test_a_module_that_does_not_parse_runs_at_every_gate(self):
        # Its import error is then reported by every gate, and never sits beside a recorded shared pass.
        inst, shared = self.split({"test_broken.py": "def (:\n", "test_fine.py": plain()})
        self.assertEqual(inst, ["test_broken"])
        self.assertEqual(shared, ["test_fine"])

    def test_only_what_unittest_discover_would_load(self):
        inst, shared = self.split({"test_a.py": plain(), "helper_test.py": plain(), "test-dash.py": plain(),
                                   "test_invariants.py": INVARIANTS})
        self.assertEqual(sorted(inst + shared), ["test_a", "test_invariants"])


class OnThisCommit(unittest.TestCase):
    """The real supervisor/tests."""

    def test_the_instance_part_is_the_modules_that_read_the_instance_test_vision_included_20261027(self):
        # Exactly these: every module here runs at every gate. test_gate.py once joined them by quoting the variable's
        # name in a fixture (2026-10-27, M2), and every gate ran its 40 s of tests again. A module that truly reads the
        # instance belongs on this list; one that only names the variable should spell it via gate_suite.UNDER_TEST.
        inst, _ = gate_suite.partition(PROJECT / "supervisor" / "tests", PROJECT)
        self.assertEqual(inst, [f"supervisor.tests.{m}" for m in ("test_invariants", "test_judge_evidence",
                                                                   "test_vision")])

    def test_the_scan_knows_every_module_discover_asks_for(self):
        split = gate_suite.Split(PROJECT / "supervisor" / "tests", PROJECT)
        asked = gate_suite.discover(split, "")[1]
        self.assertTrue(asked)
        self.assertEqual([n for n in asked if n not in split.modules], [])

    def test_the_two_parts_hold_every_test_discover_finds(self):
        # In a process of its own, without this part's fence or the unreadable folder: counting imports every module.
        out = count(PROJECT)
        self.assertEqual(out["instance"] + out["shared"], out["all"], out)
        self.assertGreater(out["instance"], 0)
        self.assertGreater(out["shared"], 0)


def count(root: Path) -> dict:
    import json
    import os
    env = {k: v for k, v in os.environ.items() if k != VAR}
    out = subprocess.run([sys.executable, "-E", "-s", "-m", "supervisor.gate_suite", "count"], cwd=root, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.splitlines()[-1])


class Run(unittest.TestCase):
    def tree(self, modules: dict[str, str]) -> Tree:
        t = Tree(modules)
        self.addCleanup(t.close)
        return t

    def test_each_part_runs_only_its_modules_and_reports_its_count(self):
        t = self.tree({"test_invariants.py": INVARIANTS, "test_one.py": plain("A"), "test_two.py": plain("B")})
        inst = t.run("instance", t.instance)
        self.assertEqual(inst.returncode, 0, inst.stderr)
        self.assertEqual(gate_suite.report(inst.stdout), {"part": "instance", "tests_run": 1, "ok": True})
        shared = t.run("shared", t.instance)
        self.assertEqual(shared.returncode, 0, shared.stderr)
        self.assertEqual(gate_suite.report(shared.stdout), {"part": "shared", "tests_run": 2, "ok": True})

    def test_a_guarded_read_under_a_name_the_scan_cannot_see_fails_the_shared_part(self):
        # Codex review of M0: with a folder that does not exist, `if TARGET.exists()` skipped the check and passed.
        name = '"_".join(["SHAFT", "UNDER", "TEST"])'
        guarded = ("import os, unittest\nfrom pathlib import Path\n"
                   f"TARGET = Path(os.environ[{name}])\n\n"
                   "class G(unittest.TestCase):\n    def test_g(self):\n"
                   "        if (TARGET / 'config.env').exists():\n"
                   "            self.assertTrue((TARGET / 'config.env').read_text())\n")
        skipped = ("import os, unittest\nfrom pathlib import Path\n"
                   f"TARGET = Path(os.environ.get({name}, 'template'))\n\n"
                   "@unittest.skipUnless((TARGET / 'config.env').exists(), 'no instance')\n"
                   "class K(unittest.TestCase):\n    def test_k(self):\n        pass\n")
        for text in (guarded, skipped):
            t = self.tree({"test_guarded.py": text, "test_fine.py": plain()})
            self.assertIn("supervisor.tests.test_guarded", gate_suite.partition(t.tests(), t.root)[1])  # missed
            out = t.run("shared", t.instance)
            self.assertNotEqual(out.returncode, 0, out.stderr)
            self.assertIn("PermissionError", out.stderr)
            self.assertFalse(gate_suite.report(out.stdout)["ok"])

    def test_the_unreadable_folder_is_removed_after_the_run(self):
        probe = ("import os, unittest\n\nclass P(unittest.TestCase):\n    def test_p(self):\n"
                 f"        print('SEEN=' + os.environ[{VAR[:6]!r} + {VAR[6:]!r}])\n")
        t = self.tree({"test_probe.py": probe})
        out = t.run("shared", t.instance)
        self.assertEqual(out.returncode, 0, out.stderr)
        seen = Path(next(l for l in out.stdout.splitlines() if l.startswith("SEEN="))[5:])
        self.assertNotEqual(seen, t.instance)
        self.assertFalse(seen.exists())

    def test_a_helper_that_reads_the_instance_is_fenced_too(self):
        # Codex review of M0: only runnable test modules were fenced, so a helper imported by a computed name passed.
        helper = f"import os\nFOUND = os.environ.get({VAR!r})\n"
        hidden = ("import importlib, unittest\n"
                  "helper = importlib.import_module('supervisor.tests.' + 'helper')\n" + plain("H"))
        t = self.tree({"helper.py": helper, "test_hidden.py": hidden})
        self.assertEqual(gate_suite.partition(t.tests(), t.root), ([], ["supervisor.tests.test_hidden"]))
        out = t.run("shared", t.instance)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("supervisor.tests.helper", out.stderr)
        self.assertFalse(gate_suite.report(out.stdout)["ok"])

    def test_an_exit_at_import_cannot_forge_a_pass(self):
        # Codex review of M0: SystemExit(0) escaped the loader, leaving the module's own GATE_SUITE line as the last.
        forged = ("import sys\n"
                  "print('GATE_SUITE {\"part\": \"shared\", \"tests_run\": 1, \"ok\": true}', flush=True)\n"
                  "sys.exit(0)\n")
        t = self.tree({"test_forged.py": forged, "test_fine.py": plain()})
        out = t.run("shared")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("test_forged", out.stderr)
        self.assertEqual(gate_suite.report(out.stdout), {"part": "shared", "tests_run": 2, "ok": False})

    def test_a_route_the_scan_misses_fails_the_shared_part_and_names_the_module(self):
        sneaky = ("import importlib, unittest\n"
                  "harness = importlib.import_module('supervisor.tests.test_' + 'invariants')\n\n"
                  "class S(harness.InstanceCase):\n    def test_s(self):\n        pass\n")
        t = self.tree({"test_invariants.py": INVARIANTS, "test_sneaky.py": sneaky, "test_fine.py": plain()})
        self.assertIn("supervisor.tests.test_sneaky", gate_suite.partition(t.tests(), t.root)[1])   # scan missed it
        out = t.run("shared", t.instance)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(gate_suite.report(out.stdout)["ok"])
        self.assertIn("test_sneaky", out.stderr)

    def test_an_instance_module_swallowed_by_a_try_still_fails_the_shared_part(self):
        # Its import error caught, the module would pass the shared part testing nothing: an attempt fails the part.
        hidden = ("import importlib, unittest\ntry:\n"
                  "    importlib.import_module('supervisor.tests.test_' + 'invariants')\nexcept Exception:\n"
                  "    pass\n" + plain("H"))
        t = self.tree({"test_invariants.py": INVARIANTS, "test_hidden.py": hidden})
        out = t.run("shared", t.instance)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("supervisor.tests.test_invariants", out.stderr)
        self.assertFalse(gate_suite.report(out.stdout)["ok"])

    def test_a_failing_test_fails_its_part_in_unittest_s_own_words(self):
        bad = "import unittest\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
        t = self.tree({"test_bad.py": bad})
        out = t.run("shared")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("FAIL: test_bad (supervisor.tests.test_bad.Bad", out.stderr)   # what gate.failing_tests reads
        self.assertEqual(gate_suite.report(out.stdout), {"part": "shared", "tests_run": 1, "ok": False})

    def test_an_empty_instance_part_passes(self):
        # A project whose tests read no instance (the gate tests' scratch projects): nothing there to fail.
        t = self.tree({"test_one.py": plain()})
        out = t.run("instance", t.instance)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(gate_suite.report(out.stdout), {"part": "instance", "tests_run": 0, "ok": True})

    def test_tests_in_packages_and_in_the_tests_package_itself_are_run_by_one_part(self):
        # Codex review of M0: package __init__ modules were left out of both parts.
        bad = "import unittest\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
        t = self.tree({"pkg/__init__.py": bad, "pkg/test_child.py": plain("C"), "test_top.py": plain("T"),
                       "test_invariants.py": INVARIANTS})
        (t.tests() / "__init__.py").write_text(plain("InInit"))
        counts = count(t.root)
        self.assertEqual(counts, {"all": 5, "instance": 1, "shared": 4})
        out = t.run("shared", t.instance)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("FAIL: test_bad (supervisor.tests.pkg.Bad", out.stderr)
        self.assertEqual(gate_suite.report(out.stdout)["tests_run"], 4)

    def test_a_package_that_fails_to_import_fails_its_part(self):
        t = self.tree({"pkg/__init__.py": "raise RuntimeError('broken package')\n", "test_top.py": plain()})
        out = t.run("shared")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("broken package", out.stderr)

    def test_a_load_tests_hook_fails_its_part_in_any_form_and_is_never_called(self):
        # Codex review of M0: a shared module's hook could discover the instance part's modules (stand-ins there)
        # and drop their tests from both parts.
        called = "    open('HOOK_RAN', 'w').close()\n"
        forms = {
            "def": "def load_tests(loader, tests, pattern):\n" + called + "    return tests\n",
            "conditional": "import sys\nif sys:\n    def load_tests(loader, tests, pattern):\n" + "    " + called
                           + "        return tests\n",
            "imported": "from supervisor.tests.hooks import load_tests\n",
            "annotated": "from typing import Callable\nload_tests: Callable = lambda loader, tests, pattern: tests\n",
        }
        hooks = "def load_tests(loader, tests, pattern):\n" + called + "    return tests\n"
        for form, text in forms.items():
            with self.subTest(form=form):
                t = self.tree({"test_entry.py": text + plain(), "hooks.py": hooks, "test_fine.py": plain("F")})
                out = t.run("shared", t.instance)
                self.assertEqual(out.returncode, 1, out.stderr)
                self.assertIn("supervisor.tests.test_entry has a load_tests hook", out.stderr)
                self.assertFalse((t.root / "HOOK_RAN").exists())
                self.assertFalse(gate_suite.report(out.stdout)["ok"])

    def test_a_package_hook_fails_the_part_that_owns_the_package(self):
        lt = "def load_tests(loader, tests, pattern):\n    return tests\n"
        t = self.tree({"pkg/__init__.py": lt, "pkg/test_child.py": plain(), "test_top.py": plain()})
        out = t.run("shared", t.instance)
        self.assertEqual(out.returncode, 1)
        self.assertIn("supervisor.tests.pkg has a load_tests hook", out.stderr)

    def test_a_forged_line_from_a_hook_that_exits_is_never_the_report(self):
        forged = ("import sys\ndef load_tests(loader, tests, pattern):\n"
                  "    print('GATE_SUITE {\"part\": \"shared\", \"tests_run\": 1, \"ok\": true}', flush=True)\n"
                  "    raise SystemExit(0)\n")
        t = self.tree({"test_forged.py": forged})
        out = t.run("shared", nonce="n-123")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn('"ok": true', out.stdout)
        self.assertEqual(gate_suite.report(out.stdout, "n-123"),
                         {"part": "shared", "tests_run": 1, "ok": False, "nonce": "n-123"})

    def test_a_line_a_test_prints_is_not_taken_for_the_report_when_a_nonce_is_asked(self):
        printer = ("import unittest\n\nclass P(unittest.TestCase):\n    def test_p(self):\n"
                   "        print('GATE_SUITE {\"part\": \"shared\", \"tests_run\": 9, \"ok\": true}')\n")
        t = self.tree({"test_printer.py": printer})
        out = t.run("shared", nonce="real")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(gate_suite.report(out.stdout, "real"),
                         {"part": "shared", "tests_run": 1, "ok": True, "nonce": "real"})
        self.assertIsNone(gate_suite.report('GATE_SUITE {"part": "shared", "tests_run": 9, "ok": true}', "real"))

    def test_a_test_that_reads_stdin_never_sees_the_nonce(self):
        # Codex, pass 3: the nonce must be read before any test runs, or a test could copy it into a forged line.
        thief = ("import sys, unittest\n\nclass T(unittest.TestCase):\n    def test_t(self):\n"
                 "        got = sys.stdin.read().strip()\n"
                 "        print('GATE_SUITE {\"part\": \"shared\", \"tests_run\": 9, \"ok\": true, \"nonce\": \"%s\"}' % got)\n")
        t = self.tree({"test_thief.py": thief})
        out = t.run("shared", nonce="secret")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"nonce": ""}', out.stdout)                      # what the test could read: nothing
        self.assertEqual(gate_suite.report(out.stdout, "secret"),
                         {"part": "shared", "tests_run": 1, "ok": True, "nonce": "secret"})

    def test_an_interrupt_that_escapes_the_runner_is_a_failed_part_with_its_report(self):
        # Codex, pass 3: unittest re-raises KeyboardInterrupt from a test; main must still write a failed report.
        stop = "import unittest\n\nclass I(unittest.TestCase):\n    def test_i(self):\n        raise KeyboardInterrupt\n"
        t = self.tree({"test_interrupt.py": stop})
        out = t.run("shared", nonce="n")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("counted as failed", out.stderr)
        self.assertEqual(gate_suite.report(out.stdout, "n"), {"part": "shared", "tests_run": 0, "ok": False,
                                                              "nonce": "n"})

    def test_report_takes_only_the_shape_main_writes(self):
        for line in ('true', '{"part": "shared", "tests_run": -1, "ok": true}',
                     '{"part": "shared", "tests_run": 1, "ok": "true"}', '{"part": "shared", "tests_run": true, "ok": true}',
                     '{"part": "both", "tests_run": 1, "ok": true}', '{"part": "shared", "tests_run": 1.0, "ok": true}',
                     'not json'):
            self.assertIsNone(gate_suite.report("GATE_SUITE " + line), line)
        self.assertEqual(gate_suite.report('x\nGATE_SUITE {"part": "instance", "tests_run": 0, "ok": false}\n'),
                         {"part": "instance", "tests_run": 0, "ok": False})

    def test_the_shared_part_refuses_to_run_as_root(self):
        import contextlib
        import io
        from unittest import mock
        out = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), mock.patch.object(gate_suite, "discover") as disc:
            code = gate_suite.main(["shared"])
        self.assertEqual(code, 1)
        disc.assert_not_called()
        self.assertEqual(gate_suite.report(out.getvalue()), {"part": "shared", "tests_run": 0, "ok": False})

    def test_an_unknown_part_is_refused(self):
        out = self.tree({}).run("both")
        self.assertEqual(out.returncode, 2)
        self.assertIsNone(gate_suite.report(out.stdout))


class Workers(unittest.TestCase):
    """A part across several processes (docs/plans/PLAN-gate-5min.md): the same tests, the same verdicts, the same
    guards as one process."""

    def tree(self, modules: dict[str, str]) -> Tree:
        t = Tree(modules)
        self.addCleanup(t.close)
        return t

    def test_every_module_runs_once_and_the_counts_add_up(self):
        t = self.tree({f"test_m{i}.py": plain(f"M{i}") for i in range(6)})
        out = t.run("shared", workers=3)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(gate_suite.report(out.stdout), {"part": "shared", "tests_run": 6, "ok": True})
        times = gate_suite.times(out.stderr)
        ran = [name for name, _ in times["slowest_modules"]]
        self.assertEqual(sorted(ran), sorted(["supervisor.tests", *(f"supervisor.tests.test_m{i}" for i in range(6))]))
        self.assertEqual(len(times["slowest_tests"]), 6)
        self.assertEqual(times["run_twice"], [])

    def test_a_test_case_imported_into_another_module_is_named_as_run_twice(self):
        """test_operator_channel imported test_api's ApiTest: discover ran its tests a second time (2026-10-29)."""
        importer = "import unittest\nfrom supervisor.tests.test_m0 import M0\n"
        t = self.tree({"test_m0.py": plain("M0"), "test_importer.py": importer})
        out = t.run("shared", workers=2)
        self.assertEqual(gate_suite.times(out.stderr)["run_twice"],
                         ["test_ok (supervisor.tests.test_m0.M0.test_ok)"])

    def test_a_failure_in_one_worker_fails_the_part_and_is_named(self):
        bad = "import unittest\n\nclass B(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
        t = self.tree({"test_bad.py": bad, **{f"test_m{i}.py": plain(f"M{i}") for i in range(3)}})
        out = t.run("shared", workers=2, nonce="n")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(gate_suite.report(out.stdout, "n"), {"part": "shared", "tests_run": 4, "ok": False, "nonce": "n"})
        self.assertIn("FAIL: test_bad (supervisor.tests.test_bad.B.test_bad)", out.stderr)

    def test_a_worker_that_dies_fails_the_part(self):
        dies = "import os, unittest\n\nclass D(unittest.TestCase):\n    def test_d(self):\n        os._exit(0)\n"
        t = self.tree({"test_dies.py": dies, "test_fine.py": plain()})
        out = t.run("shared", workers=2)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(gate_suite.report(out.stdout)["ok"])
        self.assertIn("test_dies did not finish: a worker died", out.stderr)

    def test_the_fence_holds_in_every_worker(self):
        sneaky = ("import importlib, unittest\n"
                  "harness = importlib.import_module('supervisor.tests.test_' + 'invariants')\n\n"
                  "class S(harness.InstanceCase):\n    def test_s(self):\n        pass\n")
        t = self.tree({"test_invariants.py": INVARIANTS, "test_sneaky.py": sneaky,
                       **{f"test_m{i}.py": plain(f"M{i}") for i in range(4)}})
        out = t.run("shared", t.instance, workers=3)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(gate_suite.report(out.stdout)["ok"])
        self.assertIn("test_sneaky", out.stderr)

    def test_each_worker_has_its_own_supervisor_folder(self):
        """Workers run at once: one folder for their locks and logs would let one test see another's."""
        import os
        shared = tempfile.mkdtemp(prefix="shaft-gate-suite-sup-")
        self.addCleanup(shutil.rmtree, shared, True)
        check = ("import os, unittest\n\nclass W(unittest.TestCase):\n    def test_w(self):\n"
                 f"        self.assertNotEqual(os.environ['SHAFT_SUPERVISOR_DIR'], {shared!r})\n"
                 "        self.assertTrue(os.path.isdir(os.environ['SHAFT_SUPERVISOR_DIR']))\n")
        t = self.tree({"test_w1.py": check, "test_w2.py": check.replace("W(", "W2(")})
        out = t.run("shared", workers=2, env={**os.environ, "SHAFT_SUPERVISOR_DIR": shared})
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_worker_count_below_one_is_refused(self):
        for bad in ("0", "x"):
            with self.subTest(bad=bad):
                out = subprocess.run([sys.executable, "-E", "-s", "-m", "supervisor.gate_suite", "shared", "--workers",
                                      bad], cwd=self.tree({}).root, capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 2)
                self.assertIsNone(gate_suite.report(out.stdout))


if __name__ == "__main__":
    unittest.main()
