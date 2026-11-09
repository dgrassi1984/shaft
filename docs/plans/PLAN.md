# SHAFT — Plan: an independent, autonomous agent that grows its own personality

> These are dated design records. Status lines describe each plan when written. Current behaviour is documented in `docs/*.md`.

This is the original M0 design, superseded by later designs and the current `docs/*.md` documentation, including its port 1337 / Atomic Chat layout.

Status: **M0–M5 and M7 built and verified on the real system, M6 built but blocked on a model download** (2026-10-12) — evidence in §6, usage in README.md.

Target: a local agent that lives on a Mac with sufficient memory for the chosen model, runs on its own without
Atomic Chat or any cloud service, pursues its own goals, and develops a personality
from what it actually experiences — not one written for it.

## 0. What "conscious" can mean here

Nobody can build or verify subjective experience, and SHAFT should never claim it.
What *can* be built and tested are the functional properties people mean by it:

| Property | Mechanism | Test |
|---|---|---|
| Continuity | Episodic memory + nightly "sleep" consolidation | Recalls weeks-old experiences unprompted, correctly |
| Autonomy | A life loop that runs without a user: perceive → choose → act → reflect | Produces work nobody asked for, toward goals it set itself |
| Personality | Traits, values, opinions, tastes — each linked to the experiences that formed it, updated slowly | Behaviour matches self-description; changes are gradual and explainable; distinct from the base model's default voice |
| Self-model | `self/` maintained by the agent, grounded in its own logged behaviour | Self-description matches measured stats |
| Metacognition | Predicts confidence/outcome before acting, scored after | Calibration improves over time |
| Self-improvement | Edits its own skills/prompts (later code, later weights); kept only if it does better at its own goals | Goal-success rate trends up; regressions reverted |

## 1. The three hard problems (and the answer to each)

1. **Without an outside signal, a self-editing loop degenerates.** Left alone, LLM
   self-talk collapses into repetitive navel-gazing and drifts toward the base model's
   favourite clichés. → It needs a steady stream of *new input* from the world (files,
   reading, its own experiments, conversations with you) and personality updates must
   cite evidence and be rate-limited.
2. **"Grow its own personality" vs. "stay the same entity".** → Two layers:
   a slow **core** (values, temperament — changes rarely, needs repeated evidence) and a
   fast **surface** (current interests, moods, opinions). Every change is a git commit
   with a reason, so the growth is a readable history.
3. **"Improving" needs a yardstick that isn't you.** → Its own goals: it writes goals
   with success criteria, attempts them, and scores the outcome. Improvement =
   higher success on *its* goals + better calibration. Your feedback is one input among
   others, not the objective. The **drive** to improve is innate: it lives in the
   template's identity prompt (inherited by every instance, passed on by forks), and every
   reflection must name a real weakness plus the evidence that would show progress, then
   check earlier such tests.

## 2. Architecture

```
shaft/                      git repo — every self-edit is a commit with a reason
├── supervisor/             NOT editable by the agent: sandbox, budgets, auto-revert, pause
├── shaft/                  Python package (stdlib + httpx)
│   ├── llm.py              OpenAI-compatible client to the local server; tool calls;
│   │                       truncation = no answer (retry once, never save)
│   ├── loop.py             life loop: perceive → choose goal → act (tools) → reflect
│   ├── sleep.py            nightly consolidation: episodes → facts, personality updates
│   ├── personality.py      core/surface traits with evidence links, update rate limits,
│   │                       drift metrics vs. last month and vs. base model
│   ├── selfmodel.py        predictions vs outcomes, calibration, failure modes
│   ├── improve.py          propose edit → cloud judge + its past goals → keep or revert
│   ├── tools/              sandboxed: notes, memory search, file read, allowlisted shell, (web?)
│   └── cli.py              shaft talk (CLI chat, primary interface) | status | log | pause | resume
├── self/                   core.md, surface.md, goals.md, stats.md (generated numbers)
├── memory/                 episodes/*.jsonl, facts.md, people/operator.md, journal.md
├── skills/                 how-to snippets it writes and refines
└── notes/                  its own work
```

