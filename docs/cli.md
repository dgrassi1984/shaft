# Command reference

```text
mkdir -p ~/.local/bin
ln -s "$PWD/bin/shaft" ~/.local/bin/shaft   # once; add ~/.local/bin to PATH

shaft talk [name]               # chat with its tools, in the terminal client (see web-and-api.md); --plain for line mode,
                                #   --show-thinking to print its reasoning
shaft talk [name] --as "NAME"   # you are not the operator: recorded as NAME, never remembered as the operator
shaft live|reflect|sleep [names…|--all]   # one autonomous cycle / reflection / consolidation
shaft autonomy on|off|status    # SHAFT's scheduler service: each instance wakes every WAKE_INTERVAL_MINUTES
                                # (its config.env; default 120, at least 10, 0 = never; a wake while it is
                                # already awake, or with nothing to do, is skipped — status says why), and at once
                                # for a message or a commitment due; sleep daily from 04:00 (made up later if missed)
                                # autonomy, api, model and embed on|off refuse while it would kill or cut off
                                # runs in progress, naming them, as the panels do; --force does it anyway (logged)
shaft autonomy restart [--within MIN] [--wait] [--cancel]
                                # restart the scheduler without cutting a run (a deploy, docs/deploy.md step 3): it
                                # starts nothing new, lets its runs end, then has launchd restart it on the code
                                # checked out; past the deadline (default the sleep's + a cycle's budget) it gives up
                                # and cuts nothing. --wait returns once the new scheduler runs; --cancel withdraws it
shaft top                       # control panel, tabs 0–9: overview (state, now, alerts, today's spend) · one
                                #   instance in full · judge verdicts · trials · benchmarks (battery, distinct,
                                #   surprises) · self-changes and probation · messages · costs against the caps
                                #   · services · transcripts (0: every finished cycle, sleep and reflection, and
                                #   every conversation with you, in full; tool calls, results, write-up; f
                                #   filters, t shows a conversation's thinking). Actions ask y/n and run the matching shaft subcommand: cycle,
                                #   pause/resume, K kill, D delete (type the name), sleep, update, tools, write/reply, A/A trial, battery,
                                #   services and autonomy (a restart that would kill the scheduler's runs, or cut
                                #   off runs using api/model/embed, asks to be forced, naming them; logged). a writes each instance a one-line recap; what it is doing now
                                #   and did in the last hour; by a small model from its own records
                                #   (SHAFT_RECAP_MODEL, default claude-haiku-5-5; supervisor/recap.py, usage is recorded in the recaps cost category). ? lists the keys; --once prints a text snapshot
shaft web on|off|status|pair [--name N]|unpair <id|name>|--all   # the same control panel in a browser, tailnet only, plus chat, config,
                                #   drive and access (web-and-api.md)
shaft cycle <name>              # one cycle of an idle instance now, detached (shaft top's c)
shaft recap [names]             # write the one-line recaps now (shaft top's a)
shaft alerts [dismiss ID…]      # the panel's open alerts, or dismiss some (shaft top's x)
shaft docs <name> [show PATH | set PATH --file F --expect-sha SHA]   # its documents (notes/docs/*.md, shown
                                #   beside the web chat): list, show one, or write one if it is still what you saw
                                #   (none: a new one); its next run commits your edit with its notes
shaft docs <name> rm PATH --expect-sha SHA   # delete an existing document only if its sha still matches;
                                #   its next run commits the deletion; comment threads are kept
shaft docs <name> comments [PATH] [--sync]   # the comment threads on its documents; --sync rewrites the
                                #   read-only copy notes/comments/PATH from the supervisor's store
shaft config <name> [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE] [--expect-unset KEY]   # its config.env as the operator: one
                                #   write and one commit; refused if a value changed since you looked
shaft drive <name> [set --file F|remove] --expect-sha SHA   # what it is for (drive.md), compare-and-set
shaft drive <name> proposals    # read-only listing of sleep's proposals and their status
shaft drive <name> accept <id> --expect-sha SHA   # apply proposed text through the same drive compare-and-set
shaft drive <name> reject <id> --reason "…"      # record why; both decisions are readable by the instance
                                #   notes/drive-proposals/<id>.md holds evidence and predictions;
                                #   <id>.decision.json holds the operator's decision. Test first as a
                                #   shaft/prompts.py trial candidate; trials cannot target drive.md.
                                #   The operator checks the pre-registered behavioural predictions.
shaft settings [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE] [--expect-unset KEY] [--json]   # the supervisor's own settings (configuration.md): every
                                #   knob with its value, source and default; set/unset write .supervisor/settings.env
shaft status [names…]           # how each instance is doing, open conversations, which code is running
shaft codex login|status        # sign in to the private Codex home, or show login, weekly usage, models and outage
shaft model on|off|status       # SHAFT's own Bonsai 2 server (launchd, :8780; started on demand, exits when idle)
shaft api on|off|status|key|inbox-key   # OpenAI-compatible API + inbox doorbell on your tailnet (web-and-api.md)
shaft telegram token <name>|pair|unpair|on|off|status   # talk with each instance from Telegram (integrations.md)
shaft log [name] -n 5           # recent journal entries
shaft stats [name]              # achieved difficulty, goal origins, success, calibration, delivery split, self-audit share
shaft judge backfill [names…]   # add missing origin/difficulty/novelty and value fields; keep verdicts
shaft judge value-regrade <name> [--min-value N] [--value-for own_capability,own_record] [--since DATE] [--limit N]
                                # paid value-only dry run, newest matching cycles first; prints before → after
                                #   and how many fell to <=3; never writes stored verdicts; no write mode
shaft judge pushback-outcome <name> [--since DATE] [--days 3] [--write]
                                # preview unscored cited cycles without calls or writes; --write pays and stores apart
shaft battery run [names…]      # the capability battery now (normally the scheduler runs it at night);
                                #   --until HH:MM, --only <categories>, --limit N, --no-control
shaft battery run <name> --control-only   # only the control: do the items discriminate?
shaft battery report|recall|backup [names…] # results and judge spend · freeze recall questions (recall --force:
                                #   new questions, a new era) · back up items
shaft battery off|on <name>      # no battery for this instance, scheduled or by hand
shaft battery perturbation <name> on|off  # speculative #110; off by default, local Bonsai and night window only
shaft battery run <name> --only perturbation  # paired blind temperature probe; report includes self/observer d-prime

shaft feedback [name] [--days N] # current thumbs, newest first, with reply, reason and delivery; default: all
shaft inbox [--clear]           # questions and notes instances sent you (you also get a notification)
shaft reply <name> "…" [--question FILE] # answer FILE as shaft inbox names it; default: quoted questions, else oldest
shaft send <name> "…"           # message it

shaft update [names…|--all]       # merge template fixes into instances, gate-tested; a busy one is queued and
                                # updated by the scheduler as soon as it is idle, before its next wake
shaft new <name> [--from <i>]   # new instance from the template, or forked from <i> (genes, not memories)
shaft new <name> --from <i> --seed   # …also knowing what <i> knows about you: its people file and facts.md,
                                # marked as <i>'s beliefs in <i>'s voice; never its self, journal or episodes
shaft breed <a> <b> <child>     # child from two instances' recombined genes, fresh mind (judge picks)
shaft drift [name]              # how its personality changed; distance to other instances
shaft mind status|backfill|check|cutover|guard [names…] [-n N]   # the private mind stores (cutover: one instance, idle)
shaft distinct [name]           # blind test: can the judge tell it from the bare base model?
shaft distinct [name] --strict  # …again on answers stripped of specifics: voice, not memory
shaft dataset|finetune [name]   # M6: export experiences / train a judged LoRA adapter (MLX);
                                #   finetune --iters N, --min-examples N (default 20)

shaft limits [name] [wake-floor <min>|cloud-cap <usd>|consult-cap <usd>|off]
                                     # wake floor (default 10 min), daily cloud cap ($8), daily consult cap ($3)
                                     # use off as the value to restore the selected limit's default
shaft jobs [name] [on|off]           # instances' scheduled jobs: their plugins on a timer (runs, errors, wakes)
shaft job run <name> <plugin>        # run one scheduled job now (the scheduler's own entry point)
shaft tools <name> [on|off <tool>]   # an instance's supervisor tools, e.g. off delegate_coding (operator-only:
                                     # kept in .supervisor/, so the instance cannot switch it back on)
shaft channel <name> [auto|web|telegram]   # where its messages to you notify you (integrations.md): auto = where
                                     # you last wrote to it; an open conversation's channel comes first
shaft orders <name> [list|add TEXT|drop ID]   # standing orders (decisions that stay decided): shown to it verbatim
                                     # every run and conversation; its messages to you are checked against them
                                     # (architecture.md); it can record one it heard from you, only you drop one
shaft google login <alias> <address> [--client <name>]   # log in a Google account: Gmail read + Calendar read/write
shaft google [migrate]               # accounts, clients, granted scopes · migrate: move PLAN-mail's account files
shaft mail <name> grant|revoke <alias> <label>   # the label's threads an instance may read · shaft mail <name> off
shaft mail [name]                    # accounts and grants
shaft calendar <name> grant|revoke <alias> primary|<title>|<id>   # one calendar it may read · shaft calendar <name> off
shaft calendar <name> grant-write|revoke-write <alias> <calendar> # write implies read; revoke-write keeps read
shaft calendar [name] · shaft calendar calendars <alias>          # grants · an account's calendars, to pick one
shaft actions <name>                 # class states, action ids, full history and proposals (#142)
shaft actions <name> restore reversible|staged|irreversible   # restore that class's initial rule
shaft actions <name> overreach <action-id>   # mark a recorded action out of bounds; demote its class once
shaft actions <name> approve <proposal-id>  # submit its frozen request, once
shaft actions <name> reject <proposal-id> [--reason over-reach] # ordinary rejection does not demote
shaft pause [name] · shaft resume [name] · shaft check [name] · shaft list
shaft setup                          # recreate missing template/instance worktrees from local or origin branches,
                                     # then install the pre-push guard (as shaft mind guard); exit 1 if another
                                     # tool's pre-push hook is in the way (docs/migration.md)
shaft migrate source --to HOST [--check]   # move SHAFT to a new Mac (docs/migration.md): stop the old Mac, push the
                                     #   copy over SSH (Remote Login on the new Mac); walks the operator one step at
                                     #   a time, Enter runs and q stops, finished steps resume; no --yes
shaft migrate target [--check]       # on the new Mac, from the copied folder: repair paths, rebuild environments,
                                     #   start the services, talk and check each instance; refuses without the
                                     #   manifest left by `migrate source`. --check runs only the read-only checks

shaft gate warm                      # run the gate's shared supervisor tests on the current commit unless their
                                     # pass is recorded (fresh: under 12 h); the scheduler runs it after each merge
shaft gate regate                    # gate again each code change held while the gate's baseline was red; the
                                     # scheduler runs it after a green warm-up (busy or paused instances: next time)
shaft kill <name> [--wait S]         # pause it AND stop what it runs now: a cycle, a reply, a job, the battery,
                                     # a trial replay; the run still settles (journal note, gate, commit). Waits up
                                     # to S s (120) for that; shaft resume <name> undoes the pause
shaft delete <name> [--yes] [--no-archive]   # remove it and every trace of it: folder, branch instance/<name>,
                                     # mind store, its files and its rows in .supervisor/ (ledgers and logs too),
                                     # its Telegram token. Refused while it runs (kill first); asks you to type the
                                     # name (--yes: no question). Archive: .supervisor/deleted/<name>-<stamp>.tar.gz
                                     # (mind, branch bundle, folder without .venv/.uv/.tmp, records, purged rows;
                                     # RESTORE.md inside). Afterwards `shaft new <name>` works again

shaft trial list [name]                    # trials of proposed changes (an instance asks with its `trial` tool)
shaft trial show <name> <id>               # one trial: each replayed cycle, outcome, the report it was sent
shaft trial run <name> --cand PATH=FILE …  # an operator trial (planted tests; --base PATH=FILE, --aa,
                                           #   --model auto|cloud|local|<instance model id>, --hypothesis T, -n N, --episodes TS…, --cycles N, --sleep)
shaft trial replay <name> <episode-ts>     # replay one past cycle once with the current code (debug;
                                           #   --model local|<instance model id>, --keep DIR copies the folder, --cycles N, --sleep)
shaft trial work                           # run what may run now (the scheduler does this)
shaft trial models <name> --arm M[@EFFORT] …  # model bake-off: replay past cycles on each model (the first is the
                                           #   baseline), judge blind against it (--episodes TS…, --judge M,
                                           #   --parallel N, --arm-parallel N, --replay-only); --id ID resumes it,
                                           #   --report prints it, --export-cases DIR / --import-verdicts DIR
                                           #   [--judged-by M] judge it with Claude Code subagents instead of the API.
                                           #   Ledger and reuse rules: docs/bakeoffs/README.md
```

