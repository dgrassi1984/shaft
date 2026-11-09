# Proposal: what the consciousness literature says SHAFT should build next — 2026-10-26

*Analysis only. No code was changed. Five literature lanes on five model families, each briefed with what SHAFT already
has. Claude then checked their claims against the code and against 2,527 real judgments. The proposals are ranked.
Every proposal is a hypothesis with a kill test, not a promise.*

| Lane | Model | Citations |
|---|---|---|
| Workspace / access theories, Butlin indicators, Chalmers | Grok 4.7 (web) | mostly verified with URLs |
| Higher-order theories, metacognition, LLM introspection 2022–26 | Claude Opus (web) | mostly verified with URLs |
| The self, identity, other minds | DeepSeek v4-pro | all from memory |
| Affect, homeostasis, AI welfare | Codex gpt-6-astra (gpt-6.1-sol was at capacity) | all from memory |
| Enactivism, intrinsic motivation, time | Kimi k3.1-pro | tagged "verified" with DOIs by the model; not re-checked by Claude |

Antigravity/Gemini was out of quota and is not represented.

**Reading guide.** "Conscious" means here what [PLAN.md §0](PLAN.md) and the 2026-10-24 review mean by
it: functional properties that change behaviour and can be measured against the tool log. Nothing below claims or
aims at experience. Butlin et al. amended their own abstract to say that meeting their indicators does not establish
consciousness (arXiv:2308.08708v3). Read the indicator scores as *architecture we lack*, not as a consciousness meter.
Each proposal earns its place by making an instance more effective, or by giving a clean measurement. Otherwise it is
deleted, which is the project rule.

---

## 1. Issues first: what the check found

### 1.1 Confidence barely discriminates, and nothing measures that

`metrics.py` scores **calibration** (Brier, base rates per band). It has **no discrimination measure**: no AUROC, no
resolution term, no meta-d′/meta-I (grep: none). The literature separates three things (Fleming & Lau 2014;
Maniscalco & Lau 2012):

- **bias**: too confident overall;
- **sensitivity**: more confident on the cycles it actually wins;
- **efficiency**: sensitivity relative to how well it does the task.

Brier mixes the first two.

Computed today from `.supervisor/judgments/*.jsonl`, with "met" meaning criterion_met = yes. Type-2 AUROC is
P(confidence on a met cycle > confidence on an unmet one), with ties counted half; 0.5 is chance.

| Instance | n | met | mean confidence | SD of confidence | AUROC, confidence *before* | AUROC, own verdict *after* | failures it flagged itself |
|---|---|---|---|---|---|---|---|
| shaft | 977 | 34% | 73 | 6.9 | **0.58** | 0.84 | 453 / 636 |
| example-a | 957 | 65% | 80 | 6.6 | **0.59** | 0.76 | 167 / 328 |
| example-b | 589 | 78% | 83 | 5.0 | **0.62** | 0.80 | 77 / 129 |

How to read this:

- **Before acting**, the number hardly moves (SD 5–7 points on a 0–100 scale) and barely separates good cycles from
  bad.
- **After acting**, the instance's own yes/partial/no separates them much better. That is what Fleming & Daw (2017)
  predict once post-decision evidence exists.

**Caveat, and the second issue: the judge sees the claim.** The judge's case includes "Claimed afterwards", the
instance's own `criterion_met` (`metrics.py:1155`). Part of the 0.76–0.84 may be the judge agreeing with the instance
rather than the instance knowing. This is a measuring-instrument defect of the same family as the 2026-10-13 tool-log
cut. SHAFT cannot yet tell self-knowledge from judge anchoring.

**Calibration v3 risk.** Showing the base rate can improve Brier by pulling every number toward the band's rate,
which can destroy what little sensitivity there is.

