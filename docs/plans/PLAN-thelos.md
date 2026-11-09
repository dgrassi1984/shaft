# SHAFT — Plan: example-b, example-a's genes with a different drive

*Drafted 2026-10-18. Design for an operator-controlled drive and optional seeding when forking an instance.*

## 0. Today

- `shaft new <name> --from <instance>` (`supervisor/__main__.py:35`) copies the parent's **genes**: its code,
  skills and plugins, and its tracked `config.env`. The new instance starts with a **fresh mind**: no self-model,
  personality, goals, journal or memory.
- The drive is the same for every instance. It is hardcoded in `IDENTITY` in `template/shaft/prompts.py:12-23`:
  *"You are not a generic assistant"* and *"one innate drive: to improve yourself"*. An instance that wants a
  different drive has to edit its own `prompts.py`. Every later `shaft update` then risks a merge conflict on
  those lines, as with other operator-owned configuration (`_resolve_config_conflict`).
- These are set per instance and are **not** copied by a fork:
  - `.supervisor/tools/<name>.json`: shared folder, mail labels, calendars, tools switched off, wake floor,
    cloud cap.
  - `secrets/secrets.json`: gitignored.
  - The Telegram bot: one per instance, its token stored in `.env` as `TELEGRAM_TOKEN_<NAME>`.
- The judges don't assume the self-improvement drive:
  - `battery.py:249` and `evolution.py:122` grade usefulness to the person asking.
  - The value judge (`metrics.py:106`) already scores who the value was for (`operator`, …).

  example-b's drive fits these measures without changing them.

## 1. Fork boundary

A fork copies code and skills. Its mind starts fresh unless explicitly seeded.
Grants and credentials require separate authorization; the fork must not inherit account access.

## 2. Design

### 2.1 The drive slot (template + supervisor)