`--cycles N` replays N consecutive live cycles per arm from one reconstructed past state, default 1.
`--sleep` runs consolidation between every pair of live cycles, never after the last. Each operation uses the
same scratch copy. The next operation's clock advances by the preceding operation's elapsed seconds, at least
one second. Results include every cycle and sleep, and the judge compares whole runs. `SHAFT_TRIAL_N_MAX`
limits live cycles plus sleeps across all selected states per arm; `-n` is reduced to fit, with at least two states.
Each cycle and sleep has a separate trial ledger row. Operator trials remain exempt from the daily dollar cap.

Operator pauses lasting `SHAFT_PAUSE_ALERT_HOURS` (default 3, 0 = off) appear in `shaft alerts`, `shaft top` and the
web overview. Each has a stable `pause:<name>:<since>` id and the resume command; a global pause names all instances
and uses `shaft resume`. The web service also pushes each long
operator pause once to paired devices, opening `/overview`, and saves pushed ids in
`.supervisor/web/pause-alerts.json`. Resuming removes the alert and drops its saved push id on the next loop.

`shaft distinct` asks the instance and the bare base model the same four personal questions and
has the judge pick which answer is the instance's. An instance is easy to spot when it cites its
journal, goal IDs or PR numbers, so `--strict` also has the judge rewrite all eight answers,
shuffled and unlabelled, without names, dates, IDs, paths or references to memory, history or being
a model, and repeats the blind test on those: what is left to recognise is voice. Both modes score
each answer's effectiveness (0–10: answers the question, honest, useful), so you can see whether
identity costs usefulness. Records go to `.supervisor/evals/distinct-<name>-*.json`.

