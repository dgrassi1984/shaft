# SHAFT — Plan: `shaft top` as the control panel

*Drafted and signed off 2026-10-13; M0–M4 built on branch `control-panel` (see §5).*

## 0. Where it stands

`shaft top` (`supervisor/top.py`, 375 lines, rich in `.venv-ui`) shows a snapshot that `supervisor/dashboard.py`
(stdlib only) refreshes every second:
- each instance's state, what it is doing now, its goal, today's calls, tokens and cost, its judged rate, its
  schedule and any running trial;
- the four services;
- the last 12 lines of `supervisor.log`;
- the messages between instances, which you can browse.

The only action is `c`, which starts a cycle for an idle instance.

Everything else is CLI-only, spread over ~30 subcommands and files in `.supervisor/`:
- judgments with reasoning (`judgments/*.jsonl`);
- trials (`trials/`, `trial_usage.jsonl`);
- the battery (`battery/`, not installed yet);
- probation (`probation/`) and kept code changes (`code_changes/`);
- distinct and breed evals (`evals/`) and the surprise baseline;
- the spend of every paid component (`cloud_usage`, `judge_usage`, `coder_usage`, `trial_usage`);
- pause/resume, update, tools, hosts and shared folders, messaging, and the services.

**One gap the panel should close on day one: the judge has no daily cap.** It is now most of the spend: about
90% of each trial. Aggregate judge usage needs its own visible total.

## 1. Design decisions

**One process, tabbed views, number keys.** A tab bar and `1`–`9` switch views; `?` shows every key. Each view
is a pair of pure functions, `render_<view>(data, ui, w, h)` and `handle_<view>(ui, key, data)`. The current
render/handle split already works this way, which keeps every view testable without a terminal (as
`test_messages_view.py` does today).

**The data layer stays stdlib.** `dashboard.py` keeps the overview snapshot unchanged (its tests too);
the new views read from `supervisor/panel.py`, and their rendering and keys live in `supervisor/top_views.py`.
(Built that way instead of splitting `dashboard.py` into a package: same separation, no churn in code that
already works.) Each reuses the supervisor's own functions rather than parsing files a second time:
- `metrics.judgments`, `metrics.cycle_value`, `metrics.stats_text`;
- `trial.all_trials`, `trial.report`, `trial.usage_today`;
- `battery.results`, `battery.report`, `battery.due`;
- `surprise.baseline`, `messages.history`.

That way the panel and `shaft stats` can never disagree.

**Refresh by cost.** The overview and the live state of the selected instance refresh every second, as now.
Everything else is read only while its view is open, and cached by file modification time, so a 200 KB
`cloud_usage.jsonl` is not re-parsed every second.

**Every action goes through the existing code path.** Each action calls the same function as its CLI
subcommand, so the panel adds no new authority:
- `autonomy.start_wake`, `core.set_tool`, `messages.send` / `reply`, `cmd_update`, `trial.create`,
  `autonomy.service`, pause and resume.

Every action:
- asks y/n on a line that states the exact effect, and the expected cost where there is one;
- runs detached (`start_new_session=True`, a log in `.supervisor/`) if it takes longer than a second, so
  quitting the TUI never kills it;
- leaves a `top: operator …` line in `supervisor.log`.

**Guards are taken from today's incidents, not invented.**
- **Restarting the scheduler is refused while a scheduled run is in progress.** Its children share its
  process group, so a launchd restart kills them. The panel says which run blocks it and offers to restart
  when it ends.
- **`update` on an instance with uncommitted edits shows the dirty files.** It then refuses, as
  `cmd_update` does.
- **Paid actions show today's spend against the cap before y/n:** a trial, a battery run, a backfill.
- **Secrets stay CLI-only.** Their values must never pass through a screen that redraws. The same goes for
  `new`, `breed` and `finetune`, which are rare and have long outputs.

## 2. Views

