# Running model bake-offs

A bake-off compares candidate models for one instance by replaying that instance's past cycles on each model and
having blind judges compare the runs pair by pair. The code is [supervisor/bakeoff.py](../../supervisor/bakeoff.py)
(`shaft trial models`). Replays never settle, commit, deliver or send (`supervisor/trial.py`).

## Where things are

| What | Where | In git |
|---|---|---|
| A bake-off in full: every replay's trace and diff, every judge verdict and its reasoning | `.supervisor/bakeoffs/<name>-<id>.json` on the supervisor host | no |
| Exported blind cases and the run mapping | `<dir>` you choose; the mapping in `.supervisor/bakeoffs/exports/` | no |

`shaft trial models <name> --id <id> --report` prints a saved bake-off's report again (it reads the JSON; it runs
nothing). Keep your reports and your results with your own records: they quote the instance's private cycles.

## Before a bake-off: what to reuse

1. **Earlier results.** Do not run a model, effort and path again on the same instance unless one of these changed: the
   model's version, the instance's prompts or loop, or the budget (which changes how a verdict's cost reads). A
   verdict holds for the instance code it ran (each report names its `code_rev`).
2. **Baseline:** the instance's current model, unless you want to compare against another.
3. **Cycle sets.** To compare with an earlier bake-off, replay the same cycles: the timestamps are in its report and
   `--episodes` takes them. To ask about the instance as it is now, draw a fresh set with `--draw N` from cycles after
   the last change to its loop.
4. **Prices move.** Check current provider prices and retention settings before choosing a candidate, and measure: a
   screen's estimate from list prices can be off by a factor of two or more.
5. **Not reusable:** replays, for a fair new comparison. A bake-off's arms are fixed at creation, and the web answers
   its arms shared are not kept on disk, so a new candidate cannot be added to an old bake-off fairly; every new
   bake-off replays its baseline again. The exception is a bridge (§ After a bake-off).

## Running one

```bash
bin/shaft trial models <name> --draw 24 [--since <iso>]           # look at a fresh set first: prints it, runs nothing
bin/shaft trial models <name> --arm <baseline> --arm <candidate>[@effort] \
  --draw 24 --since <iso> --until <last ts printed> --arm-parallel 3 --replay-only   # or --episodes <ts> …
bin/shaft trial models <name> --id <id> --export-cases <dir>      # blind cases + the list still unjudged
# one judge per case: read <dir>/cases/<case>.md, write only the JSON verdict to <dir>/verdicts/<case>.json
bin/shaft trial models <name> --id <id> --import-verdicts <dir>   # decides each pair, saves, prints the report
```

- Export redacts every arm's model id, also spelled out ("Claude Haiku 5.5"), and the known model families, since a
  run can print its own config. Each order is its own case with a random id, listed shuffled, so neither the file nor
  the list tells which run is the baseline. A pair replayed again since its export is exported again and its old
  verdicts are ignored; the mapping records the instance, so one directory can serve several. The mapping is kept in
  `.supervisor/bakeoffs/exports/`, never in `<dir>`, so a judge cannot read which run was which. An older
  `<dir>/mapping.json` moves there on the next export or import.
- What a judge reads (`trial._run_text`) carries no wall-clock time and no "same answer as the other run" tags: the
  baseline replays first, so those tags would mark every candidate run. The API judge gets the same redaction as the
  exported cases. Cases exported before this change are stale (`CASE_VERSION`) and are exported again.
- **Draw** (`--draw N`): live cycles that ended after `--since` (default: the last change to the instance's `shaft/`
  or `config.env`), ended by the loop's own control, with a known start, no pause for a conversation and every inbox
  message still in the mind store. Three kinds: the operator asked for it, it wrote or sent something, it only
  looked (by tool names, a heuristic). Of each kind 4, or all it has (fewer each when the set is too small for every
  kind to have 4), the rest by each kind's share, evenly spaced in time within a kind. Offsets in `--since`/`--until`
  are converted to local time, the time episodes carry. The report then adds W–L–T–S per kind and the net reweighted
  to the kinds' shares.
- **Outcomes.** W and L need both orders to agree; T is both orders saying tie; S (split) is the orders disagreeing.
  Neither T nor S counts in the sign test. A run that died on its provider (its last model call failed, or its output
  ends on the client's HTTP, connection or stream error, other than a request the provider rejects as invalid — 400,
  413, 422 — which it would reject in production too; or it was not replayed) leaves the pair unjudged, and a resume
  replays that state again in full; a run that otherwise did not complete still loses without a judge, and the report
  counts those apart ("no judge"). A refusal the provider returns as an answer is the path's own behaviour: it counts.
- Import refuses a bake-off another judge already judged (one bake-off, one judge). A verdict file that is not exactly
  the schema is named for re-judging and the pair stays unjudged: judge it again from scratch, never repair it. Re-run
  export to list what is still missing.
- `create` refuses a judge that is also an arm, but not one of the same family. A judge of the candidate's own family
  can favour it, so confirm a family win with a judge from another family.
- Use a scratch directory outside the repo for `<dir>`: cases quote the instance's private cycles.
- For many cases, run one independent judge per case with a schema holding the verdict plus `case`, `read_whole` and
  `chars_read`, and drop any verdict whose judge read under 95% of its case.
- Routing and allowed models can be set for one run in the environment (`SHAFT_OPENROUTER_PROVIDER`,
  `SHAFT_CLOUD_MODELS`, `SHAFT_CODEX_MODELS`): the live settings stay as they are.
- A dense case can overflow a judge's context, and a judge can skim without saying so. Have each judge report a partial
  read, delete that verdict, and leave the pair unjudged if the case still does not fit. After judging, compare what
  each judge read with its case's size and rerun the report's sums without the low ones.

### Before spending

- Smoke-test one cycle before a full run to check provider parameter support and retention routing.
- The arm must be in `SHAFT_CLOUD_MODELS`.
- Estimate replay cost from current prices, including any long-prompt pricing, then measure it on the smoke test.

## After a bake-off

1. Save the report (`bin/shaft trial models <name> --id <id> --report`) with your own records, with its run metadata.
2. Note which cycle set it used, so a later bake-off can replay the same cycles.

A bake-off with a broken baseline or no link to the others needs no new replays (a bridge): copy stored runs of the
same cycles from both bake-offs into a new record, then `--export-cases` and `--import-verdicts` as usual. Judges cost
the same as a new bake-off.
