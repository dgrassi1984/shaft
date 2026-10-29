"""The gate's supervisor tests in two parts (docs/plans/PLAN-gate-cache.md), run inside the committed tree as
`python -m supervisor.gate_suite instance|shared`:

- instance: the modules that read the instance under test ($SHAFT_UNDER_TEST), run at every gate;
- shared: every other module. Its result depends only on the commit, so it need not run at every gate.

Both parts are unittest discover itself (`-s supervisor/tests -t .`, as the gate ran it): the same walk, the same
load_tests, the same report of a module that fails to import. A part only stops discover importing the other part's
modules, so no test can drop out of both. `count` prints how many tests each part and the whole suite hold, importing
the modules without running them.

The split is read from the code, never kept in a list: a module is in the instance part if it is test_invariants (the
harness that reads the instance), names the variable, does not parse, or imports — directly or through any helper in
supervisor/tests — a module in that part. test_vision reaches the instance only through test_invariants.InstanceCase;
a grep for the variable would have left it in the shared part (2026-10-27). A module or package with a load_tests hook
fails its part, the hook never run: a hook can load any module itself, the other part's included, and the split could
not tell where those tests belong (Codex review of M0; no supervisor test has one).

A route the scan misses fails the shared part, never passes it tested against something else (Codex review of M0):
- the variable names a folder that exists but cannot be read, so a test that looks it up under any name and touches
  anything in it — even a guard like `(TARGET / "config.env").exists()` — raises PermissionError;
- the instance part's modules, helpers included, cannot be imported, and an attempt fails the part even when the test
  caught the ImportError.
The shared part refuses to run as root, who can read the unreadable folder. What is not caught: a test that reads
the instance under a name the scan cannot see AND skips its check when the folder is unreadable (`os.access`). The
supervisor's tests are trusted, committed code no instance can edit; the guards are for an honest mistake, not for a
test written to slip past them.

A part runs in `--workers N` spawned processes (gate.py passes SHAFT_GATE_WORKERS; default 1), one module per task,
the biggest files first (docs/plans/PLAN-gate-5min.md). Each worker loads its module with discover's own walk, every
other module a stand-in, so a module that fails to import or has a hook fails as it did in one process; each worker
installs the fence before importing any test, and has its own SHAFT_SUPERVISOR_DIR. A worker that dies fails the part.

Output is unittest's (stderr, so gate.failing_tests reads it): the failures of each module, a `GATE_SUITE_TIMES`
line with the part's seconds and slowest modules and tests (for `shaft gate budget`, never the verdict), then one last
stdout line for the gate:
`GATE_SUITE {"part": ..., "tests_run": N, "ok": true|false[, "nonce": ...]}`. With `--nonce-stdin` the first line of
stdin is read before any test is imported and repeated in that line, so a test cannot print a line the gate takes for
the real one. Exit 0 when ok, 1 when not, 2 for a wrong argument."""
from __future__ import annotations

import ast
import importlib.abc
import json
import os
import shutil
import sys
import tempfile
import traceback
import types
import unittest
from pathlib import Path

UNDER_TEST = "SHAFT_UNDER_TEST"
HARNESS = "test_invariants"
TESTS = "supervisor/tests"
REPORT = "GATE_SUITE "
PARTS = ("instance", "shared")


def _modules(tests: Path, top: Path) -> dict[str, Path]:
    """{dotted name: file} of every module in `tests` and the packages beneath it, as importable from `top`."""
    found: dict[str, Path] = {}

    def walk(d: Path) -> None:
        for p in sorted(d.iterdir()):
            if p.is_dir() and (p / "__init__.py").is_file():
                walk(p)
            elif p.is_file() and p.suffix == ".py":
                rel = p.relative_to(top).with_suffix("")
                parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
                found[".".join(parts)] = p
    walk(tests)
    return found


def _imports(name: str, path: Path, tree: ast.AST) -> set[str]:
    """Every module name an import statement in `tree` may load (a package and its submodules alike)."""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    out: set[str] = set()

    def add(dotted: str) -> None:
        parts = dotted.split(".")
        out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))   # `import a.b.c` runs a and a.b too

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[:len(base_parts) - (node.level - 1)] if node.level > 1 else base_parts
                base = ".".join(p for p in [*base_parts, node.module or ""] if p)
            else:
                base = node.module or ""
            if base:
                add(base)
                for alias in node.names:                     # `from pkg import mod` imports a submodule
                    add(f"{base}.{alias.name}")
    return out


