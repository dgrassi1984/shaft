# PLAN-gate-cache — the supervisor's own tests run once per commit, not once per code change

## Problem (today)

Every code change an instance makes is held at the test gate (`gate.run_tests`, `supervisor/gate.py:435`) for 8–10
minutes, and almost all of that time re-tests the supervisor, which did not change.

- The gate's first stage, "supervisor invariants", runs the whole `supervisor/tests` suite from the committed tree
  (`gate.py:464`): **1962 tests in 490 s**, compared with **1170 instance tests in 4.8 s** in a measured gate.
- `runner._settle` holds the instance run lock while running the gate (`runner.py:939`).
  A stopped reply that edited code can therefore delay the next message until testing finishes.
  Autonomous cycles waiting for that lock are delayed too.
- All seven callers go through `run_tests`: `runner._settle`, `shaft update` (`__main__.py:243`), `shaft check`
  (`__main__.py:1496`), the coder hand-off (`coder.py:219`), the probation revert (`metrics.py:1797`), evolution
  (`evolution.py:361`) and the trial gate (`trial.py:849`).

Only **3 of the 126** test modules read the instance under test:

| Module | How it reads the instance |
|---|---|
| `test_invariants.py` | `TARGET = $SHAFT_UNDER_TEST` (line 23), read at import (line 28) |
| `test_judge_evidence.py` | imports `InstanceCase` from `test_invariants` |
| `test_vision.py` | imports `InstanceCase` from `test_invariants` (line 383); never names `SHAFT_UNDER_TEST` |

A split by grepping for `SHAFT_UNDER_TEST` would have missed `test_vision.py`. The partition below is decided from
imports, and a wrong partition fails loudly.

Every other test module cannot reach the instance under test. `committed_tree` (`gate.py:299`) gives the suite an
empty `instances/` and a `.env` with no secrets, so `SHAFT_UNDER_TEST` is the only way to the instance. Those tests'
result depends only on the commit pair (supervisor HEAD + template HEAD) and the Python that runs them, yet every
gate runs them again.

Measured on 2026-10-27 in the gate's own tree for HEAD `8c54ef3` / template `c656515`, against example-b's code:

| Part | Tests | Time |
|---|---|---|
| Instance part (the 3 modules) | 60 (3 skipped) | 18 s |
| Whole suite (one run, as the gate runs it) | 2111 (60 skipped) | 535 s |
| Shared part, by difference | 2051 | ≈ 517 s |

No single test dominates: the slowest is 10.3 s (a gate-timeout regression), and the top 20 add up to about 100 s.
Shortening individual tests would not solve this; not running the shared part again does.

## What changes

The "supervisor invariants" stage runs in two parts:

1. **Instance part**: the modules that read the instance under test, run with `SHAFT_UNDER_TEST` at every gate, as
   today.
2. **Shared part**: every other module. It runs once per commit pair, and the pass is recorded beside the committed
   tree. A later gate on the same commit pair reads the record instead of running the shared part again.

The plugins check and the instance's own tests still run at every gate. A gate on a commit whose shared part has
passed then takes about 18 s + 5 s instead of 8–10 minutes.

### Partition: read from the code, not kept in a list

`supervisor/gate_suite.py` runs inside the committed tree as
`python -m supervisor.gate_suite instance|shared [--nonce-stdin]`. Built in M0, after two Codex review passes:

- **Both parts are unittest's own discover** (`-s supervisor/tests -t .`, as the gate runs it today): the same
  walk, packages and `supervisor/tests/__init__.py` included, the same report of a module that fails to import. A
  part only stops discover importing the other part's modules: it gets an empty stand-in for them. No test can drop
  out of both parts. A first version that walked the files itself dropped package-level tests (Codex, pass 1).
- **The split is read with `ast`, without importing anything.** A module is in the instance part if it is
  `test_invariants`, holds the string `"SHAFT_UNDER_TEST"`, does not parse, or imports a module in that part,
  directly or through any helper in `supervisor/tests`. On today's tree the instance part is `test_invariants`,
  `test_judge_evidence` and `test_vision`; the other 127 modules are shared.
- **A `load_tests` hook fails its part, and the hook is never called.** A hook can load any module itself,
  including the other part's, and the split could not tell where those tests belong (Codex, pass 2). It is checked at
  load time, so a hook that is imported, conditional or assigned counts too. No supervisor test has one.
