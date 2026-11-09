# PLAN — instance stats never mix judges

Goal: no number an instance reads about itself rises or falls because a different judge graded it. The
rule PLAN-measurement applies to the instance's own model ("never compared across models") is applied to
the judge too.

**Status (2026-10-16): built on branch `judge-mix`, waiting to be merged.** the operator approved the slim version
(remove Haiku; count only current judges; tell the instances), with no calibration set and no probation
stratification. The full suite passes (626 tests, 52 skipped), the same as on main.

## Why

A random judge rotation can change apparent claim support even when instance behaviour is unchanged.
The following aggregate comparisons motivated separating statistics by judge.

**Unsupported claims (`claim_supported: false`), sample instance, by judge and day:**

| Day | Opus | Sonnet | Haiku |
|---|---|---|---|
| 10-13 | 4/48 (8%) | — | — |
| 10-14 | 6/60 (10%) | 3/25 (12%) | 14/29 (48%) |
| 10-15 | — | 8/44 (18%) | 25/45 (56%) |
| 10-16 | — | 5/21 (24%) | 15/18 (83%) |

**The same cycles, graded by the blind second judge (GLM 5.3 Flash), to compare against:**

| Instance | Main judge | Cycles | Unsupported: main judge | Unsupported: GLM | Success: main judge | Success: GLM |
|---|---|---|---|---|---|---|
| example-a | Haiku | 26 | 54% | 4% | 0.63 | 0.88 |
| example-a | Sonnet | 19 | 21% | 5% | 0.82 | 0.92 |
| example-a | Opus | 12 | 17% | 0% | 0.88 | 0.96 |
| shaft | Haiku | 15 | 80% | 27% | 0.57 | 0.67 |
| shaft | Sonnet | 21 | 29% | 24% | 0.60 | 0.60 |
| shaft | MiMo | 17 | 24% | 12% | 0.56 | 0.56 |
| shaft | Opus | 8 | 50% | 12% | 0.81 | 0.81 |

Haiku is the outlier against all four other judges on the same cycles. The value grades already agreed
across judges (gap ≤ 0.21, PLAN-value), so value is left as it is.

**What it cost:**

- `self/stats.md` printed one running count of unsupported claims, and the success rate, confidence check and
  Brier score, pooled over every judge.
  - example-a read the count (8/107 → 73/273) as "the central fact about my accuracy". It has spent about 65% of
    its cycles since 10-13 on self-audit tooling meant to bend it.
- The header named only the last cycle's judge ("judged by claude-haiku-5-5").
- example-a is never graded by MiMo (its own model), so it got Haiku half the time and shaft a third of the time.
  The pooled numbers were not comparable between instances either.

## What changed

- **Haiku leaves the rotation.** The default `SHAFT_JUDGE_MODELS` is now `openrouter/xiaomi/mimo-v2.6-pro,claude-sonnet-5-5`.
  - example-a is graded by Sonnet only.
  - The estimated cost is about +$5/day: Haiku's share of judgments moves to Sonnet, about 20× dearer per call
    before caching.
  - Haiku stays where it isn't judging: `describe_image`, and the cloud models instances can use.
  - The trials judge and the battery grader draw from the same rotation, so they lose Haiku too. A new battery
    era starts; it had no scored items yet.
- **`stats.md` counts only verdicts by judges in today's rotation**, plus the no-intent rule (`metrics.by_current_judge`).
  - This applies to success, the confidence check and Brier score, claim matched the judge, unsupported claims
    (now "N of M"), vague criteria, failure modes, the second-judge agreement and achieved difficulty.
  - Since 2026-10-24, calibration keeps two questions separate within that rotation: v2
    (`confidence_of="criterion_met=yes"`) uses binary Brier, yes=1 and partial/no=0; v1 is the older
    "probability of success", yes=1/partial=0.5/no=0. Their Brier scores and sample sizes are never pooled.
  - Value still counts every cycle.
  - The header names the judges counted and the verdicts set aside, by judge.
  - Opus's verdicts are set aside too, because it is no longer in the rotation. This removes example-a's early history
    (111 verdicts) from those figures.
- **Probation's baseline** (`start_probation`, `_verdict`) uses only verdicts by current judges. Otherwise, for
  about a day after this change, a baseline full of the strict judge's grades would hide a real drop.
- **`shaft review`'s by-judge lines** show the unsupported claims for each judge.
- **Not changed:**
  - `surprise.py` baseline: PLAN-surprise pre-registered its baseline and plans to re-judge it.
  - `battery.py` "unsupported_claim" question: it asks what the judges found, all of it.
  - The verdict text each instance reads (it already names its judge).

## Measured on the real data (rendered from the branch, read-only)

| | Old stats.md (pooled) | New stats.md (current judges only) |
|---|---|---|
| example-a: unsupported claims | 80 (of 293) | 16 of 93 (Sonnet 93; 205 set aside: Opus 111, Haiku 94) |
| example-a: judged success; confidence | 78%; "overconfident", 82% vs 78% | 82%; "underconfident", 81% vs 82% |
| shaft: unsupported claims | 101 (of 241) | 29 of 135 (MiMo 69, Sonnet 64, no-intent rule 2; 115 set aside: Haiku 69, Opus 46) |
| shaft: judged success; confidence | 61%; overconfident, 76% vs 61% | 63%; overconfident, 75% vs 64% |

shaft's overconfidence is real; example-a's was the judge mix.

## Tests