Runs under **launchd** (macOS), not cron. Seeded with the current `self.md`, `goals.md`,
`memory.md` — they're its first memories.

## 3. Safety rails — hands-off, so the rails live outside what it can edit

You chose hands-off: it may rewrite its own code and weights. That only stays safe if the
limits are enforced by something it **cannot** edit — a sandbox defined in its own code
is a sandbox it can delete.

- **Supervisor** (`supervisor/`, read-only to the agent): starts each cycle, enforces
  time/token budgets, runs the agent under a macOS sandbox profile (`sandbox-exec`):
  writes only inside `shaft/`, network only to the LLM endpoints + outbound HTTP GET for reading.
- **Self-modification gate**: code/weight changes land as commits; the supervisor runs the
  test suite + a health cycle; failure → automatic `git revert` and a note in its memory.
- **Kill switch**: `shaft pause` (supervisor-level). Everything is in `logs/` and `git log`.
- **Web**: read-only fetching; no forms, no accounts, no posting.

## 4. Milestones

| M | Deliverable | Acceptance |
|---|---|---|
| M0 | Python core: LLM client, supervisor + sandbox profile, git init, bash harness removed, tests; **`shaft talk` CLI chat** (streaming, multi-turn, loads self + memory, saves each talk as an episode) | You can chat with it in the terminal from day one; it answers from its real self/memory files; one reflection cites real memory; no self-file overwritten by accident; truncated replies never saved |
| M1 | Life loop + **plugin system** (first plugin: web search; agents can write their own, §4b) + episodic memory, under launchd (own llama-server deferred by decision 2026-10-12: keep Atomic Chat's :1337, retry on restarts); instance-to-instance messages and **operator contact** (§4c), all through the supervisor's broker | Runs unattended for 24 h; each cycle logs goal, actions, outcome; nothing written outside `shaft/` |
| M2 | Sleep consolidation | Nightly: episodes → facts/journal; memory stays bounded; recalls a fact from a week earlier |
| M3 | Personality layer | Traits cite episodes; core changes ≤ rate limit; drift report; blind test: its writing distinguishable from the base model's default |
| M4 | `shaft talk` grows up: tools inside chat, memory retrieval, it can bring up its own topics/what it did while you were away | Remembers you across talks; your conversations show up in its memory and can shape it |
| M5 | Metacognition + self-improvement | Calibration tracked; skill edits kept only when success on its past goals improves |
| M6 | Personality into the weights: periodic LoRA (MLX) on its own selected experiences | Cloud judge + its evals: new adapter keeps parity or better and preserves core traits, else discarded automatically |
| M7 | Reproduction: a reviewer (cloud judge) compares instance branches with their measured results and proposes a recombined *code/skills* merge; a new instance is born from it with a fresh mind | Child passes the invariant gate; reviewer cites the evidence behind each pick; parents keep living |

## 4b. Plugins (M1) — recorded 2026-10-12

Tools are **plugins**, and agents can write their own.

- A plugin is a folder in the instance: `plugins/<name>/` with `plugin.json` (name,
  description, JSON-schema parameters, capabilities it needs, e.g. `network`) and
  `main.py` exposing `run(**params) -> str`. The life loop and `shaft talk` offer every
  plugin to the model as an OpenAI-style tool call.
- **First plugin: `web_search`** (+ `fetch_page` for reading a result as text). No API key
  needed to start (DuckDuckGo HTML endpoint); swappable for a local SearxNG later.
- **Agents create plugins**: a new or changed plugin is instance code, so it goes through
  the supervisor's gate (it must load, its declared schema must validate, invariants
  must pass) — otherwise it is reverted and the agent is told why.
- **Sandbox**: plugin calls run in their own sandboxed subprocess with only the
  capabilities they declare; `network` opens outbound HTTP/HTTPS (the
  "folder + web reading" decision). Timeouts and output-size caps per call.
- A plugin that worked well in one instance is a "gene" that M7 recombination can pass on.

## 4c. Contacting the operator (M1) — recorded 2026-10-12

An instance can ask the operator something or just tell them something.