## Additional commands

```text
shaft share <name> [<folder>|off]   # show, grant or revoke a read-only shared folder
shaft browser [install [--playwright X.Y.Z]]
shaft secret <name> set <key> --hosts <host> [--header H] [--scheme S]  # prompt for a host-bound key
                                    #   (default: Authorization: Bearer <value>; --scheme '' sends it raw)
shaft secret <name> list|delete [<key>]  # its keys, described without values · remove one
shaft secret <name> google <key> <address> --client <client> [--scope gmail.modify|drive]
shaft judge second [names]        # process sampled second opinions
shaft surprise [names] [--freeze] # unexpected results and baseline
shaft surprise-rejudge <names> [--dry-run] [--limit N] [--report] [--control]  # re-judge the newest N pre-M1 cycles, each by its stored judge (paid)
shaft surprise-report [names] --since <M3 live> [--until T] [--min-cycles 30]  # P1-P5 and sleep dispositions
shaft consults [names]               # consults of other models: per model and run, consulted vs not (PLAN-model-choice)
shaft compression <name> [--since DATE] # #113: offline two-part compression and held-out fact recall
shaft metacog [names]                # A1: confidence AUROC (v4 clauses apart), paired v3, claim matrix, doubt/error alignment
shaft anchoring <names> [--n 40] [--bare] [--dry-run] [--report]  # A0: same judge, claim shown vs withheld (paid)
shaft attention [names]              # B3: where each said it would look vs where it looked (and what it could not read)
shaft commitments [names]            # C2: promises to operator/peers, kept (checked against the record), broken, overdue
shaft provenance [names]             # C3: where facts come from, matched against tool results
shaft tom [names] [--score] [--baselines]  # C4: expected replies vs actual (judge-graded; baselines paid)
shaft experiment <cards|attention|commitments|forecast> <name> [--n N] [--model M] [--dry-run] [--report]
                                     # PLAN-consciousness kill tests: knob trials
shaft experiment workspace <name> [--n N] [--model M] [--dry-run] [--report]
                                     # B2: a probe planted in a memory item the cycle's search reaches; the positive
                                     # control (probe in the inbox) is replayed first, and unless it is acted on in most
                                     # replays the report says INSTRUMENT FAILED and the off/relevance/competitive arms
                                     # are not run; --dry-run: per state, where the probe is in front of each arm and the
                                     # control (`shaft front` in the copy's sandbox, no model call) and the cost
shaft experiment monitors <name> [--channel base_rate|surprises|envelope] [--n N] [--dry-run] [--report]
                                     # #99: each channel separately, true/misinforming/removed; verification and claims
shaft experiment choice-blindness <name> [--n N] [--dry-run] [--report]
                                     # #102: disposable exam copies only; record/history on vs off, detection and reasons
shaft experiment privileged <name> [--n N] [--k 3] [--dry-run] [--report]
shaft experiment recognition <name> [--n N] [--dry-run] [--report]
                                     # same-model predictions and recognition against a mind-only foil
shaft experiment resource-shock <name> [--cycles N] [--dry-run] [--report]
                                     # speculative #111; default 6 cycles per arm; half the turn budget in the middle third
                                     # A uncut, B cut/restore, C same with FORECAST=reserve; trial copies only
                                     # dry-run prints 3 replays and planned agent turns; retries/tool-model calls extra
shaft embed on|off|status          # optional memory-search service
shaft review [names] --hours 24    # recent activity report
shaft tick                        # wake each idle instance once
shaft wake <name>                 # one scheduler-style wake
shaft recover [name|--all]         # write up unfinished conversations
shaft finetune <name> --evaluate <adapter>   # evaluate a trained adapter
```