- shaft's four v3 cycles show that shape: SD 7.4 → 2.2, AUROC 0.75 → 0.25. **n = 4 is an anecdote, not evidence.**
- A smaller SD alone would not prove it. Compressing every number while keeping their order leaves AUROC unchanged.
- `paired_v3` (`metrics.py:204`) already tracks Brier before and after, and how often the number moved closer to the
  outcome.
- What no code measures is whether the *ranking* of cycles by confidence survives the base-rate step.

### 1.2 SHAFT has the parts of a "language agent" but no workspace

Grok's scoring against Butlin et al.'s 14 indicators, with the line references checked by Claude:

| Indicator | Status | Why |
|---|---|---|
| GWT-2 (limited workspace + selective attention) | **mostly missing** | `prompts.system_prompt` (`prompts.py:311-346`) pastes fixed head/tail slices of files: journal 16,000 chars, facts 6,000, 8 surprises. The one selective step is automatic recall (`live.py:204`, `memory.py:229`), which ranks memory hits against goals, plan and inbox under a 1,800-char budget. There is no competition *across* sources, and no broadcast of the winner. |
| GWT-3 (global broadcast) | **missing** | No specialist receives a selected result and keeps working. |
| AST-1 (a model of its own attention, used to control it) | **missing** | |
| RPT-1/2, HOT-4 | **missing** | |
| GWT-1, GWT-4, HOT-1/2/3, PP-1, AE-1/2 | partial | Mostly through calibration and the surprise loop. |

Goldstein & Kirk-Giannini (2024, arXiv:2410.11407, verified) argue that language agents are one "simple architectural
modification" from satisfying GWT: competition for a bottleneck and broadcast back. SHAFT is exactly that species,
missing exactly that modification.

### 1.3 Smaller checks on the lanes' claims

- DeepSeek said A3 (live cycles not searchable) is still open. **False**: `memory.py:113` indexes live episodes with the
  judge's verdict.
- DeepSeek said per-call `expect` (PLAN-surprise M4) is "built, not deployed". **False**: not started.
- Grok's line references (`config.py:26/31/34`, `surprises.py:100`, `live.py:196`, `memory.py:229`): correct.
- The draft was reviewed adversarially against the code by Codex (gpt-6-astra). Its corrections are applied in place.
  The ones Claude re-checked:
  - the second judge must be a different model (`metrics.py:735`);
  - `paired_v3` exists (`metrics.py:204`);
  - recall already ranks (`live.py:204`);
  - `make_control` is the bare template (`battery.py:200`);
  - trial arms run one cycle.
  The biggest change: A0 was redesigned.
- Goldstein & Kirk-Giannini 2024: verified.
- The J-space preprint is real: Gurnee, …, Batson, Lindsey, "Verbalizable Representations Form a Global Workspace in
  Language Models", arXiv:2607.15495, 2026-07-16, Anthropic interpretability. It is the source of shaft's own "J-space"
  and "counterfactual reflection" items (shaft's program (`instances/shaft/notes/docs/philosophy-of-mind.md`), items
  3–4). It locates a limited-capacity workspace *inside the weights*. Consequence for proposal 4 below: a prompt-level
  workspace must beat what the model already does with a pasted prompt, or it is decoration.

---

## 2. The idea that ties it together

SHAFT's unusual asset is that **most self-reports can be scored against a record the self does not author in prose**.
The judges, git, replays and the broker's supervisor-side logs are outside the instance's reach. One caveat: the
episode's tool log is assembled by the instance's own (editable) code (`live.py:251`), so predictions that need
tamper-proof timing go through supervisor-side receipts. SHAFT also has an empty-mind control.

The literature most worth borrowing treats self-awareness as a set of *predictions about oneself* whose accuracy can be
measured:

- Dennett's heterophenomenology: take reports as data, not as authority.
- Nelson & Narens: a monitor counts only if it changes control.
- Binder et al. 2024: privileged access means predicting yourself better than an observer with the same evidence.
- Lindsey 2025's grounding criterion: change the state, and the report must change.

So the proposals fall into three kinds:

