# Model bake-offs: ledger and how to run the next one

> Per-run reports are private records kept by the maintainer. Results replay cycles from one private instance and are not reproducible from this repository.

**Read this before choosing or bake-testing a model for an instance.** It lists every bake-off run so far, what each
found, and what the next one should reuse instead of paying for again. Add an aggregate comparison row for each new bake-off and keep its report private (§ After a bake-off).

The analysis and the decision behind the first round come from
(a private review record, not published); the code is
[supervisor/bakeoff.py](../../supervisor/bakeoff.py) (`shaft trial models`).

## Where things are

| What | Where | In git |
|---|---|---|
| All models ranked, with cycles, time and cost | [leaderboard.md](leaderboard.md) | yes |
| Per-run reports: arms, tokens, claims and verdicts | Maintainer's private records | no |
| The same bake-off in full: every replay's trace and diff, every judge verdict and its reasoning | `.supervisor/bakeoffs/<name>-<id>.json` on the supervisor host | no (private record, ~2–13 MB each) |
| OpenRouter candidate screen for thelos, prices of 2026-10-30 | [thelos-2026-10-30-candidates.json](thelos-2026-10-30-candidates.json) | yes |
| Public benchmark evidence (Artificial Analysis v5, vendor pages) | (a private review record, not published) | no |

`shaft trial models <name> --id <id> --report` prints a saved bake-off's report again (it reads the JSON; it runs
nothing). The `.md` files are that output.

## Ledger

MiMo = `openrouter/xiaomi/mimo-v2.6-pro`, DeepSeek = `openrouter/deepseek/deepseek-v4.1-flash`,
Sol = GPT-6.1 Sol. W–L–T compares the candidate against the baseline; a cycle counts only
when both judging orders agree.

| Bake-off  |  Cycles  |  Baseline  |  Candidates → W–L–T  |  Judge |
| --- | --- | --- | --- | --- |
| (a private review record, not published)  |  set F (12)  |  MiMo  |  Haiku 5.5 @medium 1–11–0 · Haiku 5.5 @xhigh 3–7–2 · DeepSeek 6–5–1  |  Sonnet 5.5 API |
| (a private review record, not published)  |  set R40  |  MiMo  |  DeepSeek 16–15–9  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12  |  MiMo  |  MiniMax M3 9–3–0 · Hunyuan HY3 4–5–3 · MiniMax M3.1 Flash 4–6–2 · Step 3.7 Flash 2–9–1 · Nemotron 3.5 Super 0–11–1 · GLM-4.7 0–11–1  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12  |  MiMo  |  Sonnet 5.5 (Anthropic API, effort medium) 0–10–2  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12  |  MiMo  |  Gemini 4 Argon (OpenRouter → Vertex) 9–2–1 · Haiku 5.5 @xhigh 8–4–0 · Haiku 5.5 @medium 1–8–3  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R40  |  DeepSeek  |  Haiku 5.5 @xhigh 15–20–5  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R40  |  DeepSeek  |  GPT-6.1 Sol 27–10–3 (p = 0.004) · GPT-6.1 Terra 15–18–6, 1 unjudged  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12  |  Sol via Codex, handicapped (read-only path)  |  Hy4 10–1–1 · Particle-1 9–3–0 · Mercury 3 7–5–0 · Kimi K3.1 Pro 6–4–2 · GLM 5.3 6–5–1 · GLM 5.3 Flash 6–5–1 · Astra via Codex 3–4–5 · Grok 4.7 4–6–2 · Luna via Codex 0–11–1  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12, no replays  |  DeepSeek (stored runs)  |  Hy4 6–4–2 · Particle-1 6–5–1 · Kimi K3.1 Pro 5–5–2 · Mercury 3 4–6–2 · Grok 4.7 3–8–1 · GLM 5.3 2–8–2 · GLM 5.3 Flash 2–10–0  |  Opus 5.5 subagents |
| (a private review record, not published)  |  set R12, Codex prompt fixed  |  DeepSeek (stored runs)  |  Sol via Codex 8–4–0 · Astra via Codex 8–4–0 · Luna via Codex 0–12–0  |  Opus 5.5 subagents |


### Cycle sets

The cycle sets come from the private instance after its goals-cap change. Private reports list the cycle timestamps.