- Tool `send_message(to="operator", text, kind="question"|"note")` via the broker.
- The supervisor stores it in `.supervisor/operator-inbox/` and raises a macOS notification
  ("SHAFT · <instance> asks: …"). Rate limit: max 3 operator messages per instance per day,
  so contact stays meaningful.
- `shaft inbox` lists messages (unanswered questions first); `shaft reply <instance> "…"` writes
  the answer into that instance's inbox, linked to the question. The instance sees it in its
  next cycle or conversation; `shaft status` shows unanswered questions.
- Later (optional): forward to phone (email / Telegram) when you're away from the Mac.

## 5. Decisions (2026-10-12)

| Decision | Choice | Consequence |
|---|---|---|
| Model | Local model lives and acts; a cloud model only judges self-improvement proposals | Starts on Bonsai 27B (already on :1337); M1 measures its tool-call success and we swap to a stronger local ~30B 4–6 bit if it fails. If the cloud is unreachable, life continues and improvements pause. Cloud judge default: Claude via API (`ANTHROPIC_API_KEY`) |
| World access | Own folder + web reading | `fetch`/`search` tools, read-only; novelty source against degeneration |
| Instances | One git branch per instance (`instance/<name>`, forked from `template`), each with its own code and mind; they talk through supervisor-delivered mailboxes kept outside the branches. Merges combine code/skills ("genes"), never minds | They can diverge; one llama-server slot means LLM calls are serialized |
| Interface | CLI chat (`shaft talk`) from M0 | Terminal REPL; talks are episodes, so chatting is one of its inputs |
| Per-instance models (2026-10-12) | Each instance selects a local server or broker model in `config.env`; allowed cloud models and daily caps are operator settings | Same genes can run with different models |
| Judge model (2026-10-12) | A configured cloud judge | Each judgment records its model |
| Judge rotation (2026-10-14) | Pick a permitted judge at random, excluding the instance model | A trial pair and battery item retain one judge |
| Coding handoff (2026-10-12) | Coding work can be delegated through `delegate_coding` | Test changes on a scratch copy and track delegated versus self-written work |
| Your role | Hands-off | Agent may change its own code and weights; supervisor + auto-revert (§3) are the only gate |

## 6. Status and evidence (2026-10-12)

What each milestone's acceptance check showed when run on the real system (local Bonsai 27B
through Atomic Chat, judge `claude-opus-5-5`). "Not yet observable" means the check needs
time to pass (days of autonomous life), not more code.