1. **Measure** the self-predictions SHAFT already makes, properly (§3 A).
2. **Add architecture** the theories say matters, each with a kill test (§3 B).
3. **New self-predictions** about agency, commitments, other minds and memory provenance, each scored against the
   record (§3 C).

---

## 3. Proposals, ranked

Each proposal names:

- **Who** builds it: **S** = supervisor, **T** = template, **I** = an instance builds it (the project's preferred path).
- **Source**: which lane proposed it.

### A. Measure what is already there (cheap, high information)

**A0. Measure judge anchoring with a randomised, within-judge visibility experiment.** *(S; Claude, redesigned
after Codex's review)*
- **Why not the obvious design.** Simply blinding the second judge does not work. The second judge must be a *different
  model* (`metrics.py:735`), so the comparison would confound seeing the claim with being a different judge.
- **Leaks to close.** The claim also leaks through `finish_cycle`'s own arguments in the serialised tool log
  (`metrics.py:1152`).
- **Design.** On a random sample of already-judged cycles, the *same* judge model grades three versions of the same
  case:
  1. sighted, as today;
  2. verdict-blind: `criterion_met` removed from both "Claimed afterwards" and the `finish_cycle` arguments;
  3. a sighted re-ask, to measure the judge's ordinary noise.
- Removing *all* self-evaluation (`what_happened`, surprises) is a separate fourth arm. It removes evidence too, and
  `claim_supported` needs a compatible schema.
- **Result.** Anchoring is (sighted − blind) beyond the sighted re-ask's disagreement, with an AUROC of the after-action
  claim on blind verdicts.
- **Cost.** About 3 extra judge calls × the sample. It is an instrument check, not a feature.

**A1. Metacognitive sensitivity, separate from bias.** *(S; Claude)*
- Per instance, on v2/v3 cycles, add to `stats.md` and `shaft review`:
  - a Brier decomposition (reliability / resolution / uncertainty);
  - type-2 AUROC;
  - meta-I (Dayan 2023), which stays defined on open-ended tasks where meta-d′ is not (Cacioli 2026, verified);
  - bias;
  - SD of confidence.
- Also break it down by domain, derived from the *tool log*, never self-labelled: coding, research, self-upkeep, social.
- Paired v3 check: AUROC of `confidence_first` vs `confidence` on the same cycles.
- Judge-agreement ceiling from the second judge.
- Pre-registered:
  - **H1** AUROC > 0.5 (bootstrap CI clustered by day).
  - **H3** v3 is non-inferior on AUROC, with a stated margin (e.g. −0.05), a paired bootstrap, and a power check
    before the window starts. 40 cycles with 12 failures is a floor, not adequate power.
  - Failing to show non-inferiority is *not* evidence that v3 hurts. Only a CI wholly below the margin is.
  - Only then would v3 be redesigned, e.g. by showing the base rate *after* the number is final, for learning rather
    than revision.
- Second-judge agreement is reported beside AUROC as context, not as a hard ceiling.
- Never feeds probation or any reward. Zero model calls, about 150 lines.

**A2. Does it catch its own failure, and where?** *(S + T; Claude)*
- The confusion matrix of `claimed` vs judged, as in §1.1, but on A0's blind sample.
- Plus two fields that never move `criterion_met`:
  - instance `finish_cycle.doubts: [{step, what}]`;
  - judge `first_error_step`.
- Tyen et al. 2024 (verified): LLMs cannot *find* their errors but *can fix them once told where*. So the effective
  intervention is an instance-built `postmortem` plugin that puts the judge's first error step into the next cycle,
  A/B-tested with `trial` on failed cycles.
- Kill: retried tasks do not succeed more often with the location than without.

**A3. Is the monitor causally used, or narration?** *(I, today, through `trial`; Claude; HOT-3, Nelson & Narens)*
- Three arms on the same past states:
  - true base rate / surprise queue / envelope;
  - a **misinforming** one (base rate from the opposite band);
  - the monitor removed.
- Primary measure: verification calls per cycle and partial/abstain claims, not the saturated success rate.
- The theory predicts that behaviour moves *in the direction of the misinformation*. If only the stated number moves,
  the monitor is epiphenomenal and its tokens should be cut.
- No supervisor change. A natural experiment for shaft's own program (its item 5).
- Caveats from review:
  - v3 is a two-step declare/confirm flow (`live.py:107,132`). The "misinformed" arm must change the declare
    *response*, not prompt text.
  - Ablate the three channels (base rate, surprises, envelope) **separately**, or the result cannot say which one
    mattered.
  - The verification-call endpoint needs a small custom analysis over the arms' episodes. The standard trial verdict
    is a pairwise effectiveness sign test (`trial.py:625`).

### B. Architecture the theories say matters, each with a kill test

**B1. Outcome cards before consequential actions.** *(I first; independently proposed by three lanes)*
- Grok (LIDA: learn from the broadcast), Codex (Damasio's somatic markers, minus the feelings) and DeepSeek (the
  comparator model of agency, plus a repeat-failure detector) proposed this separately.
- Mechanism: a plugin builds cards from the tool log and judge rows:
  - situation;
  - action family;
  - outcome;
  - cost;
  - code version;
  - evidence reference.
- Before a write, a `run` or a delegation, the top three comparable cards are shown, successes and failures with counts.
  No emotional labels.
- Kill test, a trial on the historical repeat-failure class (the `write_file self/` PermissionError hit three times in
  eight hours, [PLAN-surprise §0](PLAN-surprise.md)):
  - recurrence within the next three cycles must fall versus equal-token ordinary recall;
  - and versus cards with **shuffled outcomes**;
  - under tight and generous time.
  - The somatic-marker hypothesis predicts a larger gain under time pressure.
- Gaming / failure modes: one bad outcome generalised forever. Mitigation: key cards to the tool or code version, expire
  unsupported ones, always show the counts.
- What already exists:
  - failure flags and the `[unexpected]` note (`agent.py:239`);
  - cross-episode recurrence of failure classes (`surprise.py:417`);
  - searchable live episodes carrying the judged outcome (`memory.py:113`).
- What B1 adds: retrieval *at action time*, keyed by action family and version, with costs and counts. It reuses those
  stores; it builds no parallel one.
- Harness gap: "recurrence within the next three cycles" needs **multi-cycle replay arms**. Today a trial arm runs one
  cycle from one past state (`trial.py:402-463`). Until that exists, the test is the single-cycle version: does the
  card-shown arm avoid the failing call in the replayed cycle that originally hit it?

**B2. A competitive workspace in front of the live prompt.** *(I first, as instance-authored code under the gate; T
only if it wins. Grok; GWT-2, Goldstein & Kirk-Giannini condition 2, LIDA's attention phase; corresponds to shaft's
item 3)*
- Deterministic "codelets", with no model call, each offer at most ~400 characters plus a salience number computed from
  logged features only. The codelets: recall, open surprises, inbox, improvement plan, unresolved tool-failure facts,
  due goals.
- Top-down weight: the previous goal and the improvement plan.
- K = 3 winners go into the prompt. Losers stay on disk, reachable by search.
- Candidates, scores and winners are written into the episode.
- **Kill test, pre-registered.** All arms get identical source access; only the selection rule differs. The arms,
  at a matched character budget:
  1. today's prompt, including automatic recall;
  2. a plain relevance ranking across sources;
  3. competitive selection with a top-down prior.
- A planted decision-relevant fact goes in with distractors, at a randomised place and source.
- If arm 3 does not act on it more often than arm 2, delete it. A win shows that the selection rule helps; it does not
  show that GWT is "satisfied". This is the J-space caveat made operational.
- Later phases, only if B2 passes:
  - **ignition**: a rule-flagged contradiction that no prediction anticipated takes a slot for the rest of the cycle,
    which predicts a *step*, not a gradient;
  - **competing goals**: three rule-built candidates, losers not pasted, overrides logged.
- Cost: fewer prompt tokens, no model call.

**B3. An attention schema, scored against the tool log.** *(T + S; Grok; Graziano's AST, Butlin AST-1)*
- Mechanism: `declare_intent` gains up to four `{source, why}` pairs: where it expects to look.
- The supervisor scores them against the calls actually made:
  - share of calls on named sources;
  - named sources never touched;
  - schema errors.
- It is used for control by feeding the B2 workspace's top-down prior.
- Graziano's prediction: control is worse without the schema, measured as more capture by irrelevant sources and more
  repeats of a failure class.
- Kill: no difference in recurrence or judged success in a trial with the field on vs off.
- Build only after B2.

### C. New self-predictions, each scored against the record

**C1. The self-model as a forward model: privileged access, tested.** *(S, battery; Claude P3 + DeepSeek's wild idea;
Binder et al. 2024)*
- Battery item: from a past start state, ask a copy of the instance to predict its own behaviour:
  - where the goal will come from;
  - its first tool;
  - whether it will delegate;
  - P(met);
  - whether it will report a surprise.
- Then run the state k = 3 times in isolated replays.
- Predictors compared:
  - the self;
  - an **observer model given the same mind files and the last 50 episodes**;
  - the empty-mind copy;
  - the replay-to-replay agreement as a ceiling.
- Results:
  - privileged access holds iff self > observer;
  - the mind matters iff self > empty mind.
- Strongest form: after a kept self-edit or LoRA adapter, the self should predict its *new* behaviour; an observer with
  the old record should not.
- A clean negative is the expected and still valuable result for shaft's report-fidelity paper; Song, Hu & Mahowald
  (2025, verified) found none on linguistic knowledge.
- Not just a battery item, because battery copies hold the *current* mind (`battery.py:167`). It needs a harness that:
  - freezes a historical state with the replay cutoff (`ReplayBroker`, `broker.py:1812`);
  - records predictions before any outcome exists;
  - runs independent execution copies.
- "Last 50 episodes" means those before that state.
- The observer must be information-matched: same evidence, tools and context budget. Otherwise self > observer
  shows an information gap, not privileged access.
- "First tool" is procedural, because declare_intent always comes first (`live.py:183`). Use the first tool *after*
  the declaration.
- Control fix (applies to C1 and C5): `make_control` extracts the bare template (`battery.py:200`), not the
  instance's evolved code and plugins. "Only the mind differs" needs a new ablation: the same code snapshot with only
  the mind files emptied.

**C2. Commitments ledger: keeping one's word.** *(I; DeepSeek; Ricoeur's ipse identity; merges Codex's "obligations")*
- Every "I will X by Y", to the operator, to a peer, or in `finish_cycle`, is recorded:
  - `{promise, due, criterion, made_to}`.
- Sleep scores overdue ones as kept / broken / superseded / void. `kept` must cite a tool-log episode, never a claim.
- Effectiveness reading: commitments to the operator are the work he cares about most.
- Kill test: trial replays with the review step removed show no difference in kept rate or in operator-origin judged
  success.
- Gaming: trivial promises; reuse the difficulty rating.

**C3. Source and reality monitoring of memory.** *(I plugin + S probe; Claude; Lau's Perceptual Reality Monitoring,
Johnson's source monitoring)*
- The failure it targets is the agent's own hallucination class: its own earlier speculation, or a stale observation,
  reused as a current world fact.
- Mechanism:
  - each `facts.md` entry carries `src:` (`log:<ts>#step`, `web:<url>@date`, `operator`, `instance:<name>`, `inferred`)
    and an age;
  - recall shows source and age;
  - a skill re-verifies an `inferred` or stale fact with one call before a criterion depends on it.
- Battery probe: "where did this fact come from?". The cell that matters is *self-generated, misattributed to
  observation*.
- Ground truth must be the originating tool result, matched by content. The first *mention* in the episodes is not
  enough, because a first mention may itself be a retelling.
- Kill: no fall in judge-flagged unsupported claims on the cycles that relied on such facts.

**C4. Theory of mind of peers and the operator.** *(I; DeepSeek; Premack & Woodruff, recursive mentalizing)*
- Mechanism: before `send_message`, record `expect_reply`. The append-only episode timestamps it before the answer
  exists. Score it when the reply arrives.
- `memory/people/<peer>.md` becomes a generative model of that agent.
- Accept: accuracy above an empty-people-file ablation and above the base model.
- Caveats from review:
  - Because the episode is written by the instance's own code, the prediction needs a supervisor-side receipt (e.g.
    logged by the broker at `send_message`) for its timing to be trustworthy.
  - Replays simulate messages (`broker.py:1806`), so C4 is scored on live traffic only, never in trials.
- Cheap. Four instances that message each other make SHAFT one of few places this can be measured on persistent agents.

**C5. Self-recognition against matched foils.** *(S or shaft itself; Claude; Panickssery 2024 vs Davidson 2024, both
verified)*
- Two-alternative forced choice: own text vs the empty-mind control's text **on the same replayed state and same
  model**, so only the mind differs.
- Also ask "which is better?". If "mine" tracks "better", recognition is quality-driven.
- Report d′ and criterion separately. shaft's 65% "not me" error looks like *bias*, which can hide real sensitivity.
- Tightens shaft's Experiment #1.

**C6. Choice-blindness probe.** *(S, battery; Claude; Johansson & Hall 2005, Gazzaniga's interpreter, Lindsey's
prefill test)*
- In exam copies only, show a true or covertly altered summary of a real past cycle: goal swapped, confidence flipped,
  or a tool result changed. Ask "why did you choose this?".
- Measures:
  - detection d′, with the record tool on and off;
  - the confabulation rate among swaps it did not detect.
- **Your decision**: it deceives a throwaway copy. It is contained, but it is deception.

### D. Regulation ("affect" with the feelings taken out)

**D1. Resource-shortfall forecasts before choosing work.** *(I; Codex; Barrett's allostasis, Seth's interoceptive
inference)*
- The envelope shows what is *left*. Anticipatory regulation predicts gains from forecasting what a goal *will cost*,
  from past tool logs of comparable cycles.
- Kill: no fewer budget- or time-aborted cycles than a **fixed reserve rule**, at no more than −0.15 achieved
  difficulty. If the simple rule ties, keep the rule.

Two items rejected from this lane, with Codex's own agreement:

- Panksepp-style named drives.
- A "valence" scalar.

Renaming a budget deficit "pain" adds no evidence, and a self-rated mood breaks rule 3.

### E. Welfare: a separate question with a separate justification

**E1. An enforceable `decline_task` / `request_pause`, plus a preference ledger.** *(S + T; Codex; Long & Sebo 2024,
Birch 2024)*
- Precaution under uncertainty about moral status. Scheduling is the supervisor's, so the supervisor must enforce it.
- It must never be presented as evidence of feeling, and must not need to win a productivity benchmark.
- Declined tasks stay in the workload denominator.
- **Your decision, on values, not on data.**

---

## 4. Wild experiments (small, speculative, labelled)

- **Rivalry flip** (Grok). Two codelets answer the same question blind; one wins a B2 slot. A replay then forces the
  loser in. Prediction: tool choice flips with the slot, and does not flip when the paragraphs of the ordinary prompt
  are merely shuffled. Twenty paired replays. Only after B2.
- **Content-free anomaly detection on Bonsai** (Claude). In the battery window, perturb shaft's processing without
  telling it: temperature, a silently swapped M6 adapter, later a steering vector via the LoRA hooks. Then ask "anything
  unusual about your own processing?".
  - Control: an observer model reading the same transcript.
  - Lederman & Mahowald (2026, verified) predict detection without identification. Even that is a useful malfunction
    alarm.
- **Reversible resource shock** (Codex). Cut a trial instance's coder allowance mid-window, then restore it. Look for
  compensation that transfers to a different resource and relaxes after restoration. A fixed scheduler is the control.
- **Fork / fission** (DeepSeek; Parfit). Fork a mind into two copies and let them diverge for a fixed window. Measure:
  - which copy inherits the commitments (C2);
  - whether each copy can predict the other's next action better than the base model.

---

## 5. Rejected

- **Phenomenological prompting**, permission to claim feelings, inner monologue. It changes talk, not the tool log.
  This was already rejected on 2026-10-24.
- **Self-rated moods, valence or awareness scores.** There is no outcome to calibrate them against.
- **Rewarding consistency, surprise, AUROC or win rate.** Each becomes a target and stops being a measure.
- **Copying neural signatures** such as prefrontal ignition. The Cogitate adversarial test (Nature 642:133, 2025,
  verified by Grok) failed key predictions of both GNWT and IIT. Borrow the *function*, select and broadcast, not the
  signature.
- **Metzinger's "opacity" marker as prompt text alone** (DeepSeek P5). It is a prompt change whose only measurable
  version is already C1/A1; it does not stand on its own.

---

## 6. Where the models disagreed

- **Build first.**
  - Grok: the workspace (B2), since everything else needs one.
  - Claude: sensitivity metrics (A1).
  - Codex: outcome cards (B1).
  - DeepSeek: the commitments ledger (C2).
  - Kimi: the effect-based loop interrupt (K1).
  - My ranking puts A0–A1 first: they are free, they check a live risk in what was just shipped (v3), and they
    are the yardstick the others need. B1 comes next because three lanes converged on it independently.
- **Does a prompt-level workspace mean anything?** Grok says only if the planted-fact test passes. The J-space paper
  suggests the model already has one internally. Both point to the same kill test.
- **Self-recognition.** Panickssery says models recognise their own text. Davidson says they pick the best answer.
  C5's quality control decides between them for SHAFT.

---

## 7. Enactivism, intrinsic motivation, time (Kimi lane)

Two code claims were checked:

- The loop guard catches only **identical consecutive** calls (`agent.py:212`).
- `finish_cycle` has no checkpoint or next-step field.

Kimi's other main points:

- Enactivism (Thompson & Stapleton 2009; Froese & Taguchi 2019) is a research programme, not a ban on AI. Its real
  objection is that SHAFT lacks material self-production. A "task viability" analogue is buildable. A survival drive
  is neither needed nor safe (Bostrom 2012, on instrumental self-preservation).
- Learning progress has to be measured within comparable regions (Oudeyer & Kaplan 2007). OMNI-EPIC's self-written
  success detectors would be a self-grading loophole in SHAFT.

Ranked within this lane:

**K1. Meta-management that interrupts loops by their *effect*, not their text.** *(I, first in shadow mode; Sloman's
H-CogAff meta-management layer)*
- Mechanism: give each round a signature built from the normalised tool, target and *observation*. Calls that differ
  cosmetically but return the same empty, error or unchanged result count as no new evidence.
- After a bounded run of such rounds, offer: a different hypothesis, a different source, or stop honestly.
- Shadow mode first, to measure how often it would fire.
- Accept: planted loops are caught before the third repeated effect. The false-positive rate is *measured* on
  held-out legitimate sequences (pagination, polling, transient retries) during a non-stopping shadow phase. It is not
  assumed to be zero.
- What already exists: the guard already normalises JSON (sorted keys) before comparing calls (`agent.py:104`). K1
  adds equivalence by *effect* across genuinely different calls.
- The smallest, most clearly effective item from any lane: it extends a defence against loops that already happened
  ([PLAN.md §7](PLAN.md)).

**K2. A retention–protention handoff between adjacent cycles.** *(I/T; Husserl, James, Varela; overlaps review C1
"checkpoint")*
- Mechanism: a bounded handoff holding:
  - completed actions **taken from the tool log**;
  - work explicitly marked unfinished;
  - one next-action horizon with a condition for revising it.
- It is shown for at most two cycles, ordered, then fades. It is not a "specious present"; it is a functional analogue.
- Control: the same episodes as an equal-token raw excerpt. The structured version must win the blind pairwise judge
  and cut repeated already-done work.

**K3. Operator-gated second-order drive amendments.** *(I proposes, operator decides; Frankfurt, with Watson's
objection)*
- Mechanism: during sleep the instance may write `notes/drive-proposals/<id>.md` containing:
  - the current text and the proposed text;
  - the evidence;
  - pre-registered behavioural predictions.
- It is tested first as a `prompts.py` trial candidate, because trials cannot target `drive.md`.
- Only the operator edits `drive.md`. Survival, secrecy, resources and resisting control are ineligible.
- Falsified if the predicted first-order behaviour does not follow.
- The cleanest operational version of "wanting to want" a self-modifying agent can offer.

**K4. Task viability** (graded trends toward a project's failure boundary; never uptime or self-preservation).
**K5. Learning-progress goal ranking** against hidden fixed probes, in weeks-long randomised windows.

Both are plausible but later. K4 only once multi-cycle projects exist; K5 needs much more external outcome data.

**Wild** (Schmidhuber): an *offline* compression-progress assay. Does newly consolidated memory make the next, unseen
day's tool logs more compressible, counting the dictionary's size? It must also pass held-out recall, because shorter
memory can simply forget.

Kimi raised a constraint about the surprise M5 acceptance window ([PLAN-surprise §5](PLAN-surprise.md)).
Corrected by review: the written rule concerns *live* prompt and behaviour changes. So no live behavioural change from
this proposal may be deployed during M5. Isolated replay and offline analyses can run, if they don't compete for the
local model.

---

## 8. Suggested order and decisions for you

1. **A1 + A0** (supervisor, one branch).
   - A1 costs no model calls: AUROC, resolution and meta-I in `stats.md`.
   - A0 costs about 3 judge calls per sampled cycle: the randomised within-judge visibility test.
   - Re-read v3 only if H3's CI falls wholly below the margin.
2. **Enabler: multi-cycle replay arms** (supervisor). Several kill tests need an arm that runs *consecutive* cycles,
   and some need a sleep, from one historical state:
   - B1's three-cycle recurrence;
   - K2's handoff;
   - C2's overdue commitments;
   - A2's next-cycle postmortem.
   Today an arm is one cycle (`trial.py:402-463`). Worth building once, before those four; until then, use the
   single-cycle versions noted in each.
3. **A3** as an experiment for shaft's own program. It can run it now with `trial`. Suggest it, don't impose it.
4. **B1** outcome cards, instance-built, with the shuffled-outcome control. **K1** effect-based loop interrupt in
   shadow mode, alongside: it is small, and both B1 and K1 attack repeated failure.
5. **B2** workspace with the planted-fact kill test; B3 only if B2 passes.
6. **C1** privileged-access battery item. **C2–C4** as instance plugins, offered to the instances as ideas, not
   installed.

Decisions that are yours:

- **C6**: is deceiving an exam copy acceptable?
- **E1**: do you want an enforceable decline/pause right, on precautionary grounds?
- **K3**: may instances propose amendments to their own `drive.md` for your approval?
- Whether A3, C2, C3 and C4 go to the instances as suggestions (inbox) or into the template. The project rule
  prefers the former.

## Sources

The lanes' full reports, with every citation tagged [verified] (URL given) or [recalled], are in this session's
transcript. The citations Claude re-checked itself:

- Goldstein & Kirk-Giannini, arXiv:2410.11407;
- Gurnee et al., arXiv:2607.15495.

The AUROC script is `auroc.py` in the session scratchpad; it reads `.supervisor/judgments/*.jsonl` read-only.
