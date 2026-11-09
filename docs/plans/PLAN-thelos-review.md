# SHAFT — Plan: the first batch from the example-b review (goals cap, verdict feed, forecast, pause alarm)

Status: **requested and built 2026-10-28; M1–M4 reviewed.**
Source: (a private review record, not published), items U1, C1, C2 and A1.

## 0. Why these four first

They are small, each fixes a measured defect, and three of them feed what example-b knows about itself:

| Item | Defect, measured 2026-10-28 | Where |
|---|---|---|
| U1 | `self/goals.md` was pasted whole into every prompt and grew mainly as an appended improvement-plan ledger. The record-write guard requires `replace=true` to shorten `self/`; sleep appended instead of compacting it. | `template/shaft/prompts.py`, `tools.py` |
| C1 | Sleep read one verdict page under `JUDGMENTS_BUDGET` (5,500 characters), enough for only a few full verdicts. Its review marker could not keep up with new judgments. | `template/shaft/sleep.py`, `supervisor/broker.py` |
| C2 | The forecast counted `ended_by` steps/repeating or no finish. A forced deadline wrap-up could still finish with `ended_by="control"`, hiding time exhaustion. | `template/shaft/forecast.py`, `agent.py` |
| A1 | A pause could remain in force indefinitely without alerting the operator. Notifying the instance did not tell the operator that it was still paused. | `supervisor/core.py`, `panel.py`, `web_alerts.py` |

## 1. Milestones

### M0 Plan, review, issues
- This file and the review in `docs/plans/`. Every other item of the review (U2–U6, C3–C7, A2–A6, plus the
  unlogged Sonnet judge cost) is a GitHub issue under milestones *example-b review M2–M5* (§3).
- Acceptance: §3 lists every issue and every issue links back to the review.

### M1 U1 — goals.md has a budget in the prompt, and sleep keeps it under it (template)
- New config knob `GOALS_CHARS` (int, default 12000) in `template/shaft/config.py`.
- The prompt shows `self/goals.md` whole when it fits. Otherwise it shows whole `## ` sections in file order while
  they fit, then one line per section left out (title and size, "read_file self/goals.md for it"), under a
  first line saying the file's size and the budget. A first section larger than the budget is cut at it, with a
  marker. Never a section cut in half otherwise.
- When goals.md is over the budget, SLEEP step 4 says so with both numbers and asks for a rewrite with
  `replace=true` under the budget, keeping active goals and standing rules, with the history moved to a `notes/`
  file. Under the budget, step 4 reads as today.
- Acceptance: regression test with a 160 KB goals.md shaped like example-b's (four short sections, then one huge
  ledger section): the rendered goals block is at most `GOALS_CHARS` + 1,000; the first four sections are whole; the
  ledger section is named as left out; the sleep prompt carries the rewrite instruction; a 5 KB goals.md renders
  byte-identical to today and the sleep prompt has no rewrite instruction.

### M2 C1 — sleep reads every new verdict (supervisor + template)
- Supervisor: the sleep's machine read (`kind="judgments"`, `since`, `format="json"`) gets its own budget
  `SLEEP_JUDGMENTS_BUDGET = 24000` (it is called from code, not a model tool, so the 6,000-character tool-result cut
  does not apply) and a compact verdict text: the same fields, the judge's reasoning cut at 600 characters with
  `record(kind='judgments', cycle='…')` for the rest. The model-facing `record` keeps `JUDGMENTS_BUDGET` and the full
  text. The tie handling at the page boundary is unchanged.
- Template: sleep reads up to `SLEEP_VERDICT_PAGES = 3` pages, following `next_since` until it is null, joining the
  texts and rows; the marker moves to the last page's `next_since`. A failed page keeps what earlier pages read and
  stops. Works unchanged against an older supervisor (one page with a null or later `next_since`).
- Acceptance: supervisor test — 60 verdicts of example-b's length: one JSON page holds at least 20 and its text fits
  the budget; the model-facing text is unchanged. Template test with a fake broker serving pages — the marker ends at
  the newest of 50 verdicts after one sleep, every verdict is appended once, and a broker error on page 2 keeps
  page 1 and its marker.

### M3 C2 — a deadline wrap-up counts as running out (template)
- `agent.Outcome` gets `wrapped_up: str` — `"deadline"`, `"steps"`, `"yield"` or `""` — set when the run's final
  wrap-up turn was forced, and why. The live episode records it (`"wrapped_up"`, only when set).
- `forecast.summarise` counts as ran out: no finish (whatever ended the loop), or `wrapped_up` deadline, even with
  a finish. A finished steps wrap-up does not count (counting it conflates a step limit
  with deadline exhaustion).
- Acceptance: regression tests — `agent.run` with a deadline inside `WRAP_UP_RESERVE_S` returns
  `wrapped_up == "deadline"`; on its last step `"steps"`; a finished episode with `wrapped_up: "deadline"` counts
  in `ran_out`, one with `"yield"` does not; the episode written by a live cycle carries the field.

### M4 A1 — the operator hears about a long pause (supervisor)
- New knob `SHAFT_PAUSE_ALERT_HOURS` (default 3; 0 = off), group "Runs and conversations".
- One function, `core.long_pauses(now)`, returns each operator pause flag (`paused-<name>`, or `paused` for all)
  older than the knob: who, since when, for how long. Flags written before this change hold an ISO time
  already (`set_paused`).
- `panel.alerts` shows each as an alert with the stable id `pause:<name>:<since>` (dismissable like the others):
  Synthetic alert: "example has been paused for 4 h: shaft resume example".
- The web service's alert thread (`web_alerts`) pushes each long pause once to the paired devices (a record of the
  ids pushed in `.supervisor/web/pause-alerts.json`), opening the control panel.
- Acceptance: tests — a flag 4 h old with the knob at 3 gives one alert and one push, and a second loop pushes
  nothing; 1 h old gives neither; the knob at 0 gives neither; resuming removes the alert; a global pause names
  "all instances".

### M5: verification with fixtures
- Render representative goals and sleep prompts before and after the change.
- Verify forecast counts on synthetic episodes with explicit expected wrap-up reasons.
- Check verdict pagination and alert rendering with fixture records.
- Run the full supervisor suite.

## 2. Not in this batch
Everything else in the review §4. The kill tests for U1 and C1 (median prompt down ≥40%, marker lag <24 h) need
the batch deployed; they are §3's first follow-up and deployment determines when.

## 3. Issues
Filed 2026-10-28 under milestones *example-b review M2 — what a cycle is for*, *M3 — recurring complaints*,
*M4 — measure what is built* and *M5 — proactive care*; the list is in §3.1 once filed.

### 3.1 Filed

| Milestone | Item → issue |
|---|---|
| example-b review M2 — what a cycle is for | U2 #129, U3 #130, U4 #131, C3 #132; existing #104, #106 |
| example-b review M3 — recurring complaints | U5 #133, U6 #134, A3 #135 |
| example-b review M4 — measure what is built | C6 #136, C7 #137, C4 #138, C5 #139 |
| example-b review M5 — proactive care | A2 #140, A4 #141, A5 #142 (design-needed), A6 #143 |

Judge cost reporting prices unpriced rows from token counts when read (`panel.judge_usd`).