def _names_the_variable(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and UNDER_TEST in n.value
               for n in ast.walk(tree))


class Split:
    """Which modules of `tests` read the instance under test, helpers included."""

    def __init__(self, tests: Path, top: Path):
        self.tests, self.top = Path(tests).resolve(), Path(top).resolve()
        self.modules = _modules(self.tests, self.top)
        self.reads: set[str] = set()
        edges: dict[str, set[str]] = {}
        for name, path in self.modules.items():
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except (SyntaxError, ValueError, UnicodeDecodeError, OSError):
                self.reads.add(name)              # its import error is then reported by every gate
                continue
            if name.rpartition(".")[2] == HARNESS or _names_the_variable(tree):
                self.reads.add(name)
            edges[name] = _imports(name, path, tree) & self.modules.keys()
        while True:
            more = {n for n, deps in edges.items() if n not in self.reads and deps & self.reads}
            if not more:
                break
            self.reads |= more

    def part_of(self, name: str) -> str:
        """A module discover asks for that the scan never saw is run at every gate."""
        return "shared" if name in self.modules and name not in self.reads else "instance"


class _PartLoader(unittest.TestLoader):
    """discover, importing only the modules of one part (None: all of them), and of those only `only` when it is
    given (a worker's one module). Any other module is an empty stand-in — no tests, no load_tests, its file
    untouched — so discover's walk goes on exactly as it would."""

    def __init__(self, split: Split, part: str | None, only: str | None = None):
        super().__init__()
        self.split, self.part, self.only, self.asked = split, part, only, []

    def _get_module_from_name(self, name):
        self.asked.append(name)
        if (self.part is not None and self.split.part_of(name) != self.part) or (self.only not in (None, name)):
            return types.ModuleType(name)
        return super()._get_module_from_name(name)

    def loadTestsFromModule(self, module, *args, pattern=None, **kws):
        if getattr(module, "load_tests", None) is not None:     # however it got there: def, import, assignment
            return _refused(module.__name__)
        return super().loadTestsFromModule(module, *args, pattern=pattern, **kws)


class HookRefused(unittest.TestCase):
    """A module with a load_tests hook, reported as a failed test named after it; the hook is never called."""


def _refused(name: str) -> unittest.TestSuite:
    def test(self):
        self.fail(f"{name} has a load_tests hook: the gate's split cannot tell which part the tests it loads belong "
                  "to, so it is not run (supervisor/gate_suite.py)")
    short = name.rpartition(".")[2]
    return unittest.TestSuite([type("HookRefused", (HookRefused,), {short: test})(short)])


if not hasattr(unittest.TestLoader, "_get_module_from_name"):   # the hook _PartLoader overrides: without it both
    raise ImportError("unittest.TestLoader has no _get_module_from_name: gate_suite cannot split this Python's "
                      "discover")                                 # parts would import everything (fail closed)


def discover(split: Split, part: str | None, only: str | None = None) -> tuple[unittest.TestSuite, list[str]]:
    """The suite of one part (None: the whole suite; a name no part has: no module imported), or of its module
    `only`, and every module name discover asked for."""
    loader = _PartLoader(split, part, only)
    if str(split.top) not in sys.path:
        sys.path.insert(0, str(split.top))
    suite = loader.discover(str(split.tests), top_level_dir=str(split.top))
    return suite, loader.asked


def partition(tests: Path, top: Path) -> tuple[list[str], list[str]]:
    """(instance part, shared part): the test modules discover would load, by name, without importing any."""
    split = Split(tests, top)
    asked = discover(split, part="")[1]                  # a part no module is in: every module a stand-in
    names = [n for n in dict.fromkeys(asked) if not (n in split.modules and split.modules[n].name == "__init__.py")]
    return ([n for n in names if split.part_of(n) == "instance"], [n for n in names if split.part_of(n) == "shared"])


class _Fence(importlib.abc.MetaPathFinder):
    """In the shared part: the instance part's modules cannot be imported, and every attempt is remembered — a
    test that caught the ImportError would otherwise pass, testing nothing."""

    def __init__(self, blocked: set[str]):
        self.blocked, self.tried = set(blocked), []

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.blocked:
            self.tried.append(fullname)
            raise ImportError(f"{fullname} reads the instance under test: it runs in the instance part, never in "
                              "the shared part")
        return None