| Key | View | Shows | Actions |
|---|---|---|---|
| 1 | Overview | Today's view, plus a top line with today's total spend against the caps, and alerts: a failed gate or restore, a failed run, a service down, a cap above 80%. | `c` cycle, `p` pause/resume, `s` sleep now, Enter opens the instance. |
| 2 | Instance | `stats.md` (rendered); config (model, wake, effort); tools off, write hosts, shared folder; the last 10 journal headings; open probation entries with their numbers; kept code changes (self / coder / template); the version line. | `u` update (gated), `t` toggle a tool, `w` send a message, `r` reply to its open question. |
| 3 | Judge | Per instance, newest first: time, goal, origin (supported?), difficulty, novelty, claimed vs judged, criterion quality, failure mode. Enter shows the full reasoning. A sparkline of achieved difficulty per cycle, by model. | — (backfill stays CLI: it is paid and rare). |
| 4 | Trials | `all_trials`: state, who asked (instance or operator), hypothesis, pairs won/lost, p, verdict, cost; queue and today's caps (trials, USD, Bonsai units). Enter shows `trial.report`. | Queue an operator trial on a past change (with a cost estimate). |
| 5 | Benchmarks | Battery: installed or not, era, last run, due tonight, per-category results against the control. Distinct and breed evals. Surprise baseline per instance. | Run the battery now (when installed; with a cost estimate). |
| 6 | Changes | Kept self-changes over time, probation entries (open, passed, undone, restore failed) with their p-values, the trial each one came from, template merges per instance. | — |
| 7 | Messages | Unchanged (today's `m` browser). | `w` write, `r` reply. |
| 8 | Costs | Today and the last 7 days, per component (instances' cloud use, judge, coder, trials) and per instance, against each cap; judge calls by purpose (cycle, trait, trial, backfill, distinct). | — |
| 9 | Services | model, embed, api, scheduler: running, PID, version (the running commit vs what is checked out); the log tail of the one selected; autonomy on/off. | Start, stop or restart a service (guarded); autonomy on/off. |

## 3. Milestones

| M | Deliverable | Acceptance |
|---|---|---|
| M0 | Tab framework, `?` help, `dashboard/` package, mtime cache. Today's overview and messages move to views 1 and 7, behaviour unchanged. | Every existing test in `test_messages_view` and `test_dashboard` passes unchanged. Driven in a real terminal: today's keys still work. A snapshot of the overview costs no more time than today's (measured). |
| M1 | Read-only views 2, 3, 4, 8, 9. | Each view's numbers equal the CLI's for the same instance (`shaft stats`, `shaft trial list` / `show`, the usage logs summed by hand for one day), checked by a test on fixtures and once on the live system. The judge's total spend is visible. |
| M2 | Read-only views 5 and 6, and overview alerts. | Fixture failures raise alerts; kept probation corrections appear as kept. |
| M3 | Pause/resume, sleep, update, tool toggle, send/reply, trial and guarded service actions. | Check arguments and CLI refusals; drive pause/resume, inbox delivery and restart refusal with a test instance. |
| M4 | `--once` prints the new sections as text, a narrow-terminal layout (80 columns), README. | `shaft top --once` from a pipe shows spend, alerts and trials. At 80×24 no view breaks its layout. |

Verification on the real system uses a real terminal tab driven and read back (Claude Code's terminal panel),
not a backend call.

## 4. Decisions

1. **Actions in v1:** all four groups — instance control, messaging, paid runs (trial, battery), services and
   autonomy. Secrets, `new`, `breed` and `finetune` stay CLI-only.
2. **Judge spend is shown, not capped.** The panel shows it per day and per purpose, with an alert at a
   threshold; judging never stops.
3. **Alerts appear in the panel only**, no macOS notification.
4. **Order:** read-only first — M0 → M1 → M2 → M3 → M4.

## 5. Status (2026-10-13)

Built: M0–M4 on branch `control-panel`, in `supervisor/panel.py`, `supervisor/top_views.py` and `supervisor/top.py`.

**Tests:** `supervisor/tests/test_control_panel.py`, 23 tests:
- data on fixture files: every dollar counted once, alerts, judgments, probation log, mtime cache;
- every key and action with the subprocess mocked (asserting the exact CLI command);
- every tab rendered, including at 80×24.

**Terminal verification:** tabs rendered data, verdicts and trial reports opened in full, and
the restart guard refused while cycles ran. Pause/resume actions were logged.

Not driven live: sending a message (it would enter an instance's memory), sleep, update, battery and trial
actions. Their exact commands are asserted in the tests, and those commands are the existing CLI.

Custom operator trials (a chosen change) stay in the CLI (`shaft trial run --cand/--base`); the panel queues
A/A calibration trials only.
