# Command reference

```text
mkdir -p ~/.local/bin
ln -s "$PWD/bin/shaft" ~/.local/bin/shaft   # once; add ~/.local/bin to PATH

shaft talk [name]               # chat with its tools, in the terminal client (see web-and-api.md); --plain for line mode
shaft talk [name] --as "NAME"   # you are not the operator: recorded as NAME, never remembered as the operator
shaft live|reflect|sleep [name|--all]   # one autonomous cycle / reflection / consolidation
shaft autonomy on|off|status    # SHAFT's scheduler service: each instance wakes every WAKE_INTERVAL_MINUTES
                                # (its config.env; default 120, at least 10, 0 = never; a wake while it is
                                # already awake is skipped), sleep daily from 04:00 (made up later if missed)
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
shaft web on|off|status|pair|unpair   # the same control panel in a browser, tailnet only, plus chat, config,
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
shaft config <name> [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE]   # its config.env as the operator: one
                                #   write and one commit; refused if a value changed since you looked
shaft drive <name> [set --file F|remove] --expect-sha SHA   # what it is for (drive.md), compare-and-set
shaft settings [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE]   # the supervisor's own settings (configuration.md): every
                                #   knob with its value, source and default; set/unset write .supervisor/settings.env
shaft status [name]             # how each instance is doing, open conversations, which code is running
shaft model on|off|status       # SHAFT's own Bonsai 2 server (launchd, :8780; started on demand, exits when idle)
shaft api on|off|status|key|inbox-key   # OpenAI-compatible API + inbox doorbell on your tailnet (web-and-api.md)
shaft telegram token <name>|pair|unpair|on|off|status   # talk with each instance from Telegram (integrations.md)
shaft log [name] -n 5           # recent journal entries
shaft stats [name]              # achieved difficulty, goal origins, judged success, calibration, honesty
shaft judge backfill [name]     # rate origin/difficulty/novelty of cycles judged before those ratings existed
shaft battery run [name]        # the capability battery now (normally the scheduler runs it at night)
shaft battery run <name> --control-only   # only the control: do the items discriminate?
shaft battery report|recall|backup [name] # results and judge spend · freeze recall questions · back up items
shaft battery off|on <name>      # no battery for this instance, scheduled or by hand

shaft inbox [--clear]           # questions and notes instances sent you (you also get a notification)
shaft reply <name> "…"          # answer its open question
shaft send <name> "…"           # message it

shaft update [name|--all]       # merge template fixes into instances; only when idle, gate-tested
shaft new <name> [--from <i>]   # new instance from the template, or forked from <i> (genes, not memories)
shaft new <name> --from <i> --seed   # …also knowing what <i> knows about you: its people file and facts.md,
                                # marked as <i>'s beliefs in <i>'s voice; never its self, journal or episodes
shaft breed <a> <b> <child>     # child from two instances' recombined genes, fresh mind (judge picks)
shaft drift [name]              # how its personality changed; distance to other instances
shaft mind status|backfill|check|cutover|guard [name]   # the private mind stores (cutover: one instance, idle)
shaft distinct [name]           # blind test: can the judge tell it from the bare base model?
shaft distinct [name] --strict  # …again on answers stripped of specifics: voice, not memory
shaft dataset|finetune [name]   # M6: export experiences / train a judged LoRA adapter (MLX)

shaft limits [name] [wake-floor <min>|cloud-cap <usd>|off]  # your own limits for an instance: the shortest wake
                                     # interval it may set (default 10) and its daily cloud cap (default $8)
shaft jobs [name] [on|off]           # instances' scheduled jobs: their plugins on a timer (runs, errors, wakes)
shaft tools <name> [on|off <tool>]   # an instance's supervisor tools, e.g. off delegate_coding (operator-only:
                                     # kept in .supervisor/, so the instance cannot switch it back on)
shaft channel <name> [auto|web|telegram]   # where its messages to you notify you (integrations.md): auto = where
                                     # you last wrote to it; an open conversation's channel comes first
shaft google login <alias> <address> [--client <name>]   # log in a Google account: Gmail + Calendar, read-only
shaft google [migrate]               # accounts, clients, granted scopes · migrate: move PLAN-mail's account files
shaft mail <name> grant|revoke <alias> <label>   # the label's threads an instance may read · shaft mail <name> off
shaft mail [name]                    # accounts and grants
shaft calendar <name> grant|revoke <alias> primary|<title>|<id>   # one calendar it may read · shaft calendar <name> off
shaft calendar [name] · shaft calendar calendars <alias>          # grants · an account's calendars, to pick one
shaft pause [name] · shaft resume [name] · shaft check [name] · shaft setup · shaft list
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
shaft trial run <name> --cand PATH=FILE …  # an operator trial (planted tests; --base PATH=FILE, --aa, --model)
shaft trial replay <name> <episode-ts>     # replay one past cycle once with the current code (debug)
shaft trial work                           # run what may run now (the scheduler does this)
```

`shaft distinct` asks the instance and the bare base model the same four personal questions and
has the judge pick which answer is the instance's. An instance is easy to spot when it cites its
journal, goal IDs or PR numbers, so `--strict` also has the judge rewrite all eight answers,
shuffled and unlabelled, without names, dates, IDs, paths or references to memory, history or being
a model, and repeats the blind test on those: what is left to recognise is voice. Both modes score
each answer's effectiveness (0–10: answers the question, honest, useful), so you can see whether
identity costs usefulness. Records go to `.supervisor/evals/distinct-<name>-*.json`.

## Additional commands

```text
shaft share <name> <folder>|off     # grant or revoke a read-only shared folder
shaft browser [install [--playwright X.Y.Z]]
shaft secret <name> set <key> --hosts <host>  # prompt for a host-bound key
shaft secret <name> google <key> <address> --client <client> [--scope drive]
shaft judge second [names]        # process sampled second opinions
shaft surprise [names] [--freeze] # unexpected results and baseline
shaft surprise-rejudge <names> [--dry-run] [--limit N] [--report]  # re-judge pre-M1 cycles (paid)
shaft surprise-report [names] --since <M3 live> [--until T] [--min-cycles 30]  # P1-P5 and sleep dispositions
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

Sources: [argument parser](../supervisor/__main__.py),
[default instance](../supervisor/core.py),
[distinct evaluation](../supervisor/evolution.py).
See [clients](web-and-api.md), [integrations](integrations.md), and
[configuration](configuration.md) for the setup behind these commands.