- **Output:** unittest's own on stderr, so `failing_tests` (`gate.py:518`) still names each failure. Then one last
  stdout line, `GATE_SUITE {"part", "tests_run", "ok"[, "nonce"]}`. With `--nonce-stdin` the first line of stdin is
  read before any test is imported and repeated in that line. `report(stdout, nonce)` accepts only that exact shape
  carrying that nonce, so nothing a test prints can pass for the report. Any exception during discovery or the run,
  `SystemExit` included, is a failed part.
- `python -m supervisor.gate_suite count` prints the test counts of the whole suite and of each part. It imports
  the modules but runs none; the parity test uses it.

**Guards: a missed dependency fails the shared part, never passes it tested against something else.**
- `SHAFT_UNDER_TEST` names a temporary folder that exists but cannot be read (mode 000), removed after the run. A
  test that looks the variable up under any name and touches anything inside, even a guard like
  `(TARGET / "config.env").exists()`, gets a `PermissionError`.
- Every module the scan puts in the instance part, helpers included, is blocked from import. An attempt fails the
  part even when the test catches the `ImportError`.
- The shared part refuses to run as root, who can read the folder.

**What M0 does not catch, by choice:** a test that looks the variable up under a name the scan cannot see *and*
skips its check when the folder is unreadable (`os.access`). The supervisor's tests are trusted code, committed to
`main`, that no instance can edit; the guards are for an honest mistake, not for a test written to slip past them.

### The record

- **Where:** `<project>/.supervisor/gate-tests/<commits>/shared-passed.json`. `<commits>` is the folder
  `committed_tree` already makes per commit pair. The record sits next to the tree, not inside it: the tests run with
  the tree as their working folder. It is removed with the tree after `TREE_IDLE_S`. An instance cannot write there,
  because its sandbox allows writes only inside its own folder (`sandbox.py:131–132`).
- **Key, stored and compared in full:** the supervisor and template commits, the `sys.version` of `core.PYTHON`, and
  a digest of the `SHAFT_*` variables in the environment the tests get (not counting `SHAFT_UNDER_TEST`). If any of
  them differs, the record is ignored.
- **Age:** a record older than `TREE_IDLE_S` (a day) is ignored, the same limit the tree itself has. This covers a
  library upgraded under the same commit; the warm-up (below) re-runs the shared part before any gate needs it.
- **Contents:** the key, `tests_run`, `seconds` and `passed_at`. It is written atomically (temporary file, then
  rename), only after exit 0 and `tests_run > 0`.
- **Only passes are recorded.** After a failure, the next gate runs the shared part again: a flaky test or a
  machine problem must not fail every gate on that commit. A real failure fails each gate, as it does today.