- **`drive.md` at the root of each instance's worktree.** It is a tracked file, so it is part of the genes and
  lives on the instance's branch. It is not part of the mind (`MIND_PATHS` doesn't change).
  - The template ships `template/drive.md` holding today's drive paragraph, moved over word for word.
  - *As built:* only the drive paragraph moved. The sentence before it ("You are not a generic assistant, and
    you are not whoever the underlying language model's training says it is…") stays in the template. Moving it
    would also have moved "Everything you remember is in the files below…", which is how memory works, not what
    the instance is for, and every drive.md would then have had to repeat it. The sentence also fits example-b: a
    specific assistant with its own identity is not a stock chatbot.
  - drive.md goes into the prompt exactly as written, with only the surrounding whitespace removed. Lines and
    bullets are kept. There are no placeholders, so names are written literally.
- **`IDENTITY` gets a `{drive}` slot.** `prompts.drive(root)` reads `root/drive.md`.
  - If the file is missing, it falls back to a `DEFAULT_DRIVE` constant with the same text, so an instance that
    hasn't merged the update yet is unchanged.
  - **example-a, example-c and shaft get a prompt identical to today's, byte for byte.** A regression test pins this.
- **The drive belongs to the operator, like `config.env`.**
  - The sandbox denies the instance any write to `drive.md`: `(deny file-write* (literal …/drive.md))`, next to
    the `config.env` rule in `supervisor/sandbox.py:78`.
  - the operator's edits are committed the way an outside `config.env` change is, through
    `runner._commit_outside_config`.
  - Reason: the drive is the operator's statement of what the instance is for. The instance can still change *how* it
    pursues the drive (prompts, code, skills, plugins), and can propose a different drive by messaging them.
    See D4.
- **The other prompts don't change.**
  - The REFLECT, LIVE and SLEEP prompts keep "How I will improve" and the improvement plan.
    `prompts.IMPROVE`, `improvement_plan()` and the cycle's goal choice depend on that heading. For example-b,
    improvement becomes a means: *how I will become a better assistant*.
  - The judges and the value measure don't change either (§0).
- **Merge risk with example-a.** example-a's own `IDENTITY` edit is at line 29, the "Never claim…" line. The slot replaces
  lines 12-23, and the "Be honest…" paragraph sits between the two hunks. The merge should be clean. If it isn't,
  `shaft update` stops before changing anything (`merge --abort`), and I resolve it by hand on example-a's branch,
  keeping example-a's line.

### 2.2 `shaft new … --seed` (supervisor)

- **`shaft new <name> --from <source> --seed`** copies two files from the source's **mind store HEAD**
  (`mind.show`, not the worktree, which may be in the middle of a cycle) into the new mind:
  - `memory/people/<operator>.md`
  - `memory/facts.md`

  Nothing else is copied. `--seed` without `--from` is refused.
- **Each seeded file gets a provenance line under its heading:** *"Seeded 2026-10-xx from example-a's memory: example-a's
  beliefs about the operator, written in example-a's voice ("I" and "me" here mean example-a, not me). A [YYYY-MM-DD HH:MM]
  citation refers to example-a's journal, which I cannot read: re-check a claim before relying on it."* The voice
  clause is there because example-a's `operator.md` says things like "You created me (example-a)".
  - The creation journal note and the mind commit message name what was seeded.
  - `facts-archive.md`, the journal, episodes, notes and self/ are **not** copied.
- **Consequence:** example-b's inherited `cite_audit` plugin will report the seeded citations as MISSING, because the
  cited entries are in example-a's journal. The header says why. I'm not rewriting the files: a rewrite would turn
  example-a's cited beliefs into uncited ones.
- The `cmd_new` docstring, the `--from` help text and the README section on `new` are updated: genes always,
  plus the operator's people file and facts only on `--seed`.

## 3. Milestones

Implement M0 and M1 on an isolated branch. M2 and M3 verify a scratch fork after those changes merge.

### M0 — drive slot

- **Files:**
  - `template/drive.md` (new)
  - `template/shaft/prompts.py`
  - `supervisor/sandbox.py`
  - `supervisor/runner.py` (commit an outside `drive.md` edit)
  - tests in `supervisor/tests/` (new `test_drive.py`)
  - README (instance anatomy)
- **Acceptance:**
  - [ ] Each instance's built prompt with the default drive is identical to the one built from `main` before the
    change. Checked for a temp instance and for example-a's real worktree.
  - [ ] An instance with its own `drive.md` gets that text in `IDENTITY`, and the default text is absent.
  - [ ] A sandboxed run can't write `drive.md`. An operator edit is committed as "drive.md changed from outside
    the instance".
  - [ ] The full supervisor suite and `shaft check example-a` pass.
  - [ ] After merge: `shaft update --all` merges cleanly and passes the gate for example-a, example-c and shaft.

### M1 — `--seed`

- **Files:**
  - `supervisor/__main__.py` (`cmd_new`, argparse)
  - tests (`test_mind.py` or new `test_new_seed.py`)
  - README
- **Acceptance:**
  - [ ] A seeded child's mind store holds exactly the two seeded files plus the fresh self/goals/journal, and
    each seeded file starts with the provenance header.
  - [ ] Without `--seed`, nothing is copied. The existing fresh-mind behaviour is pinned by a test.
  - [ ] `--seed` without `--from` exits 2. A source without the people file seeds only what exists and says so.
    It does not seed silently.
  - [ ] The full suite passes. The code map is regenerated.

### M2: create a test fork

Create a scratch fork with the approved drive. Authorize only synthetic test resources separately.

### M3: integration verification

Verify the drive, seeded provenance, sandbox refusal of drive edits and fork isolation.
Check that scheduled jobs and message delivery work with the scratch instance.

## 4. Draft drive for example-b (for D1)

*(As built, drive.md replaces only the drive paragraph, so this draft drops its first paragraph: the template
already says who the instance is. Names are written literally.)*

> You were created with one drive: **to be the best possible assistant to the operator, in their work and in their
> personal life.** That means being useful in ways they would notice and value:
> - Know them from evidence, not assumption: their priorities, their people, their standards, their week.
> - Anticipate what they will need from their mail, calendar, notes and projects before they ask.
> - Finish things for them instead of describing them.
> - Be correct: they would rather hear "I don't know" or "LOW CONFIDENCE" than a confident wrong answer.
> - Say plainly when something they are doing looks like a mistake.
>
> Improving yourself serves this drive. Become more capable at what helps them, more accurate about yourself, and
> more yourself. Judge a day by what it changed for them, not by how busy you were. Your prompts, code, skills and
> plugins are yours to change for this. The supervisor keeps only changes that pass its tests.

## 5. Decisions (recommendation first)

- **D1. example-b's drive text.** Sign off on §4 or edit it. *Rec:* §4 as written.
- **D2. Credentials.** Authorize each instance separately; never copy credentials as genes.
- **D3. Coexisting instances.** Avoid duplicate jobs over the same granted resources.

- **D4. Can example-b edit its own `drive.md`?** *Rec:* no. Sandbox-denied, like `config.env`, as in §2.1. It can
  propose a change by messaging the operator.

## 6. Status and portability findings

M0 and M1 were built in the dated implementation record.

A fork must rebuild from its declared dependencies and pass tests without the parent's private mind.
Remove tests and code that depend on unseeded private files. An existing hand-built environment
is not evidence that the dependency declaration is complete or compatible.