def report(stdout: str, nonce: str | None = None) -> dict | None:
    """The gate's line from a run's stdout: the last GATE_SUITE line, or None when there is none, it is not the
    shape main writes, or `nonce` is given and the line does not carry it."""
    line = next((l for l in reversed(stdout.splitlines()) if l.startswith(REPORT)), None)
    if line is None:
        return None
    try:
        r = json.loads(line[len(REPORT):])
    except ValueError:
        return None
    if not (isinstance(r, dict) and r.get("part") in PARTS and type(r.get("tests_run")) is int
            and r["tests_run"] >= 0 and type(r.get("ok")) is bool):
        return None
    if nonce is not None and r.get("nonce") != nonce:
        return None
    return r


def _count(split: Split) -> dict:
    return {"all": discover(split, None)[0].countTestCases(),
            **{part: discover(split, part)[0].countTestCases() for part in PARTS}}


TIMES = "GATE_SUITE_TIMES "   # stderr: the part's seconds and slowest tests, for the budget check (not the gate's verdict)
SLOWEST_KEPT = 15


def _modules_of(split: Split, part: str) -> list[str]:
    """The part's modules discover would load, in its order, none imported: each one a worker's task."""
    asked = discover(split, part="")[1]                  # a part no module is in: every module a stand-in
    return [n for n in dict.fromkeys(asked) if n in split.modules and split.part_of(n) == part]


_worker: dict = {}


def _worker_start(split: Split, part: str, blocked: list[str] | None) -> None:
    """A worker process: its own supervisor folder (tests write locks and logs there, and workers run at once), and
    in the shared part the same fence as one process had, before any test module is imported."""
    os.environ["SHAFT_SUPERVISOR_DIR"] = tempfile.mkdtemp(prefix="shaft-sup-test-")
    fence = None
    if blocked is not None:
        fence = _Fence(set(blocked))
        sys.meta_path.insert(0, fence)
    _worker.update(split=split, part=part, fence=fence)


def _run_module(name: str) -> dict:
    """One module, as discover would load it within its part, run in this worker. Never raises: a test that stops the
    run (KeyboardInterrupt, SystemExit out of a fixture) is the module's failure, its tests counted as not run."""
    import io
    import time
    fence = _worker["fence"]
    tried = len(fence.tried) if fence else 0
    out = io.StringIO()
    t0 = time.monotonic()
    try:
        runner = unittest.TextTestRunner(stream=out, verbosity=0)
        result = runner.run(discover(_worker["split"], _worker["part"], only=name)[0])
        run, ok = result.testsRun, result.wasSuccessful()
        durations = [(str(t), round(s, 3)) for t, s in getattr(result, "collectedDurations", [])]
        text = out.getvalue() if not ok else ""
    except BaseException:                                # SystemExit included: never a report a test chose
        run, ok, durations = 0, False, []
        text = out.getvalue() + traceback.format_exc() + f"gate_suite: {name} stopped before it finished, counted as failed\n"
    return {"module": name, "tests_run": run, "ok": ok, "text": text, "seconds": round(time.monotonic() - t0, 3),
            "durations": durations, "tried": list(fence.tried[tried:]) if fence else []}


def _size(split: Split, name: str) -> int:
    try:
        return split.modules[name].stat().st_size
    except OSError:
        return 0


