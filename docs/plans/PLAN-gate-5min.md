# PLAN-gate-5min — every gate test stage under five minutes, and kept there

## Problem (2026-10-29)

`SHAFT_GATE_TIMEOUT_S` lets each gate test stage run 1800 s (`supervisor/gate.py:24`). The shared part of the
supervisor suite (`python -m supervisor.gate_suite shared`) took **494–955 s** for 2205–2542 tests between
2026-10-27 and 2026-10-29 (`supervisor.log`, "shared supervisor tests passed … recorded"). It timed out against the
old 900 s limit on 2026-10-19, -25 (×3), -26 and -28 (×2), failing the instance's change each time. The operator
asked for a 5-minute limit, only the tests that matter, and a limit that holds as tests are added.

A full run on python@3.12 (2026-10-29 14:29, load average 15–24): **2615 tests, 759 s wall, 167 s user + 176 s sys
CPU**. Wall time is more than twice the CPU time, so most of it is waiting: sleeps, timeouts, subprocesses and
contention.

| Where the time goes | Seconds |
|---|---|
| `test_judge2 … test_a_run_left_out_is_not_counted_as_a_failure` (one test) | 72 |
| Next 9 modules (`test_run_transaction`, `test_trial`, `test_gate`, `test_mind`, `test_api`, `test_web_chat`, `test_measurement`, `test_supervision`, `test_commit`) | 356 |
| 212 tests ≥ 1 s | 491 |
| The other ~2400 tests | 267 |

The 72 s test is a leak. It patches `metrics.INSTANCES`, but `metrics.stats_text` → `provenance.section` reads
`core.INSTANCES`, so in the main checkout it can check live facts against live episodes
(`provenance.py:131`). In the gate's committed tree `instances/` does not exist, so the gate does not pay those
72 s. Tests must not read live data in any checkout (docs/worktrees.md).

## Decisions (operator, 2026-10-29)

- **Prune and run in parallel.** Run the shared part across worker processes, and also delete or speed up tests
  that do not earn their time.
- **The limit is per stage**, as today: `SHAFT_GATE_TIMEOUT_S` = 300.

## Milestones

### M0 — tests never read live instance data
- `supervisor/tests/__init__.py` already moves `SHAFT_SUPERVISOR_DIR` out of the real state. Do the same for the
  instances folder, so no test in any checkout can reach `instances/`.
- Fix `test_judge2`'s leak at the root: point the instances folder at the test's temp folder.
- Acceptance: the judge test runs in under 1 s in the main checkout. A regression test fails if a test run's
  instances folder is the real one.

### M1 — the gate's parts run in parallel
- `gate_suite` runs a part's modules in a pool of worker processes (stdlib `concurrent.futures`, spawn), one
  module per task, slowest-first queue. Each worker installs the same guards as today: the shared part's fence
  and unreadable stand-in folder, and its own `SHAFT_SUPERVISOR_DIR`.
- A module that fails to import, has a `load_tests` hook, or tries to import the instance part fails the part, as
  today. The GATE_SUITE line and its nonce are printed only by the parent.
- Worker count: knob `SHAFT_GATE_WORKERS` (default 4), passed by `gate.py`; 1 keeps today's single process.
- Acceptance: the same tests run (`count` unchanged), with the same pass and fail outcomes. Five parallel runs in a
  row have no flaky test, and the existing `test_gate_suite` guards still pass.

### M2 — the limit is 300 s and stays there
- `SHAFT_GATE_TIMEOUT_S` defaults to 300 (`gate.py`, `docs/configuration.md`, `test_gate_timeout.py`).
- **Budget before merge:** `python -m supervisor.gate_suite budget` runs both parts as the gate does, and times
  every test. It fails when either part takes more than half the stage limit (headroom for a loaded machine), or
  when any single test takes more than 10 s. A slow new test fails the check on the change that adds it, not later,
  when the sum creeps over the limit. It prints the slowest modules and tests. It becomes the overlay's `gate`
  command (`agent-overlay.yaml`), which every work-issue, land-prs and release run executes.
- The live gate keeps only the stage timeout. A per-test cap there would flake on a loaded machine, and a flake
  in the live gate reverts a good instance change.
- After a merge, the scheduler's warm-up already runs the shared part under the same 300 s limit and alerts when
  it fails.

### M3 — keep the tests that matter
- Fix the slow waits (sleeps, timeout constants, per-test setup that could be per class) and merge tests that pay
  the same expensive setup. Delete tests that are redundant (another test fails on the same regression) or guard
  nothing.
- Never deleted: tests for sandbox and secret boundaries, the gate itself, mail label grants, data integrity, and
  the rules in `.claude/CLAUDE.md`.
- The candidate list is reviewed by a second model (Codex). **Every deletion is listed for operator sign-off
  before it is made.**

## Acceptance (whole plan)
- The shared part passes in ≤ 150 s at the measured load, and the instance part in well under that.
- `SHAFT_GATE_TIMEOUT_S` = 300.
- Adding a test that sleeps longer than the per-test cap fails the gate, and the budget check names it.

## Results (2026-10-29, `shaft gate budget`, 4 workers, load average 17–25)

| | Before | After |
|---|---|---|
| Shared part | 594–955 s (single process) | 102 s |
| Instance part | — | 6 s |
| Tests in the shared part | 2593 (19 ran twice) | 2574 |
| Slowest test | 72 s (live-data leak) | 7.7 s |

What the time was, and what was done:

- **Waiting, not testing.** Every test server stopped at `shutdown()` only on `serve_forever`'s next poll, every
  0.5 s. In test runs only, it now polls every 10 ms (`supervisor/tests/__init__.py`). `test_template_client` and
  `test_api`: 28.7 s → 6.7 s.
- **Tests that ran twice.** `test_operator_channel` imported `ApiTest`, so discover ran its tests again.
  `DocEditsTest` and `ClearTest` subclassed `DocsInChatTest` for its `setUp`, so its tests ran three times. The budget
  now fails a test that runs twice.
- **Paths frozen by a patch.** A supervisor module first imported inside a test that patched `core.SUP_DIR` kept that
  folder. In one alphabetical process this never showed. In parallel, `trial.SUP_DIR` froze on `test_browser`'s
  folder, and `test_kill_delete` failed. The test package now imports every supervisor module before any test runs.
  A probe of all 163 modules finds no frozen path.
- **A guard that did not guard.** `test_gate`'s group-kill tests passed with the group never killed. Their leftover
  wrote under `shaft/`, which the sandbox denies anyway. They now write to `.tmp/`, and fail when `_end_group` is
  disabled.
- **A race hidden by the slow shutdown.** `test_egress` read the proxy's log as soon as the file existed. It now waits
  for a whole line.

Deletion: a second model (Codex gpt-6.1-sol) classified the ~460 slowest tests: 149 KEEP, 277 SPEED-UP, 10 MERGE,
21 duplicate runs (removed above), and 2 of low value. The suite's time was waiting and duplication, not unimportant
tests. The operator signed off on deleting the two low-value tests (2026-10-29), and both are deleted:

- `test_trial.py:228`: an override whose body was `pass`, so it still paid the inherited setup (1 s). It is now `= None`, so the parent's test runs once and the subclass runs none.
- `test_surprise.CuriosityDrawTest.test_config_default`: pins the default `5`, and tests no behaviour.

Not done, with room to spare: Codex's merges (RT-links, TR-replay, M-backfill, ME-watch) and the shorter waits in
`test_gate`, `test_pause` and `test_telegram`. These are for when the budget runs close.
