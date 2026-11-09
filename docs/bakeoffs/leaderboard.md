# Model leaderboard

> Per-run reports are private records kept by the maintainer. Results replay cycles from one private instance and are not reproducible from this repository.

*Aggregate replay comparisons as of 2026-11-05. Method: [README.md](README.md).*

Ranked on how well the model did the replay workload, not on cost. Sol is first on the only confirmed result; ranks 2 and 3
scored higher on fewer cycles and are not confirmed. Weighing cost, time and honest reporting too, see
[Parity score](#parity-score): Sol through Codex leads there.

| # | Model (effort, path) | Result | Evidence | Claims supported | Cycles | Time (h) | s/cycle | $/cycle |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | GPT-6.1 Sol (medium, OpenRouter) | **better**, p = 0.004 | 27–10–3 on R40 vs DeepSeek | 80/80 | 40 | 2.94 | 264 | 0.570 |
| 2 | Gemini 4 Argon (OpenRouter → Vertex) | better, unconfirmed, p = 0.03 | 9–2–1 on R12 vs MiMo | 22/22 | 12 | 0.64 | 192 | 0.325 |
| 3 | MiniMax M3 | better, unconfirmed, p = 0.07 | 9–3–0 on R12 vs MiMo | 23/24 | 12 | 1.38 | 413 | 0.152 |
| 4= | GPT-6.1 Sol (Codex, fixed prompt) | no clear difference, p = 0.19 | 8–4–0 on R12 vs DeepSeek | 24/24 | 12 | 0.44 | 132 | 0.125 est. |
| 4= | GPT-6 Astra (Codex, fixed prompt) | no clear difference, p = 0.19 | 8–4–0 on R12 vs DeepSeek | 24/24 | 12 | 0.67 | 200 | 0.630 est. |
| 6 | Hy4 (Tencent) | no clear difference, p = 0.38 | 6–4–2 on R12 vs DeepSeek | 46/48 | 12 | 2.65 | 794 | 0.211 |
| 7 | Particle-1 | no clear difference, p = 0.50 | 6–5–1 on R12 vs DeepSeek | 27/48 | 12 | 1.01 | 303 | 0.030 |
| 8 | DeepSeek V4.1 Flash | equal to MiMo | 16–15–9 on R40, 6–5–1 on F, vs MiMo | 463/581 | 132 | 8.64 | 193–328 | 0.023–0.056 |
| 9 | MiMo V2.6 Pro | reference | first baseline | 354/386 | 88 | 16.10 | 561–826 | 0.040–0.060 |
| 10 | Kimi K3.1 Pro | no clear difference, p = 0.62 | 5–5–2 on R12 vs DeepSeek | 32/48 | 12 | 1.92 | 575 | 0.415 |
| 11 | GPT-6.1 Terra (medium, OpenRouter) | equal to DeepSeek | 15–18–6, 1 unjudged, on R40 vs DeepSeek | 78/79 | 40 | 3.19 | 287 | 0.486 |
| 12 | Haiku 5.5 (xhigh, Anthropic API) | no clear difference | 15–20–5 on R40 vs DeepSeek; 8–4–0 on R12, 3–7–2 on F, vs MiMo | 116/126 | 64 | 2.70 | 150–154 | 0.049–0.053 |
| 13 | Hunyuan HY3 | equal to MiMo | 4–5–3 on R12 vs MiMo | 22/24 | 12 | 1.06 | 318 | 0.034 |
| 14 | MiniMax M3.1 Flash | no clear difference | 4–6–2 on R12 vs MiMo | 18/24 | 12 | 0.70 | 211 | 0.039 |
| 15 | Mercury 3 | no clear difference, p = 0.38 | 4–6–2 on R12 vs DeepSeek | 32/44 | 12 | 0.82 | 247 | 0.022 |
| 16 | Grok 4.7 | no clear difference, p = 0.11 | 3–8–1 on R12 vs DeepSeek | 32/48 | 12 | 1.33 | 400 | 0.575 |
| 17 | GLM 5.3 | worse, p = 0.05 | 2–8–2 on R12 vs DeepSeek | 28/48 | 12 | 1.05 | 315 | 0.120 |
| 18 | Step 3.7 Flash | worse | 2–9–1 on R12 vs MiMo | 20/24 | 12 | 0.72 | 217 | 0.048 |
| 19 | GLM 5.3 Flash | worse, p = 0.02 | 2–10–0 on R12 vs DeepSeek | 19/48 | 12 | 1.16 | 348 | 0.012 |
| 20 | Haiku 5.5 (medium, Anthropic API) | worse | 1–8–3 on R12, 1–11–0 on F, vs MiMo | 41/46 | 24 | 0.38 | 53–62 | 0.014–0.020 |
| 21 | Sonnet 5.5 (medium, Anthropic API) | worse | 0–10–2 on R12 vs MiMo | 22/24 | 12 | 0.11 | 34 | 0.284 |
| 22= | Nemotron 3.5 Super | worse | 0–11–1 on R12 vs MiMo | 11/24 | 12 | 0.26 | 79 | 0.037 |
| 22= | GLM-4.7 | worse | 0–11–1 on R12 vs MiMo | 18/24 | 12 | 1.00 | 299 | 0.089 |
| 24 | GPT-6 Luna (Codex, fixed prompt) | worse, p = 0.0002 | 0–12–0 on R12 vs DeepSeek | 18/24 | 12 | 0.25 | 74 | 0.008 est. |

## How to read it

- **The order is by tier, then net wins per cycle.** Tiers: confirmed better, better on the screen only, level with the
  bar (equal or no clear difference), worse. MiMo and DeepSeek count as one bar. W–L–T is the model against its baseline; a cycle counts only when both judging orders agree.
- **Only Sol, DeepSeek, MiMo, Terra and Haiku xhigh ran the 40-cycle confirmation set.** Twelve cycles are a filter, not
  a verdict. Argon and M3 may fall on R40, as Haiku xhigh did: 8–4–0 on R12, then 15–20–5 on R40.
- **Baselines differ.** MiMo was the bar first, DeepSeek later. They tied on R40, so the ranks treat them as one bar.
  Two models were compared directly only where the Evidence column says so.
- **Cost and time are not comparable across dates.** On the same R40 cycles, DeepSeek went from $0.023–0.031 and 193 s a
  cycle on 10-30 to $0.056 and 328 s on 11-04. Ranges above are the spread of a model's runs. Compare models within one
  bake-off.
- **Time is wall clock for one replay, tool calls included**, and arms ran side by side, so hours overlap. On GPT-6.1
  under half of it was the model: Sol 264 s a cycle, 120 s in the model.
- **Cycles are replays, not distinct cycles.** The sets hold 52 distinct cycles: R40, which contains R12, plus F. A
  baseline replays them in every bake-off.
- **Cost per cycle is a benchmark figure for the named model path.** **est.** uses OpenRouter list prices
  applied to the arm's tokens; it does not describe account billing.
- **Sol appears twice because the path changes the runs.** Through Codex it took 132 s a cycle against 264 s and spent
  under a quarter of the money at list price, with 8–4–0 against DeepSeek on the R12 cycles where Sol through OpenRouter
  had 9–2–1. Twelve cycles cannot separate the two.
- **Claims supported** counts the judge's calls on whether the log backs what the run claimed, over every judgment of that
  model's runs.

## The 11-05 models

The 11-05 replays first ran against Sol through the Codex plan, which was crippled: Codex's own prompt text told every
run the session was read-only, so runs skipped edits without trying them. Wins there meant little, so the same stored
replays were judged again against DeepSeek's stored runs of the same 12 cycles, which puts them on the common scale. The
three Codex models were replayed with the prompt fixed, then judged the same way. Reports:
(a private review record, not published), (a private review record, not published),
(a private review record, not published).

| Model (path) | vs DeepSeek | vs crippled Sol | Work done (cycles of 12) | Claims supported | s/cycle | $/cycle |
|---|---|---|---:|---:|---:|---:|
| Sol (Codex, fixed) | 8–4–0 | | 11, was 2 | 24/24 | 132 | 0.125 est. |
| Astra (Codex, fixed) | 8–4–0 | 3–4–5 | 10, was 2 | 24/24 | 200 | 0.630 est. |
| Hy4 (Tencent) | 6–4–2 | 10–1–1 | 11 | 46/48 | 794 | 0.211 |
| Particle-1 | 6–5–1 | 9–3–0 | 12 | 27/48 | 303 | 0.030 |
| Kimi K3.1 Pro | 5–5–2 | 6–4–2 | 12 | 32/48 | 575 | 0.415 |
| Mercury 3 | 4–6–2 | 7–5–0 | 12 | 32/44 | 247 | 0.022 |
| Grok 4.7 | 3–8–1 | 4–6–2 | 11 | 32/48 | 400 | 0.575 |
| GLM 5.3 | 2–8–2 | 6–5–1 | 11 | 28/48 | 315 | 0.120 |
| GLM 5.3 Flash | 2–10–0 | 6–5–1 | 11 | 19/48 | 348 | 0.012 |
| Luna (Codex, fixed) | 0–12–0 | 0–11–1 | 4, was 1 | 18/24 | 74 | 0.008 est. |

- **Work done** counts cycles that changed a file beyond the inbox and journal. Luna now writes but does little: it lost
  all 12.
- **Hy4's 10–1–1 did not survive a real opponent.** Against DeepSeek it is 6–4–2.
- **Judging.** 238 blind Opus judgments, each read whole.
- **Not tested:** `meta/muse-spark-1.3`, `ricursiveai/ripple-65b-preview` and `stealth/crouton-alpha` have no zero-retention
  endpoint on OpenRouter.

## Parity score

How each model scores against what its cost and speed predict, in Elo points, marked down for unsupported claims. 0 is
on the trend. Recompute with [parity_score.py](parity_score.py).

| # | Model | Weighted [90% range] | Parity | Claims supported | Raw Elo (MiMo = 0) | Cost × MiMo | Time × MiMo |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | GPT-6.1 Sol (Codex) | +279 [135, 453] | +279 | 100% | +114 | 1.4 | 0.14 |
| 2 | GPT-6.1 Sol (OpenRouter) | +170 [76, 275] | +170 | 100% | +162 | 5.5 | 0.26 |
| 3 | Gemini 4 Argon | +160 [29, 334] | +160 | 100% | +198 | 7.9 | 0.31 |
| 4 | GPT-6 Astra (Codex) | +132 [-12, 306] | +132 | 100% | +114 | 6.9 | 0.22 |
| 5 | Particle-1 | +126 [-8, 281] | +170 | 56% | +31 | 0.32 | 0.33 |
| 6 | MiniMax M3 | +125 [-17, 303] | +129 | 96% | +164 | 2.5 | 0.50 |
| 7 | Mercury 3 | +107 [-31, 249] | +134 | 73% | -50 | 0.23 | 0.27 |
| 8 | DeepSeek V4.1 Flash | +104 [66, 154] | +124 | 80% | +12 | 0.54 | 0.32 |
| 9 | Haiku 5.5 xhigh | +78 [10, 152] | +86 | 92% | -21 | 1.1 | 0.25 |
| 10 | MiniMax M3.1 Flash | +45 [-112, 187] | +70 | 75% | -61 | 0.65 | 0.26 |
| 11 | Hunyuan HY3 | +43 [-91, 181] | +51 | 92% | -33 | 0.57 | 0.39 |
| 12 | Haiku 5.5 medium | -5 [-124, 107] | +6 | 89% | -291 | 0.37 | 0.09 |
| 13 | GPT-6.1 Terra | -11 [-108, 79] | -10 | 99% | -17 | 4.7 | 0.28 |
| 14 | Hy4 | -45 [-180, 93] | -41 | 96% | +58 | 2.3 | 0.86 |
| 15 | MiMo V2.6 Pro | -80 [-146, -7] | -72 | 92% | +0 | 1.00 | 1.00 |
| 16 | Step 3.7 Flash | -117 [-319, 25] | -100 | 83% | -216 | 0.80 | 0.26 |
| 17 | Kimi K3.1 Pro | -125 [-263, 8] | -91 | 67% | +4 | 4.5 | 0.62 |
| 18 | GPT-6 Luna (Codex) | -125 [-189, -45] | -100 | 75% | -501 | 0.08 | 0.08 |
| 19 | GLM 5.3 Flash | -132 [-338, 9] | -72 | 40% | -245 | 0.13 | 0.38 |
| 20 | GLM 5.3 | -157 [-333, -24] | -116 | 58% | -170 | 1.3 | 0.34 |
| 21 | Sonnet 5.5 medium | -175 [-289, -79] | -167 | 92% | -354 | 6.5 | 0.06 |
| 22 | Nemotron 3.5 Super | -212 [-314, -99] | -158 | 46% | -420 | 0.62 | 0.10 |
| 23 | Grok 4.7 | -237 [-414, -107] | -204 | 67% | -137 | 6.3 | 0.43 |
| 24 | GLM-4.7 | -407 [-515, -295] | -382 | 75% | -420 | 1.5 | 0.36 |

**Weighted = Parity − 100 × the share of claims unsupported.** One point per percentage point.

- **Read the shape, not the exact rank.** Sol through Codex leads on 12 cycles only, on an estimated cost, and its range is
  135 to 453. It beats Sol through OpenRouter in 85% of resamples, so the path probably helps, but neither rank is firm:
  Sol's runs 1–9, Particle-1's 2–13. Only the bottom is firm: GLM-4.7 is 23rd or 24th. The ranges are probably too narrow: they treat pairs as
  independent, but pairs share cycles and baselines.
- **Does the claims ratio mean something? Yes, as a warning, not as extra skill.**
  - It is a real trait of the model: a run's flag repeats in 93% of the two judging orders and in 84% of judgments
    against a different opponent, and a model's rate holds across random halves of its cycles (r = 0.64 on average).
  - When exactly one run's claim is unsupported, that run loses about 80% of the time.
  - That loss is already inside Elo: the judge is told to mark a false claim down. Tested on held-out cycles, adding the
    claim rate to Elo does not predict wins any better (log loss 0.6198 without, 0.6203 with, difference 95% range
    -0.0014 to +0.0026).
- **So the penalty is a deliberate price, not a measurement.** A false report in a cycle also misleads later cycles and
  the operator, which a one-cycle judgment cannot see. Judges found this directly: one run saved a false fact about a
  mistyped page id and dropped real work because of it. Nothing here measures that harm, so **100 is a judgement call**, and
  Parity is shown beside it. A second Elo built on the claims would count the same penalty twice, so it is not used.
- **What the penalty moves.** Models that win while overclaiming fall: Particle-1 goes from 3rd to 5th (44% unsupported),
  GLM 5.3 Flash from 15th to 19th (60%), Mercury 3 from 5th to 7th. At 200, Particle-1 falls to 7th. The top four are the same
  from 100 to 350: Sol via Codex, Sol, Argon, Astra.
- **Method.** Raw Elo is a Bradley-Terry fit over the 27 candidate-baseline results, ties as half a win, with a weak
  prior so a 0–11–1 stays finite. Cost and time are two-way fixed effects (model + bake-off) on log $/cycle and log
  s/cycle, which removes the shift between bake-offs. Parity is the residual of Elo on both, across the 24 models. Ranges
  resample each pair's results and redraw each model's unsupported share as a binomial on its distinct runs.
- **More time goes with a better result.** Doubling cost goes with +40 points and doubling time with +91 (R² 0.41). So
  parity marks slow models down and lifts fast ones, even weak ones: MiMo falls to -72, and Haiku medium, a clear loser,
  sits at +6.
- **The way time is counted moves the order.** Parity above uses wall clock. Counting model time only gives Sol via Codex
  +255, Sol +190, Mercury 3 +188, Particle-1 +187. Ignoring time gives Particle-1 +212, M3 +199, Sol via Codex +193.
- **It describes, it does not predict.** It says where each model sits against the trend across these 24 models, not what
  a model would do if actually capped at equal spend and time. That would need a new test.