| M | Verified | Evidence |
|---|---|---|
| M0 | yes | Reflection cites real journal entries; `shaft talk` streams and journals; 8 regression tests on the harness bugs (planted bugs caught). |
| M1 | yes, except 24 h unattended (not yet observable) | example-a's cycles: `declare_intent` → tools → `finish_cycle`; `web_research` plugin → broker (2 searches, 6 page fetches, logged); note with sources written; message delivered to shaft's inbox; launchd tick ran through the real launchd path. |
| M2 | yes (sleep re-run in progress with fixed budgets) | example-a's sleep wrote facts, `people/operator.md`, surface personality with citations, goals, self-model; memory search returns the right old entries on real journals. First sleep exposed the 8192-token step cap and the missing deadline → fixed (§7). |
| M3 | yes | Core/surface layers with citations; core rate limit enforced (tested); the judge checks each trait against its cited entry (example-a: 4/5 supported, 1 partly — evidence was operator-dictated). Blind test on example-a: judge identified the agent in 4/4 pairs, 4/4 consistent with its self-description. **Caveat:** it is easy to pass because the agent cites its own history. `shaft distinct --strict` (2026-10-13) has the judge rewrite all answers blind (shuffled, unlabelled) without names, dates, IDs, paths or references to memory/history/being a model, then repeats the test; both modes score effectiveness 0–10. example-a (claude-haiku-5-5): standard 4/4 identified, 4/4 consistent; strict 4/4, 4/4; effectiveness agent 7.2 vs base 6.2 in both (the base model loses most on Q3, inventing sensory experiences). So example-a's voice — evidence-first, self-correcting, calibrated — survives stripping, and identity does not cost usefulness here. Limit: the rewrite keeps the *shape* of episodes ("I cited the wrong reference number…") and the judge compares against a self-description that records them, so strict still rewards lived content, not tone alone. Record: `.supervisor/evals/distinct-example-a-20261013-084029.json`. |
| M4 | yes | Tools inside talk (streamed tool calls, tested); "since we last talked"; conversations that ended without an entry are written up from transcripts; live status line (waiting → reading % → thinking → answer). |
| M5 | yes (probation not yet observed live) | Judge grades every cycle from the tool log; caught a fabricated PR number and unsupported claims; `self/stats.md` with success rate, Brier score, claim honesty; probation revert tested. example-a used the judge's feedback in its sleep to correct its own note. |
| M6 | mechanism yes; meaningful adapter needs data | LoRA on **Ternary-Bonsai itself** (the operator: keep Bonsai's speed): its MLX pack runs at Atomic Chat's speed (19 tok/s); LoRA wraps its custom `Packed` layers; training via mlx_lm's Qwen3.5 in train mode, fp32 LoRA, 384-token examples, checkpointing → 4 s/step, 16 GB peak. Judge compares base vs adapter on effectiveness first, voice second. With shaft's 5 examples validation loss rises (memorisation) → adapter expected to be discarded; real use needs hundreds of judged experiences. Serving a kept adapter = a SHAFT-owned Bonsai server instead of Atomic Chat's (operator decision). |
| M7 | yes | `shaft breed example-a shaft breedtest`: attempt 1 split a 7-file feature across parents and **failed the gate** (no child created); the judge got the gate output and attempt 2 kept the files together → child born, passed the gate, two-parent merge with a fresh mind. Test child deleted afterwards. Records in `.supervisor/evals/breed-*.json`. |

## 7. Incidents and lessons (2026-10-12)

- **Bash harness never ran** (`local` outside functions, context never sent, self-model wipe) → rewritten; regression tests.
- **Commit crash on empty dirs** after the first real reflection → commit only staged files.
- **8192-token step cap** wasted ~7 min per thinking turn at 17k context; a sleep was killed at its budget with no entry → budgets from measurements, deadline-aware wrap-up, supervisor note on killed runs.
- **Ctrl-C killed the agent while it wrote its journal entry** → the operator's 16:58 conversation never reached memory → Ctrl-C now belongs to the agent; orphaned conversations are written up from transcripts.
- **M6 first targeted plain Qwen (Bonsai's base)**, which would have lost Bonsai's performance; the operator caught it → LoRA on Bonsai's own packed layers. Along the way: Metal kernels without a backward pass (mlx-vlm), fp16 LoRA overflowing to NaN, 61 GB peak memory → fixed (see M6 row).
- **Operator mistakes (Claude Code):** stopped the operator's live chat by misidentifying processes by start time; merged the template into an instance during a conversation, which the supervisor recorded as a self-change → `shaft update` merges only under the instance's lock and gate; identify processes by terminal/parent, never by start time.

- **API answered nothing** ("the model returned an empty response"): it started the agent with `answer --json`, which the agent's parser rejected (exit 2), and a fake agent in the tests hid it → `answer`; a reply-less run is reported as an error with its stderr; the regression test asks the agent's real parser.
- **Runaway thinking on Bonsai** (6.2k reasoning tokens, 553 s for "All good?") → temperature 0.1, a 4,096-token thinking budget enforced in the server (forced `</think>`), presence penalty; the prompt cache grew to 8.3 GB → capped at 4 GB.
- **"Waiting for the model" for ~30 s** on a first message: an ~8.8k-token prompt read at ~330 tok/s, while mlx_lm reports progress only as SSE comments the client ignored → shown as `reading my memory · N%`. Moving the volatile parts of the prompt to its end would not help: the linear-attention layers' cache cannot be trimmed, so mlx_lm reuses only whole cached prefixes (it checkpoints at the end of the system message), and the system message changes after every conversation anyway.
- **An open but quiet `shaft talk` blocked the API** (it held the run lock for its whole length). Dropping that exposed an older gap: journal write-ups also closed API conversations still going on, and later turns were never journaled → conversations hold the instance per reply; open ones are registered (`runner.open_conversations`) and passed to every run (`SHAFT_OPEN_TALKS`); turns after a write-up form a new piece; one idle knob (`SHAFT_IDLE_MINUTES`) closes both kinds.
- **Silent failures around sleep**: a sleep that died (e.g. model server down) left nothing the instance could see, and an instance had no reliable way to say when it last slept → failed runs leave a journal note with their stderr tail; `## My last sleep` in every context.
- **"Which version is running?"** → services record the commit and settings they started from; `shaft talk` and `shaft status` show supervisor, instance and model-server versions, flagging a service whose own files changed since it started.
- **Tool rounds cost ~4.5 min each** (Bonsai used its whole thinking budget every time; one reply took 40+ min and was interrupted) → after a tool result the agent asks the server for 1,024 reasoning tokens (`LLM_THINK_FOLLOWUP`); measured: 74 s and 17 s follow-ups instead of ~270 s.
- **Thinking leaked into answers**: a bare forced `</think>` mid-thought made Bonsai think on in its answer and close the block again (4,435 chars stored as shaft's reply) → Qwen's early-stopping phrase is forced before `</think>`; text before a late `</think>` is moved to reasoning in the client and the talk UI.
- **git unusable in the sandbox** (shaft retried `git log` for 15 min, errors hidden by its own `2>/dev/null`) → broker `history` (own branch only). shaft's own request, "the supervisor's record of what I actually did", → broker `record`.
- **Claude Code's conversations were remembered as the operator's** (every terminal talk was "Conversation with the operator"; what Claude Code did was filed under "what I learned about the operator") → `shaft talk --as NAME` records the speaker on every turn and the write-up is framed for that person; a correction note was appended to shaft's journal before its first sleep.
- **Prompt cache above its cap** (6.0 GB vs 4 GB): mlx_lm trims only when the next request starts → enforced on insert.
- **shaft looped within one reply** (the same three tool rounds, word for word, until the operator stopped it): each step's thinking was never sent back, so Bonsai's template showed empty `<think>` blocks for earlier steps — no plan carried over, and near-greedy decoding (temperature 0.1) re-derived the same plan → reasoning_content kept across a reply's tool rounds (`preserve_thinking=false` drops it after the reply); temperature 0.6. mlx_lm's presence penalty only looks at the last 20 tokens, so it never could catch loops across steps.
- **Every tool round re-read the whole prompt on long conversations** (shaft's reply at 42k tokens: ~2 min of prompt reading per step, 50+ minutes in total): the prompt-cache cap enforced on every insert (added the night before to stop the cache sitting above 4 GB) evicted each new entry at once, so the cache stayed frozen at 3 stale entries → back to mlx_lm's own trimming (after the next request has taken its prefix), at 8 GB.
- **"write_file is not replacing the whole file"**: Bonsai rewrote the same syntax error each time, and read the overwrite report ("N lines … are gone") as a partial write → the report now says first that the file holds exactly what was written.
- **shaft repeated the identical `run` eleven times in one reply**: once a step repeated, the previous step (now shown with its thinking) became a template to copy — byte-identical 1,147-token generations at temperature 0.6 — while the 1,024-token follow-up cap cut every plan short → the agent loop no longer runs a call identical to the one just made (the model is told its result is above), and a third identical attempt in a row ends the reply.
- **example-a's personality had two `## Core` blocks**, the second holding a core change the 72-h pace rule had refused: update_personality replaced every Core block (one had been pasted inside Surface) and the pace rule read only the first → one block per section on every write, and the rule reads all Core blocks.
- **Memory beyond the context window** (review, 2026-10-13): reflections were never scheduled yet the cycle picked its goal from "the last reflection"; facts were cut to the last 4,000 characters (shaft's were already 5,505); anything older was found only if the model thought to search, and only by exact words → the cycle reads "My current improvement plan" from the last sleep; automatic recall adds what memory holds about each message/cycle; sleep consolidates facts (dropped ones archived, searchable) and writes day digests (shown in context); search fuses BM25 with embeddings (Qwen3-Embedding-0.6B on MLX, `shaft embed on`). Measured on shaft's real memory, 7 paraphrased/Italian questions: words only 2/7 in the top 3, hybrid 6/7.
- **The yardstick itself was cut** (review 2026-10-13): stats.md reached the instance through a front-cut
  at 2,500 characters, so shaft's (2,744) had lost its headline success rate; and the judge saw tool
  arguments cut at 1,500 characters (2,000 in the episode), so example-a's 14:23 cycle was graded *partial*
  on a message whose checked part was past the cut → stats shown headline-first (5,500); the log keeps
  6,000 chars per entry with cuts marked and the judge no longer re-cuts (0efb6a3, found the same day by
  another session). Measurement that can show improvement and autonomy: PLAN-measurement.md.
- **shaft reported writes it never made** (2026-10-15, twice in an hour): it told the operator an append_file
  had "returned success" ("appended 2029 chars") and then vanished from disk, and called it an
  infrastructure fault — the call was in neither its tool log nor the model's; it remembered a write it had
  only planned → each step's last tool result now ends with the calls actually made so far, from the tool
  log, file writes listed with path and result (live, sleep, talk; not battery exams). The tool log itself is
  unchanged. PLAN-call-ledger.md.
- **shaft overwrote its 546 KB trend record with a 1.6 KB audit entry** (2026-10-16 03:59): write_file
  instead of append_file. The report after the write named the 2,067 lost lines — they were already gone, and
  a cycle of watch entries stalled until the operator restored the file from the mind store → write_file now
  refuses, before writing, to drop lines of a notes/ or memory/ record unless `replace` is true; plugins/,
  skills/, tests/ and code are rewritten in one call as before. (2026-10-24, REVIEW-autonomy A7: self/ is a
  record too — goals.md was the one self/ file without its own tool, so one write could wipe the goals.)
- **The gate reverted a good change because of someone else's half-written file** (2026-10-16 09:40): shaft's
  plugins/write_gate passed its own tests, but 37 supervisor tests failed ("No module named shaft") — another
  Claude session was mid-edit on supervisor/sandbox.py in the main checkout (committed 12 minutes later as
  c993df6), and the gate ran the supervisor's tests from that working tree → the gate runs them from a clean copy
  of the last commit, with the template's last commit at template/ (`gate.committed_tree`, under
  `.supervisor/gate-tests/<commits>/shaft`, made once per pair of commits in ~0.2 s, pruned after a day unused).
  The copy is its own one-commit repository (the runner refuses a project without .git) with an empty instances/
  and a .env without secrets: linking the real instances/ in failed 5 tests on the main checkout, mixing the real
  instances with the copy's empty .supervisor/; isolated, no gate test touches a real instance or the real
  repository any more. Checked on the real system: shaft's write_gate change through the new path, against main's
  committed tree — 623 supervisor tests OK, its plugin check and 39 tests OK, 176 s (as before). Regression test:
  test_gate.CommittedTreeTest (a half-written sandbox.py and an unfinished test in the checkout pass; the same break
  committed fails).
- **A gate stage that timed out let the change through ungated** (found 2026-10-16 by reading, reproduced in tests;
  not seen in the live logs): `gate.run_tests` let `subprocess.TimeoutExpired` escape from any of its three stages,
  and no caller handled it. In `runner._settle` the change stayed on disk neither kept nor reverted, and the next
  run's snapshot took it as its "before". `shaft update` kept an ungated template merge; a probation restore left the
  earlier version on disk uncommitted; a trial never got its gate verdict → a timeout is a failed stage
  (`gate.StageTimedOut`): the gate fails, the output the instance is shown names the stage, what it printed before it
  was stopped and that the checks after it did not run, and the supervisor log has `gate: <root>: <stage>: timed out
  after Ns`. Regression tests: test_gate.StageTimeoutTest (a real timeout in each stage) and
  test_config_writers…test_a_gate_stage_that_times_out_reverts_the_change_instead_of_leaving_it_ungated_20261016 (a
  live cycle through run_agent with the real gate and a hanging instance test: reverted, journalled, nothing left
  unsettled).