- **One run at a time per commit pair:** a lock (`flock`) on `<commits>/shared.lock`. A second gate (another
  instance's settle, or the warm-up) waits for the first one's result instead of running its own 8-minute copy. If the
  first run failed, the waiter runs the shared part itself, so its log shows its own failure. The wait is bounded by
  the stage's `timeout_s`; when it runs out, the gate fails closed (`StageTimedOut`).

### Output

The stage header stays `== supervisor invariants (<label>) ==`, which `test_gate.py` (lines 156 and 234–237) checks
for. Under it there are two parts:

```
-- instance part: 60 tests --
<its output, cut as today>
-- shared part: passed on this commit at 14:02 (2051 tests, 517 s), not run again --
```

or the shared part's own run output. `failing_tests` reads both parts, and a failed gate's full log keeps both.

### Warm-up: no wait on the first gate after a merge

`main` moves several times a day. Without a warm-up, the first code change after each merge pays the full run while
holding its instance's lock.

- The scheduler (`autonomy.serve`, `autonomy.gate_warm`) starts `shaft gate warm` as a child when `main`'s commits
  change, and looks at the same commits again hourly (`WARM_CHECK_S`). It uses the same `children` dict as wakes,
  jobs and trials and sits inside the not-paused branch. Reading the commits gives git `COMMITS_ID_S` (10 s), and
  nothing in `gate_warm` can raise into the loop.
- `gate.warm()` runs the shared part unless a record of the gates' key is younger than `WARM_REFRESH_S` (half a day).
  The gates accept a record for a day, so with the warm-up running they never see one expire. The verb runs at a
  lower priority (`nice`), and on SIGTERM or SIGINT it ends its test run first (`gate.end_stages`): a stage starts its
  own session and would otherwise outlive it.
- It goes through the same lock, so a gate that arrives during a warm-up waits for its result.
- A pass logs `gate: shared supervisor tests passed on <label> (N tests, S s), recorded`. A failure logs one line the
  control panel shows as an alert, naming the failing tests and the kept log; the scheduler's own line about that
  child avoids the alert words, so one failure is one alert. A crash before `warm()` could say anything is the
  scheduler's alert instead. A failed run of the shared part, by a warm-up or a gate, also deletes that key's
  record: a refresh that fails must not leave the older pass for the gates. A test failure is not retried on the same
  commits. A timeout returns `WARM_TIMED_OUT` and retries an hour after it ends (`WARM_RETRY_S`), indefinitely.
  Every gate without a recorded pass runs the shared part itself.
- Held changes are re-gated on any fresh shared pass for the current key, including a pass recorded by an instance
  gate. A pass must be newer than the last re-gate attempt; a cached pass cannot trigger a loop. No re-gate starts
  while the scheduler is paused or draining.

### What does not change

- The instance part, the plugins check and the instance's own tests run at every gate.
- The tests still come from the committed tree, never from the checkout. Timeouts, process groups, fail-closed
  paths and gate logs stay as they are.
- `shaft check <name>` uses the record too. The shared part's verdict is the same for any instance on that commit.

## Risks

- **A shared test that depends on the instance without going through `SHAFT_UNDER_TEST`.** The committed tree holds
  no instance, so no route is known. If one appeared, it would be tested against the tree's empty `instances/`, not
  skipped. The guard and the parity test (M0) catch the known route.
- **A flaky shared test is hidden after one pass.** Today the same flake reverts a good instance change at random,
  so this is a gain. The warm-up log still shows a flake when it fails.
- **The environment drifts without a commit** (Homebrew upgrades Python's site-packages). A new Python version
  changes the key; any other upgrade is covered by the one-day limit.

## Milestones

Branch `gate-cache` in its own worktree. It merges to `main` only when the operator says so. Nothing is deployed and
no service is restarted without a go-ahead.

**M0: partition** (`supervisor/gate_suite.py`, `supervisor/tests/test_gate_suite.py`)
- [x] On HEAD, the instance part holds exactly `test_invariants`, `test_judge_evidence` and `test_vision`.
- [x] Parity: instance-part count + shared-part count equals the count from `unittest discover -s supervisor/tests`
      on the same tree, so no test is dropped. This is checked by a test (`gate_suite count`), not by hand.
- [x] Guard: a scratch project whose shared module reaches the instance by a route the scan misses (a computed
      import, a helper, a computed variable name) fails the shared part and names the module.
- [x] Two Codex review passes. Their findings are fixed, each with a regression test that fails when its guard is
      turned off.
- [x] `kill.py`: the runner's command line `-m supervisor.gate_suite` matched `kill.SUPERVISOR`, so a process a test
      started in an instance's folder looked like that instance's agent. The first real shared run failed
      `test_kill_delete` this way. The runner is now a gate stage (`kill.GATE`), never a holder, with a regression
      test in `test_kill_delete.TargetsTest`.

**M1: the gate uses the split and the record** (`gate.py`, `supervisor/tests/test_gate.py`)
- [x] The gate runs each part with `--nonce-stdin` and a fresh token. A part passes only with exit 0 *and*
      `report(stdout, token)` showing `ok` true; a shared pass is recorded only when `tests_run > 0`.
- [x] A second gate on the same commit does not run the shared part again: a fake shared test appends to a file, and
      the file has one line after two gates.
- [x] A new commit runs it again. A failure is not recorded. A run of 0 tests is not recorded. A record with a
      different key, a different Python, more than a day old, dated in the future or of the wrong shape is ignored.
- [x] One record per key (`shared-passed-<digest>.json`): gates with different settings, such as a terminal's
      `shaft check` and a service, keep their own record instead of overwriting each other's. A first version with one
      record per commit failed its own settings test this way.
- [x] Regression named after the 2026-10-27 example-b 14:13–14:23 block: with a shared record present, a bad instance
      change still fails the gate through the instance part.
- [x] Two gates on the same commit at once run the shared part once; waiting for another gate is bounded and fails
      closed.
- [x] A line a test prints cannot pass for the shared part (it lacks the token), and a tree without
      `supervisor/gate_suite.py` fails closed.
- [x] Codex review: logging could make `run_tests` raise (`core.log` writes the file directly), which leaves a change
      unsettled. All gate logging is now best effort, the two older failure-path calls included, with a regression
      test. The checkout-isolation test clears the record before its second gate, so that gate still re-runs the
      shared part.
- [x] The existing `test_gate.py` tests pass, with one assertion changed: the second gate of
      `test_half_written_sandbox…` reads "Ran 3 tests" only after its record is cleared. `test_config_writers` and
      `test_pyenv` give their stand-in trees the runner and a folder of their own (they had written the lock and the
      record into `$TMPDIR`). The full supervisor suite is green.
- [x] Real system, 2026-10-27, against example-b's real code: `check example-b` took 508 s the first time (the shared part,
      2131 tests, recorded) and 18 s the second; through the CLI's `main` with the real instances folder, the whole
      gate passed (PASS) in 23.6 s.
- [ ] Real system, after the operator approves the merge and the service restart: example-b's next code change shows a
      gap of less than one minute between `end <kind>` and `code change passed tests` in `supervisor.log`.

**M2: warm-up** (`autonomy.py`, a `gate warm` verb in `__main__.py`, an alert)
- [x] Tests (`test_autonomy.GateWarmTest`): one warm-up child for new commits, none while one runs, the same commits
      looked at again hourly; a test failure is not retried, a timeout retries hourly; an unreadable commit is said
      once; and the real `serve` loop starts the warm-up and hears how it ended.
- [x] Tests (`test_gate.WarmTest`): the warm-up records under the gates' key and the next gate reads it; a record past
      half its life is refreshed while gates still take it; a failure records nothing and raises exactly one panel
      alert; a warm-up and a gate at once run the shared part once; the verb lowers its priority and traps signals.
- [x] Codex review, five findings, each fixed with a regression test that fails when its fix is reverted:
      a scheduler stop left the warm-up's test run alive (`gate.end_stages` and a signal handler; checked for real:
      SIGTERM ended the run within 4 s); `gate_warm` could raise into `serve` (now it never does); the commits read had
      no timeout (`COMMITS_ID_S`); a failed refresh left the older pass usable (the record is deleted); one failure
      raised two alerts (the scheduler's line no longer matches).
- [x] Found while verifying: `test_gate.py` had joined the instance part by quoting the variable's name in a fixture,
      so every gate would have re-run its 40 s of tests. It now spells the name via `gate_suite.UNDER_TEST`, and the
      instance part is pinned to exactly three modules.
- [x] Real system: `python -m supervisor gate warm` took 460 s on a commit with no record (pass recorded), then 0.1 s.
- [ ] Real system, after the merge and a restart of the scheduler: after a commit on `main`, `supervisor.log` shows
      the warm-up's pass, and the next settle reads the record.

**M3: docs**
- [x] `docs/safety.md` "Test gate": the two parts, the record and its key, that only passes are recorded and a failed
      or timed-out shared run deletes the record, the lock, the token, the guards and the accepted residual case, and
      the scheduler's warm-up. Drafted by Codex (gpt-6-astra) and checked sentence by sentence against the code.
- [x] `docs/cli.md`: `shaft gate warm`.
- [x] Docstrings: `gate` module, `committed_tree`, `run_tests`, `autonomy.serve`.
- [x] The code map (`docs/CODEMAP.md`, `docs/codemap/`) regenerated: it was behind the code, `gate_suite.py` missing.
- [x] Found while checking the docs: a refresh whose shared run *timed out* left the older pass in place (only a
      reported failure deleted it). A shared run that fails to finish now deletes the record too, with a regression
      test.

## Not in this plan

- Running the gate outside the instance's run lock. After this plan, a reply still waits for the gate behind a
  code change, but for about 25 s instead of 10 minutes.
- Splitting `test_vision.py` by class. Only `AnswerEndToEndTest` reads the instance, but the module goes to the
  instance part whole. That costs about 1 s per gate.