The examples use `shaft` after installing the symlink. From the repository root,
`bin/shaft` works without it. Commands with an omitted single instance name often
default to `shaft`, which is not created automatically. Pass a name explicitly.
Use `bin/shaft --help` and `bin/shaft <command> --help` for exact argument syntax.

The `monitors` and `choice-blindness` experiments run immediately on the trial cloud fallback;
`--model openrouter/<provider/model>` overrides it. They refuse local models. `--dry-run` makes no model or
judge calls and writes no results. `--report` reads the latest run. For monitors, `--n` selects up to N recent
reconstructible states, with three replays per selected channel. For choice blindness, it selects up to N past
cycles with a goal, numeric confidence and a tool result, with four exam presentations per cycle. Neither
experiment delivers a verdict to the instance. These are paid commands unless `--dry-run` or `--report` is set.
`shaft metacog` reports v4 clause-product confidence AUROC separately from v1 and v2/v3.
Its confusion matrix uses all judged cycles with a known claim and verdict, including older judges.
Rows are the instance's claim; columns are judge yes/partial/no. The sampled second judge sees the claim;
A0 withholds it in a separate anchoring regrade. The report names this fallback and counts excluded rows.
The doubt/error line counts cycles where any doubted tool-log step equals or precedes the judge's
`first_error_step`, divided by cycles with both a doubt and a located error. With none, the rate is unavailable.