def run_part(split: Split, part: str, workers: int) -> tuple[int, bool, list[str], dict]:
    """Every module of `part` across `workers` processes, the biggest files first so the slowest are not left for the
    end: (tests run, ok, modules that tried to import the instance part, timings). Workers are spawned, not forked,
    so none inherits a half-imported test module, and they share this process's group: a gate stage that times out
    ends them with it. A worker that dies fails the part."""
    import multiprocessing
    import time
    from concurrent.futures import ProcessPoolExecutor, as_completed
    names = sorted(_modules_of(split, part), key=lambda n: -_size(split, n))
    blocked = sorted(split.reads) if part == "shared" else None
    t0 = time.monotonic()
    run, ok, tried, durations, modules = 0, True, [], [], {}
    with ProcessPoolExecutor(max_workers=max(1, min(workers, len(names) or 1)),
                             mp_context=multiprocessing.get_context("spawn"),
                             initializer=_worker_start, initargs=(split, part, blocked)) as pool:
        futures = {pool.submit(_run_module, n): n for n in names}
        workers_seen = dict(pool._processes)              # private, checked below: fail closed if it goes
        for f in as_completed(futures):
            try:
                r = f.result()
            except BaseException as e:                   # the worker died (os._exit, a crash): fail closed
                ok = False
                print(f"gate_suite: {futures[f]} did not finish: a worker died while it ran or waited its turn "
                      f"({type(e).__name__}), counted as failed", file=sys.stderr, flush=True)
                continue
            run += r["tests_run"]
            ok &= r["ok"]
            tried += r["tried"]
            durations += r["durations"]
            modules[r["module"]] = r["seconds"]
            if r["text"]:
                sys.stderr.write(r["text"])
                sys.stderr.flush()
        workers_seen.update(pool._processes)
    # A worker that returned every result and then died on its way out (a finalizer, an atexit hook) failed the part
    # in one process — its exit code was the runner's. Its results stand only if it exited cleanly (Codex review).
    for pid, proc in sorted(workers_seen.items()):
        if proc.exitcode != 0:
            ok = False
            print(f"gate_suite: worker {pid} exited with {proc.exitcode} after its modules ran, counted as failed",
                  file=sys.stderr, flush=True)
    seconds = round(time.monotonic() - t0, 1)
    print(f"Ran {run} tests of the {part} part in {seconds}s ({len(names)} modules, {workers} workers): "
          f"{'OK' if ok else 'FAILED'}", file=sys.stderr)
    from collections import Counter
    times = {"seconds": seconds,
             # a TestCase class imported into another module is discovered there too: the same test id runs twice
             "run_twice": sorted(t for t, n in Counter(t for t, _ in durations).items() if n > 1),
             "slowest_tests": sorted(durations, key=lambda d: -d[1])[:SLOWEST_KEPT],
             "slowest_modules": sorted(modules.items(), key=lambda m: -m[1])[:SLOWEST_KEPT]}
    return run, ok, tried, times


def times(stderr: str) -> dict | None:
    """The timings the last run printed (run_part), or None."""
    line = next((l for l in reversed(stderr.splitlines()) if l.startswith(TIMES)), None)
    try:
        return json.loads(line[len(TIMES):]) if line else None
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    args, workers = [], 1
    rest = iter(argv)
    for a in rest:
        if a == "--workers":
            try:
                workers = int(next(rest, ""))
            except ValueError:
                workers = 0
        else:
            args.append(a)
    plain = [a for a in args if a != "--nonce-stdin"]
    if (len(plain) != 1 or plain[0] not in (*PARTS, "count") or workers < 1
            or (plain[0] == "count" and len(plain) != len(argv))):
        print(f"usage: python -m supervisor.gate_suite {'|'.join(PARTS)} [--nonce-stdin] [--workers N] | count",
              file=sys.stderr)
        return 2
    part = plain[0]
    nonce = sys.stdin.readline().strip() if "--nonce-stdin" in args else None   # before any test can read stdin
    split = Split(Path.cwd() / TESTS, Path.cwd())
    if part == "count":
        print(json.dumps(_count(split)))
        return 0

    def done(tests_run: int, ok: bool) -> int:
        sys.stderr.flush()
        print(REPORT + json.dumps({"part": part, "tests_run": tests_run, "ok": ok}
                                  | ({"nonce": nonce} if nonce is not None else {})), flush=True)
        return 0 if ok else 1

    if part == "shared" and hasattr(os, "geteuid") and os.geteuid() == 0:
        print("gate_suite: the shared part does not run as root: root can read the folder that stands in for the "
              "instance", file=sys.stderr)
        return done(0, False)
    poison, run, tried = None, 0, []
    if part == "shared":
        poison = Path(tempfile.mkdtemp(prefix="shaft-no-instance-"))
        poison.chmod(0)
        os.environ[UNDER_TEST] = str(poison)             # before any worker starts: they inherit it
    try:
        run, ok, tried, timing = run_part(split, part, workers)
        print(TIMES + json.dumps(timing), file=sys.stderr)
    except BaseException:
        traceback.print_exc()
        print("gate_suite: the part stopped before it finished, counted as failed", file=sys.stderr)
        ok = False
    finally:
        if poison:
            poison.chmod(0o700)
            shutil.rmtree(poison, ignore_errors=True)
    if tried:
        ok = False
        print(f"gate_suite: the shared part tried to import {', '.join(sorted(set(tried)))}, which "
              "read(s) the instance under test", file=sys.stderr)
    return done(run, ok)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
