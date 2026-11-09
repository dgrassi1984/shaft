# PLAN — the instance mind leaves the project's git

Goal: what an instance remembers and writes for itself is no longer committed to the project repository, so
nothing it read (mail first: PLAN-mail.md) can be pushed, merged or copied with the code. The mind is `self/`,
`memory/`, `notes/` and `inbox/` (`skills/`: see D2). The supervisor still keeps a version of the mind as it was
at every cycle, because trial replays, drift and the instance's own `history` tool need it. That versioning
moves to a store that is local only, with no remote.

**Status (2026-10-15): done through M5 except the battery check.**
The design moves the mind out of remotely tracked branches while retaining local snapshots for replays.
It must precede mail access because mail content may become part of the mind.

## What depends on the mind being in git today (read in the code, 2026-10-15)

| Where | What it does with the mind | After this plan |
|---|---|---|
| `core.py:27` `STATE_PATHS`, `core.py:345` `commit_state` | Commits the mind after every run (`live`, `measurement`, `update`, `sleep skipped`; callers `runner.py:560/602`, `__main__.py:113/332`) | Commits the mind to the mind store. The project branch gets code only. |
| `trial.py:237` `state_of` | Finds `start_commit` = the last commit on `instance/<name>` before the cycle began; stored in each `trial.json` | Unchanged. The mind's commit is derived from the stored `t0` when needed (`mind.at(name, t0)`), so trials already on disk keep working with no new field |
| `trial.py:248` `reconstruct` | `git archive start_commit`: the code **and** the mind at cycle start | Code from `start_commit`, mind from `mind.at(name, t0)` |
| `trial.py:270` | Reads the inbox messages from `memory/messages/` at the branch tip | Reads them from the mind store's tip |
| `trial.py:504` | Reads `self/goals.md` at `start_commit` for the judge | Reads it at `mind.at(name, t0)` |
| `broker.py:820` `_history` (the instance's `history` tool) and `ReplayBroker._history_head` | Shows log and diffs of its branch, mind changes included | Reads both repos. Each line is tagged `code` or `mind`, and a commit ID is looked up in either. |
| `evolution.py:39` `drift` | `git log` of `self/self.md`, `self/personality.md` on the branch | Same log, read from the mind store |
| `evolution.py:340` `_build_child`, `__main__.py:53` create / fork | Commits the newborn's empty mind on its branch | Creates the newborn's mind store with that first commit |
| `__main__.py:222` status | When `self/self.md` last changed, from git | From the mind store |
| `battery.py:129` `_files` (snapshot and restore around an exam in the instance's **real** folder) | Lists files with `git ls-files --cached --others --exclude-standard` | **Would silently stop restoring the mind** once it is gitignored: an exam could leave its edits in the instance's real memory. It must list the mind paths explicitly. |

Things that don't change: the gate (`gate.CODE_PATHS` is code only), `restore_change` (code only), the
`config.env` commit, probation, `version.py`, and the template merge in `shaft update` (the template branch
tracks no mind file except `skills/writing-plugins.md`, see D2).

## Decisions

- **D1 — The store is a private git repository per instance**: `.supervisor/minds/<name>.git` (bare), with the
  instance folder as its work tree and only the mind paths added. *(approved 2026-10-15)*
  - It is still git, but never the project's repository:
    - no remote, ever: every commit checks that `git remote` is empty, and refuses and logs loudly if not;
    - a `pre-push` hook that always fails;
    - it lives under `.supervisor/`, which is gitignored and outside every sandbox.
  - Why git and not a custom snapshot store: delta compression and the history tools come free. A plain copy
    per cycle would not scale. example-a's `memory/journal.md` is 1.0 MB with 267 versions, about 270 MB for that
    one file without deltas.
  - The `git_wt` hardening applies: hooks off, fsmonitor off, nothing read from the instance-writable folder as
    configuration. Paths are added with `add -f -A -- <mind paths>` so the instance's own `.gitignore` files
    cannot hide part of its mind from the store.
- **D2 — `skills/` stays in the project's git** *(approved 2026-10-15)*. It acts as a gene, not
  memory:
  - the template ships `skills/writing-plugins.md`;
  - `evolution.GENES` recombines skills when breeding, and `fork` copies them;
  - `evolution.MIND` already leaves skills out.

  Moving it would break both, and `shaft update` would recreate the template's file. The risk accepted: an
  instance could write mail text into a skill. If you still want it out, M2 gains an evolution rework: skills
  read from the mind store, and the template's skill shipped elsewhere.
- **D3 — The past is backfilled, not rewritten.**
  - Every existing commit on `instance/<name>` that touched the mind gets a twin in the mind store: the same
    mind tree, the same author and commit dates, the same message. Replays of any past cycle then use **one**
    path (the mind store), with no "before / after the cutover" branch in the code.
  - The project branches keep their past mind commits **as they are**. Rewriting them would change every commit
    ID that the supervisor stored (probation entries, `code_changes`, trial records with `start_commit`,
    `restore_change`), and those would silently stop resolving.
  - What the old commits hold predates any mail grant, so it cannot contain mail.
- **D4 — The remote instance branches.** `origin/instance/shaft` and `origin/instance/example-a` (pushed by hand on
  2026-10-13) hold the mind as of that day. The proposal:
  - delete them;
  - add a `pre-push` hook in the project repository that refuses any `instance/*` ref, because their local
    history still holds the old mind.

  Deleting a remote branch is irreversible: **done only on your explicit go**, in M3. — Done 2026-10-15; the
  first hook also refused the delete itself (a delete names `refs/heads/instance/*`), fixed in `mind.py`
  with a regression test.
- **D5 — Cutover per instance, while it is idle** (not busy, no open conversation: `runner.open_conversations`).
  The order is example-c (smallest: 87 commits), then shaft, then example-a. One instance at a time, each verified before
  the next.

## M0 — measure, on copies (nothing live touched)

- Run the backfill against a `git clone --mirror` copy of the project for all three instances. Record the time,
  the size of each mind store, and the number of mind commits against `git rev-list --count instance/<name>`.
- **Equivalence**: for 20 past live cycles per instance (picked across the whole history, through
  `trial.state_of`), the mind tree from the mind store at `--before=t0` must be byte-identical to the mind
  subtrees of `git archive start_commit`. Accept: 60/60 identical. Any mismatch is a backfill bug, fixed before
  M1.

### M0 results (2026-10-15, on `git clone --mirror` of the project taken at about 13:30; nothing live touched)

| | example-c | shaft | example-a |
|---|---|---|---|
| `git rev-list --count instance/<name>` | 87 | 588 | 689 |
| first-parent commits touching the mind | 23 | 490 | 591 |
| mind commits made by the backfill | 23 | 490 | 591 |
| backfill time | 1.3 s | 16.8 s | 20.9 s |
| mind store size (after `gc`) | 0.12 MB | 3.70 MB | 8.18 MB |
| live cycles found by `trial.state_of` | 4 (all it has) | 161 | 216 |
| equivalence, picked across the history | **4/4** | **20/20** | **20/20** |
| every first-parent commit: the store at its time = the branch's mind then | 87/87 | 588/588 | 689/689 |

How to read it:
- **44/44, not 60/60.** example-c has only 4 live cycles in all, and all 4 are checked. To make up for that, a
  second check compares, at the moment of every commit on each branch's own line, the store's tree with the
  branch's mind subtrees (`mind.check_every_commit`, tree ids, so byte-identical): no difference on any of the
  1364 commits.
- **A bug found on the way, in `trial.state_of`, not in the backfill.** The first example-c run was 3/4. The cycle of
  2026-10-13 20:58 got `start_commit` a8c52d5, a *template* commit (17:27:32) that was merged into example-c later:
  `git log -1 --before=t0` walked the merges' second parents too. That replay was built from the template's
  tree, so it had an empty mind and the template's skills. Fixed with `--first-parent`, with the regression test
  `test_state_of_follows_the_instance_line_not_a_merged_template_commit`. **On disk, 7 of the 8 trials have at
  least one such state: 8 of 50 replayed states started from a template commit.** Those verdicts stay as they
  were recorded (none was rewritten). A re-run of one of those stored states now gets the right mind (from the
  store at `t0`, which never depended on `start_commit`), but its skills still come from the stored
  `start_commit`, so they are still the template's. A new trial picks its states with the fixed `state_of`.
- The backfill copies the objects with `pack-objects` → `index-pack`, then builds each twin with `mktree` and
  a raw `hash-object -t commit` that copies the author and committer lines as they are (names, e-mails, dates,
  time zones) and the message bytes. Equal tree ids are what the second check compares.

## M1 — `supervisor/mind.py`

- `init(name)`, `commit(name, message, root) -> bool` (`commit_state` builds the one message both sides
  get), `at(name, when) -> str | None`, `archive(name, rev, dest)`, `files(name, rev)`,
  `show(name, rev, path)`, `log(name, *args)`, `backfill(name) -> int`, `check(name, n)`,
  `check_every_commit(name)`, `cutover(name)`, `guard_project()`. Every function takes `project`: the real
  project's stores are in `SUP_DIR/minds/`, another repository's (a test, an M0 copy) in
  `<project>/.supervisor/minds/`.
- `MIND_PATHS = ("self", "memory", "notes", "inbox")` replaces the mind part of `STATE_PATHS` in `core.py`. That
  tuple becomes `("skills",)` (D2) and is renamed for what it now means.
- `commit_state` commits code and skills to the branch as before, and the mind to the mind store. A failure of
  either is logged with git's own error and returns False, the way it does today. Same commit message on both
  sides.
- Every git call on the mind store goes through one hardened helper, like `git_wt`.

## M2 — every consumer from the table

- `trial.py`: `reconstruct` lays the mind from the mind store at `mind.at(name, t0)` over the code from
  `start_commit`. The inbox messages and `self/goals.md` are read from the mind store. A trial replay
  **never** writes to the mind store; a test checks this (project rule: replays never commit).
- `broker.py`: `_history` covers both repos. `ReplayBroker` stops both at the replayed cycle (`start_commit` and
  `mind.at(name, as_of)`).
- `evolution.py`: `drift` reads the mind store; `_build_child` creates the child's mind store.
- `__main__.py`: `create` / `fork` create the mind store with the first mind commit; status reads `self/self.md`
  from it.
- `battery.py`: `_files` = git-listed files ∪ every file under the mind paths, so an exam's mind edits are undone.
- Template `.gitignore`: `self/`, `memory/`, `notes/`, `inbox/`. `commit_state` never stages mind paths
  whatever `.gitignore` says.

## M3 — cutover (per D5), once M1–M2 are merged

Built as `shaft mind cutover <name>` (one instance at a time). It holds the instance's run lock, refuses while a
conversation is open, and refuses while the API service still runs code from before the merge: the API runs
conversations in its own process, so old code there would put the mind back on the branch. Then it runs
`mind.backfill`, both M0 checks (20 picked cycles and every commit), and stops with nothing changed if any check
differs. Otherwise it makes the untrack commit "the mind moves to the supervisor's mind store" on
`instance/<name>` (`git rm -r --cached`, so the files stay on disk), then commits the mind as it is on disk to the
store. **None of this has been run on a live instance.**

Merging changes behaviour at once: `commit_state` refuses an instance with no mind store (logged, "shaft mind
cutover <name>"), so a run between the merge and its cutover commits nothing. Hence the order:
1. `shaft pause` (all).
2. Merge `mind-store` into `main`, and `template-mind-store` into `template`.
3. `shaft api off && shaft api on`: the API runs conversations in its own process, so it must run the new code
   (the Telegram service only forwards to the API; the scheduler starts each cycle as a new process).
4. `shaft mind cutover example-c`, check it (`shaft mind status example-c`, `git -C instances/example-c show --stat HEAD`),
   then shaft, then example-a.
5. `shaft update --all`: the template's `.gitignore` lines reach the instances. Until then the mind shows up as
   untracked in `git status`; nothing stages it (`commit_state`, `shaft new`, birth all leave it out).
6. `shaft resume`, then M5.

Then:
- `shaft mind guard`: the project's `pre-push` hook refusing `instance/*` (it never replaces a hook it did not
  write).
- **Only on your go**: `git push origin --delete instance/shaft instance/example-a`.

## M4 — tests (regression, named for what they guard)

- `test_commit_never_stages_mind`: even with an empty `.gitignore`, the branch commit holds no mind path, and
  the mind store holds the change.
- `test_mind_store_refuses_remote`: a remote added to the store makes the next commit refuse loudly, and the
  `pre-push` hook fails.
- `test_mind_store_ignores_instance_gitignore`: a `memory/.gitignore` written by the instance hides nothing from
  the store.
- `test_replay_mind_as_of_cycle`: the reconstructed mind equals the mind at `t0`, not the current one.
- `test_replay_never_writes_mind_store`.
- `test_backfill_equivalence`: on a fixture branch with mind and code commits interleaved.
- `test_history_spans_code_and_mind`, and the replay variant stops at both heads.
- `test_battery_restores_mind_edits`: an exam that edits `memory/journal.md` in the real folder is undone.
  This guards the silent failure found while planning.
- `test_drift_reads_mind_store`
- `test_create_and_birth_init_mind_store`
- Update the tests that pin the old behaviour: `test_commit.py`, `test_pause.py`, `test_sleep_awareness.py`
  (`commit_state`), `test_trial.py` (`reconstruct`, `_history`), `test_supervision.py` (`_history`), and
  `test_measurement.py` (the battery snapshot). A test that still passes unchanged on
  the new code means a missed callsite: look again.
  - Done: `test_commit.py`, `test_trial.py`, `test_measurement.py` and `test_config_writers.py` (a real live
    cycle through `runner.run_agent`: its journal now lands in the store, under the run's message) moved to the
    new layout. The other three, read again: `test_pause.py` and `test_sleep_awareness.py` stub `commit_state`
    out (they test pausing and sleep notes, not what is committed). `test_supervision.py` has no `_history`
    test; its probation-restore fixture keeps a mind on the branch, which is the pre-cutover past that
    `restore_change` (code only) must still handle. All three pass unchanged, rightly.
  - New: `test_mind.py` (backfill, cutover tooling, drift, status, create, fork and birth) and `mindfix.py`.
- Full suite green: 499 tests, 44 skipped, 1 failure that is not this branch's —
  `test_shaft_secret_set_from_stdin_never_prints_the_value` asserts the checkout folder is named `shaft`, so it
  fails in every worktree (also on `main`) and passes in a checkout named `shaft`.
- Merged with `claude/mail-access-7d2dd4` (+ `template-mail`) on a throwaway branch: all code merges by itself, the
  generated code map conflicts (regenerate it with `gen_codemap.py --root .`), and one mail test pins the old
  layout: `test_attachment_lands_in_ignored_mail_dir` expects `notes/from-mail.md` committed on the branch. In the
  merge it must check the note lands in example-a's mind store and neither the note nor `mail/` on the branch; with
  that change the combined suite (534 tests) has only the failure above.

## M5 — verify in the running system (example-c first)

1. Let example-c run one live cycle. Accept: `git show --stat HEAD` on `instance/example-c` lists no mind path; the mind
   store's newest commit holds the cycle's journal and episode changes; the files on disk are unchanged in
   behaviour.
2. Through the normal talk path, ask example-c to show its recent history. Accept: it sees the cycle's journal change
   (tagged `mind`).
3. Run a trial replay on one cycle from before the cutover and one from after. Accept: both reconstruct, and the
   judge sees `self/goals.md` as of each cycle.
4. Run one battery item on the instance arm that writes to memory. Accept: the real `memory/` is unchanged
   afterwards.
5. `shaft top` and status show self-model freshness.
6. Then shaft and example-a (D5). Only after example-a is cut over can PLAN-mail.md M5 grant `AssistantAccess`.

### M5 results (2026-10-15)

1. Verify that a cycle without code changes creates no gene commit and that its mind snapshot remains available.


## Risks and what this does not do

- **The mind store keeps every version, locally, forever.** Mail text that enters example-a's mind stays in its
  history even after a revoke. A purge command (`shaft mind forget <name> <path>`, history rewrite of the mind
  store only) is a follow-up if needed. Its commit IDs are referenced only by trials, so it can be rewritten
  far more safely than the project branches.
- **No offsite copy of the mind** apart from the machine's own backup (e.g. Time Machine). That is the point of
  this plan, and a loss if the disk dies.
- Two repositories to keep in step: a commit on one side without the other leaves code and mind out of step for
  that cycle. Both are written in the same `commit_state` call, and a failure on either is logged and returned.