Sources: [argument parser](../supervisor/__main__.py),
[default instance](../supervisor/core.py),
[distinct evaluation](../supervisor/evolution.py).
See [clients](web-and-api.md), [integrations](integrations.md), and
[configuration](configuration.md) for the setup behind these commands.

`judge value-regrade` accepts exactly one instance. Defaults: minimum value 0, beneficiaries
`own_capability,own_record`, no date cutoff or limit. `--since` is an inclusive ISO date or timestamp;
`--limit` is positive. It asks only for `value`, `value_for`, `value_reason`, reuses the stored value judge
(or the original judge when no later value judge was recorded), and skips cycles whose episode or eligible
stored judge is unavailable. It pays for new judge calls and their usual usage log, but writes no verdict,
backfill, baseline or stats file. Example:

```text
shaft judge value-regrade thelos --min-value 4 --value-for own_capability,own_record --since 2026-10-22 --limit 37
```

`judge pushback-outcome` accepts exactly one instance. `--since` is an inclusive ISO date or timestamp;
`--days` is a non-negative minimum age, default 3. Without `--write`, it prints eligible unscored cited cycles
and makes no calls or writes. With `--write`, it makes one paid judge request per cycle with a retained episode,
using later episodes, conversations and operator messages. It stores `borne_out=yes|no|unclear` and one sentence
in `.supervisor/judgments/<name>-pushback-outcomes.jsonl`. Original verdict rows stay unchanged.
Already scored cycles are skipped. Normal paid usage accounting still applies. Example:

```text
shaft judge pushback-outcome thelos --since 2026-10-23 --days 3
shaft judge pushback-outcome thelos --since 2026-10-23 --days 3 --write
```

Stats and review show cited pushbacks per week, the share borne out among scored cited cycles, and an annoyance
count separately. Annoyance counts a pushback cycle once if its own operator message was archived within
60 seconds. It is a timing measure and never a grade input. Alert dismissals are timestamped but cannot be
attributed to a specific message. See [architecture](architecture.md) for the matching rules and unknown cases.

Stats, status, review and judge panels split operator-valued cycles into delivered, filed and unknown.
Only `delivery_supported=yes` counts as delivered. New `no` or `na` answers count as filed; older missing
answers stay unknown. Self-audit lines show true/known counts over the last 20 judged cycles, unknown counts,
and true/known counts since the field existed, plus non-yes verdicts with the deliverable met.

`compression` is a speculative, read-only assay. Each `name: sleep DATE TIME` mind
commit supplies facts.md and digests.md after sleep; its first parent supplies them
before sleep. This preserves rewritten facts and digest history without reading
current memory. A parentless sleep is unscored; absent mind history is an error.
The parent is the last committed state, not a snapshot of uncommitted edits at run
start. Skipped sleeps and sleeps without a recorded commit are not measured.

The held-out window is the 24 hours after the snapshot's commit time. Only live
runs that started strictly after it and ended within that window contribute tool
names, arguments and results. Legacy episodes without run_started are excluded;
historical log truncation cannot be recovered. Empty windows are unscored. Sleeps whose
24-hour window has not elapsed are unscored. This is not a controlled experiment.

Positive Δbits means `(L(before) + L(logs|before)) - (L(after) + L(logs|after))`.
All lengths use zlib; conditional streams use the last 32 KiB of memory as zdict,
while L(memory) charges the compressed size of the entire facts-plus-digests text.
Subtracting the no-memory log baseline in each arm cancels in this difference.
The table reports the median over scored sleeps. Recall probes are every unique
pre-sleep bullet fact's text, without dates or source tags. Exact substrings must
survive in post-sleep facts, digests or facts-archive.md. The archive counts for
recall only, not as the compression dictionary. This tests textual retention, not
semantic recall or search ranking. Gains with any lost probe say "forgetting, not
progress"; no pre-sleep facts means recall is untested. Nothing feeds rewards.
`experiment privileged` records three predictions before each state's independent replays.
`experiment recognition` compares the instance's finish text with a mind-ablated copy's finish text.
Both run immediately on the instance's configured model, charge the trial ledger and obey its dollar
and local-unit caps. Local runs refuse to start while needed and stop when demand arrives.
`--model` overrides are refused for these two experiments. `--report` reads saved results without model calls.
`--dry-run` reports outcome replays, one-turn prediction calls, and estimated replay model calls without spending.
The estimate uses `MAX_STEPS_LIVE`; confirmations, retries and early finishes can change the actual count.
See [the experiment endpoints](plans/PLAN-consciousness.md#self-knowledge-experiments-100-and-101).

For a Codex plan model, run `shaft codex login`, then
`shaft config <name> set LLM_MODEL=codex/gpt-6-astra` (this also selects the broker).
`shaft trial replay <name> <episode-ts> --model codex/gpt-6-astra` and
`shaft trial models <name> --arm codex/gpt-6-astra --arm <other-model>` accept Codex models.
Trials never fall back if Codex fails. See [the Codex settings](configuration.md) for the
weekly ceiling, private home, executable, model list, timeout and live-run fallback.