- **R40**: 40 cycles evenly spaced over the 197 not used before; their judge outcomes match that period's mix.
  For confirmation runs.
- **R12**: 12 of R40, evenly spaced. For screening: a filter, not a verdict.
- **F**: 12 failure-heavy cycles (8 the judge marked partial, 4 rated value 4). Leans to cycles that went wrong, by
  design.

### What the models did (thelos's loop, its prompts as of 2026-10-30; the GPT-6.1 rows as of 2026-11-04, the 11-05 models as of 2026-11-05)

| Model (path) | $/cycle measured | Result | Status |
|---|---|---|---|
| GPT-6.1 Sol @medium (OpenRouter → Azure, then OpenAI) | 0.570 | better than DeepSeek on R40 (27–10–3, p = 0.004), claims supported 80/80 | best confirmed result |
| GPT-6.1 Sol through the Codex plan, prompt fixed | 0.125 est. | 8–4–0 vs DeepSeek on R12, claims supported 24/24, 132 s; first on the [parity score](leaderboard.md#parity-score) | R12 only; re-test on R40 |
| GPT-6 Astra through the Codex plan, prompt fixed | 0.630 est. | 8–4–0 vs DeepSeek on R12, claims supported 24/24 | no reason to prefer it over Sol at five times the list price |
| GPT-6 Luna through the Codex plan, prompt fixed | 0.008 est. | 0–12–0 vs DeepSeek on R12 | settled: no |
| Hy4 (Tencent) | 0.211 | 6–4–2 vs DeepSeek on R12, claims supported 46/48; slow (794 s) | candidate if speed stops mattering |
| Particle-1 | 0.030 | 6–5–1 vs DeepSeek on R12 at 60% of its cost, but claims supported 27/48 | cheap and capable, reports unreliably: do not run unsupervised |
| Kimi K3.1 Pro · Mercury 3 · Grok 4.7 | 0.022–0.575 | no clear difference from DeepSeek on R12, claims supported 67–73% | no |
| GLM 5.3 · GLM 5.3 Flash | 0.012–0.120 | worse than DeepSeek on R12 | settled: no |
| GPT-6.1 Terra @medium (OpenRouter → Azure, then OpenAI) | 0.486 | = DeepSeek on R40 (15–18–6) at 8.6× the cost, claims supported 78/79 | settled: no |
| DeepSeek V4.1 Flash (OpenRouter, Novita/DeepInfra) | 0.023–0.056 | = MiMo on R40 and F at ~53% of the cost; lost to Sol on R40 (claims supported 119/159 in that run) | reference baseline |
| Hunyuan HY3 | 0.034 | = MiMo on R12 | equal alternative at the same price |
| MiniMax M3 | 0.152 | better than MiMo on R12 (p = 0.07) | **needs R40 to confirm** |
| Gemini 4 Argon (OpenRouter → Vertex) | 0.325 | better than MiMo on R12 (p = 0.03) | higher per-cycle benchmark cost |
| Haiku 5.5 @xhigh (Anthropic API) | 0.049–0.053 | 3–7–2 on F, 8–4 on R12, lost to DeepSeek 15–20–5 on R40 | settled: not better than DeepSeek, 2.3× the cost |
| Haiku 5.5 @medium | 0.014–0.020 | worse twice (claims little) | settled: no |
| Sonnet 5.5 @medium (Anthropic API) | 0.284 | 0–10–2: stops after 3–6 steps | settled at medium; xhigh not run |
| MiniMax M3.1 Flash | 0.039 | 4–6–2, claims supported 18/24 | no |
| Step 3.7 Flash · Nemotron 3.5 Super · GLM-4.7 | 0.037–0.089 | worse | no |

Public benchmark scores did not predict these results: what won was diligence (checking before claiming, reading the
source), which favours models that take more steps.

## Before the next bake-off: what to reuse

1. **Results above.** Do not run a model, effort and path again on the same instance unless one of these changed: the
   model's version, the instance's prompts or loop, or the budget (which changes how a verdict's cost reads). Say
   which one when you re-run. A verdict holds for the instance code it ran (each report names its `code_rev`).
2. **Baseline:** use the instance's current model unless the operator names another.

3. **Cycle sets.** To compare with the rows above, use R12 (screen) and R40 (confirm): the timestamps are in the
   reports and `--episodes` takes them. To ask about the instance as it is now, draw a fresh set by the same rule
   from cycles after the last change to its loop, and name it here.
4. **Judging path:** blind Opus subagents export and import verdicts through the workflow below.
   Keep judges independent of the candidate family when testing GPT arms.

5. **Screening list.** `thelos-2026-10-30-candidates.json` holds 160 OpenRouter models priced for thelos's volume
   (`pin`/`pout`/`pc` $/M input, output, cache read; `cost` projected $/day; `pinned` = on a pinned zero-retention
   provider; `implicit` = implicit caching). Prices move: refresh them before choosing, and **measure**: the screen
   estimated MiniMax M3 at $0.067 a cycle and it cost $0.152.
6. **Not reusable:** replays, for a fair new comparison. A bake-off's arms are fixed at creation, and the web answers its
   arms shared are not kept on disk, so a new candidate cannot be added to an old bake-off fairly; every new bake-off
   replays its baseline again ($0.056 a cycle on DeepSeek in the last one). The exception is a bridge: stored runs
   judged against each other, which links bake-offs at the price of unshared web answers (see § After a bake-off).

## Running one

```bash
bin/shaft trial models thelos --arm openrouter/deepseek/deepseek-v4.1-flash --arm <candidate>[@effort] \
  --episodes <ts> … --arm-parallel 3 --replay-only
bin/shaft trial models thelos --id <id> --export-cases <dir>      # blind cases + the list still unjudged
# one Opus subagent per case: read <dir>/cases/<case>.md, write only the JSON verdict to <dir>/verdicts/<case>.json
bin/shaft trial models thelos --id <id> --import-verdicts <dir>   # decides each pair, saves, prints the report
```

- Export redacts every arm's model id, also spelled out ("Claude Haiku 5.5"), and the known model families (one
  DeepSeek run printed its own config). Each order is its own case with a random id, listed shuffled, so neither the
  file nor the list tells which run is the baseline. A pair replayed again since its export is exported again and
  its old verdicts are ignored; the mapping records the instance, so one directory can serve several.
- Import refuses a bake-off another judge already judged (one bake-off, one judge). A verdict file that is not
  exactly the schema (one judge wrote text after its JSON) is named for re-judging and the pair stays unjudged: judge
  it again from scratch, never repair it. Re-run export to list what is still missing.
- `create` refuses a judge that is also an arm, but not one of the same family. Opus judging Sonnet or Haiku could
  only favour them, and they still lost; a family win under its own family's judge needs a second judge.
- Use a scratch directory outside the repo for `<dir>`: cases quote the private record.
- While the delegation guard reroutes general-purpose subagents at a require rule, spawn judges as `Explore` with model `opus`.
  They are read-only and some refuse to write a file: have each return `case <id>` and the JSON as its final reply, then save
  the replies to `verdicts/` with a script.
- Routing and allowed models can be set for one run in the environment (`SHAFT_OPENROUTER_PROVIDER`, `SHAFT_CLOUD_MODELS`,
  `SHAFT_CODEX_MODELS`): the live settings stay as they are.
- A dense case can overflow a judge's context, and a judge can skim without saying so. Have each judge report a partial
  read, delete that verdict, and leave the pair unjudged if the case still does not fit. After judging, compare what each
  judge read with its case's size (transcripts, Read and Bash output) and rerun the report's sums without the low ones.

### Before spending

- Smoke-test one cycle before a full run to check provider parameter support and retention routing.
- The arm must be in `SHAFT_CLOUD_MODELS`.

- Rough cost: replays ~$0.01–0.58 a cycle per arm (table above); Haiku 5.5 prompts over 100K tokens are billed at its
  long-prompt card.

## After a bake-off

1. Save the report: `bin/shaft trial models <name> --id <id> --report` into the maintainer's private records, with its run metadata.
2. Add its row to the ledger, its models to the results table and [leaderboard.md](leaderboard.md), its arms to
   `DATA` and `CLAIMS` of [parity_score.py](parity_score.py) (then rerun it for the leaderboard's score), any new cycle
   set to § Cycle sets.
3. Keep instance deployment decisions and operational details in private review records.

A bake-off with a broken baseline or no link to the others needs no new replays: copy stored runs of the same cycles from
both bake-offs into a new record, then `--export-cases` and `--import-verdicts` as usual. See
(a private review record, not published). Judges cost the same as a new bake-off.