- `supervisor/tests/test_measurement.py`, `JudgeMixTest`: Haiku is out of the default rotation, a retired
  judge's verdicts are not counted, "N of M", only retired verdicts produces no division by zero, and the
  probation baseline ignores retired judges.
  - The four `metrics.py` tests fail on main's `metrics.py`.
- Fixtures that had a placeholder judge ("j", or none) now name a judge from the current rotation.
- The review test pins the new per-judge claim counts.

## After merging

1. Merge `judge-mix` into main. Each instance's `self/stats.md` is rewritten after its next cycle.
2. Send one message to example-a and to shaft, if the operator agrees. It goes through `shaft send`, so it reaches them as
   a message from the operator. The draft is in the merge request; it gives the facts and does not tell them
   what to work on.

## 2026-10-20: Sonnet out, DeepSeek V4.1 Flash in, one run in two judged (#45)

- **Why.** Judge costs and model exclusions can narrow the effective rotation.
  Admission therefore needs a same-case calibration comparison.

- **The admission check this plan asks for.** DeepSeek V4.1 Flash re-judged 40 runs (20 example-a, 20 example-b) that GLM had
  graded blind as the second judge. It used the same cases as `metrics._case` rebuilds them, the same system prompt and
  schema, and the same provider policy.

  | Judge | Unsupported claims | Success | Difficulty | Value |
  |---|---|---|---|---|
  | GLM 5.3 Flash | 2% | 98% | 2.17 | 1.90 |
  | DeepSeek V4.1 Flash | 0% | 98% | 2.23 | 1.60 |
  | The main judge at the time (mostly Sonnet) | 8% | 91% | 2.05 | 1.65 |

  - DeepSeek and GLM agreed on whether the criterion was met in 40 of 40 runs. That is weaker evidence than it looks:
    nearly every case was a "yes", so this sample cannot tell the two apart well.
  - Both are more lenient than Sonnet was.
  - Value runs 0.3 lower with DeepSeek (example-b 1.85 vs 2.40). Value is still pooled across judges, so watch it.
- **The second judge moves to DeepSeek.** GLM had been both a main judge and the second judge, and graded four runs twice
  (example-a 14:33 and 14:54, example-b 14:16, shaft 15:17).
  - A run the second judge will grade is never given that model as its main judge (`judge.pick(avoid=…)`).
  - The agreement in stats.md counts only today's second judge against another model (`metrics.second_counted`).
- **`SHAFT_JUDGE_EVERY` (default 2).** A run left out is recorded in `judgments/<name>-unsampled.jsonl`, not in the
  judgments, so no figure counts it.
- Configure the permitted judge pool separately from calibration.

## 2026-10-29: today's judges are not pooled either

- **Why.** Removing Haiku left one pooled figure across every judge still in the rotation. That was safe while the
  rotation was MiMo and Sonnet: they agreed on shaft, 24% vs 29%. It is not safe for MiMo, GLM 5.3 Flash and
  DeepSeek V4.1 Flash. Unsupported claims on the same cycles (main judge vs blind second judge), 10-20 → 10-29:

  | Instance | Main → second | n | Main | Second |
  |---|---|---|---|---|
  | example-a | GLM → DeepSeek | 82 | 2% | 10% |
  | example-b | GLM → DeepSeek | 89 | 3% | 16% |
  | shaft | GLM → DeepSeek | 49 | 18% | 27% |
  | shaft | MiMo → DeepSeek | 46 | 33% | 20% |

  - The strictness order is MiMo, then DeepSeek, then GLM.
  - Judged success differs less: 0–8 points, with GLM higher on example-a and example-b.
  - Achieved difficulty agrees across the three: shaft MiMo 0.25 vs DeepSeek 0.26 on the same cycles. Probation and
    the headline are left as they are.
- **What changed** (approved by the operator 2026-10-29; PROPOSAL-judge-mix-2026-10-29.md):
  - `stats.md` gives judged success and unsupported claims per judge (`metrics.by_judge_trend`).
    - Each judge gets one entry, its 7 days to the latest verdict against its 7 before (`TREND_DAYS`).
    - Fewer than 10 verdicts in a window (`TREND_MIN`) shows "too few (n)".
    - No figure pools two judges.
    - The windows end at the latest verdict, not at now, because stats.md is rewritten after every cycle.
    - A judge with no verdict in the latest window has no entry. In practice that is the no-intent rule.
  - The header says that today's judges differ too, and that each figure is read against the same judge's earlier one.
  - **The success line moved** from "Success against my own criteria (raw)" into "What I achieved", next to success
    by difficulty.
    - The LIVE prompt shows only stats.md's first 5500 characters (template `prompts._head`).
    - On example-a's real stats.md, the pooled line ended at about character 4960.
    - The per-judge line in the same place started at character 4972, so the figures fell out of the prompt.
    - The claims line was past the cut before this change (character ~6400) and still is.
- **Not changed:**
  - Calibration (Brier, AUROC, base rates) stays pooled within the rotation. Split per judge, n falls to about 30–90.
  - Also still pooled within the rotation: "success at 1–2/3/4–5" by difficulty, "My own claim matched the judge" and
    vague criteria.
  - `shaft review` already gives claims by judge.
- **Not done:** a hand-graded claim set to decide which judge is right. GLM's 2% on example-a, against Opus and Sonnet's
  17–21% on 10-16, is the figure to doubt.
- **Tests:** `JudgeMixWithinRotationTest` in `supervisor/tests/test_measurement.py`.
  - All five tests fail on the earlier `metrics.py`. The first fails on the pooled "12 of 40".
  - Four older tests that pinned the pooled line now pin the per-judge entries.
