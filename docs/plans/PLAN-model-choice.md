# PLAN-model-choice: instances consult other models, on their own allowance

Status: **decided 2026-10-27 (the operator); building M0–M2.** Source: the brainstorm in
`PROPOSAL-model-choice-2026-10-27.md` (Codex, Grok and Claude lanes). Decisions:

1. **A separate consult allowance, $3/day per instance.** An instance may ask the operator for more; the operator
   decides (`shaft limits <name> consult-cap <usd>`).
2. **Caps stay apart:** cloud (the main model), coding and consult each keep their own ledger and cap.
3. **The menu**, all through OpenRouter:

   | Model | Input $/M | Output $/M | Zero-retention providers (2026-10-27) |
   |---|---|---|---|
   | `anthropic/claude-opus-5.5` | 4.00 | 20.00 | Bedrock, Google |
   | `anthropic/claude-sonnet-5.5` | 2.00 | 10.00 | Bedrock, Google |
   | `moonshotai/kimi-k3.1-pro` | 2.00 | 10.00 | Moonshot AI |
   | `google/gemini-4-argon` | 2.00 | 10.00 | Google |
   | `openai/gpt-6.1-sol` | 2.00 | 10.00 | Azure |
   | `openai/gpt-6.1-terra` | 1.00 | 5.00 | **none** |
   | `meta/muse-spark-1.3` | 1.25 | 4.25 | **none** |
   | `z-ai/glm-5.3` | 0.07 | 1.40 | many |
   | `minimax/minimax-m3.1-flash` | 0.15 | 0.60 | several |

   The data rule stays: no data collection, zero retention. Terra and Muse Spark have no qualifying provider, so
   the menu shows them as unavailable and a call to either fails saying why. **Decided 2026-10-27 (the operator): the
   zero-retention rule stays; Terra and Muse Spark stay unavailable.** They become usable on their own if a
   qualifying provider appears in OpenRouter's zero-retention list (the catalogue is re-read daily).
4. **The instances build the router.** The supervisor provides a metered transport, the menu and the books, and
   nothing that decides.
5. **M1 and M2 now.**

## M0 — supervisor: transport, allowance, books (lane A)

- **`POST /llm/consult/v1/chat/completions`** on the run's broker, with the same token as `/llm`. Non-streaming
  only. The body is OpenAI-style (`model`, `messages`, `max_tokens`, `temperature`, …).
  - The broker sets the provider policy (`SHAFT_CONSULT_PROVIDER`); an instance cannot override it.
  - It asks OpenRouter for usage accounting.
  - The reply is OpenRouter's JSON plus `"shaft": {"usd", "spent_usd", "reserved_usd", "cap_usd"}`.
  - Header `X-Shaft-Caller` (free text, ≤ 60 characters, e.g. the plugin's name) is recorded.
- **Refused, with a reason:**
  - a scheduled job (`model_allowed` false);
  - any run not billed to the instance itself (`usage_name != name`: trial replays, the battery). Consults in
    replays need trial accounting first (M3);
  - a model not on the menu;
  - a menu model with no qualifying provider;
  - an unknown price (unpriced means uncapped);
  - `stream: true`.
- **Allowance:** `SHAFT_CONSULT_DAILY_USD` (3) per instance per day. The operator's override is
  `consult_daily_usd` in `supervisor/tools/<name>.json`, set with `shaft limits <name> consult-cap <usd>|off`.
- **Reservation:** under a file lock, before the call, `spent + open reservations + this call's worst case` must not
  exceed the cap.
  - Worst case = prompt characters / 3 × input price + `max_tokens` × output price. `max_tokens` defaults to and is
    capped at `SHAFT_CONSULT_MAX_TOKENS` (8000).
  - After the call the reservation is released and the real cost recorded: OpenRouter's `usage.cost`, or the worst
    case if no cost came back, never $0.
  - Stale reservations (> 30 min) lapse.
  - The 429 says spent, reserved, this call's worst case and the cap, and that the operator can raise it.
- **Catalogue:** prices and zero-retention availability come from OpenRouter's public `/api/v1/models` and
  `/api/v1/endpoints/zdr`. They are cached in `.supervisor/consult_catalogue.json` and refreshed when older than
  24 h. A failed refresh keeps the stale cache; with no cache at all, calls are refused.
- **Ledger** `.supervisor/consult_usage.jsonl`, one row per call:
  `{"ts", "instance", "model", "caller", "run", "run_started", "input_tokens", "output_tokens", "usd",
  "reserved_usd", "seconds", "error"}`.
  - `model` is the menu name (`openrouter/<vendor>/<id>`).
  - `run` and `run_started` are the broker's run kind (`live`, `sleep`, `talk`, `api`, …) and its start time, set
    by `runner._launch`.
- **Envelope** gains `"consult": {"spent_usd", "reserved_usd", "cap_usd", "models": [{"model", "in_per_m",
  "out_per_m", "available", "why"}]}`.
- **`record(kind="consults")`**: the instance's own consult rows of the last 7 days, as readable lines.
- `shaft delete` removes the instance's rows from the shared ledger; `docs/configuration.md` and `docs/cli.md` are
  updated.

**Settled by the reviews** (Claude's review of the Codex lane; Grok's review of the whole diff, 2026-10-27):

- **Text only:** image, audio and file parts are refused, because they bill input the worst case cannot see.
- **No paid extras:** `plugins`, `web_search_options` and `modalities` are refused. `reasoning.max_tokens` is
  clamped to `max_tokens`.
- **One attempt:** one POST with a 300 s deadline, so the reservation (30 min) always outlives the call. An outright
  4xx (not 429) costs $0. A reported cost of 0 or none records the worst case. A cost above its reservation is
  recorded as reported and logged.
- **Prices:** the menu shows list prices; the reservation uses the dearest qualifying provider.
- **The envelope never fetches:** it reads the cached catalogue and refreshes a stale one in the background.
- **Exact attribution:** the episode records `run_started` (`SHAFT_RUN_STARTED`). A consult belongs to the cycle
  with the same run start. A run killed before writing an episode is booked to no cycle.
- **Judges:** a call that was sent and failed still counts as consulted for judge exclusion. The second judge is
  skipped on a cycle that consulted its vendor.

## M1 — the instances build it (template, Claude)

- `skills/choosing-a-model.md`. It covers:
  - the endpoint and an example plugin skeleton (not a finished plugin);
  - the menu in the envelope;
  - the evidence: one model per bounded subtask, never per turn; escalate a failed subtask once; check a helper's
    answer; your own confidence is not a gate; consulting is not free;
  - how to ask the operator for more allowance (`send_message`, with the evidence).
  - **2026-10-30:** a cheaper model is rarely cheaper than the instance's own. In three days no instance had
    consulted. The page had steered "summaries, extraction, format conversion" to a cheap model, with GLM-5.3 as
    the example default. A cached baseline can be cheaper than a cold call to a nominally cheaper model;
    compare total input, output and cache-read prices, including reasoning effort.
    The page now gives the whole-consult arithmetic with dated prices, names ability and independence as the
    strong reasons, and the example has no default model.
- The prompt's operational envelope gains one line: the consult allowance left and the menu with prices.

## M2 — measure (lane B module, Claude integration)

- `supervisor/consultstats.py`, read-only over the ledger, episodes and judgments:
  - `section(name)`: a `stats.md` section, empty until there is a consult in the last 7 days. It shows per-model
    calls and dollars; consulted vs not-consulted live cycles (success, value); dollars per met cycle; and a
    collapse line when one model has ≥ 90% of ≥ 20 consults in 7 days.
  - `review_line(name)` and `report(names)` for `shaft consults`.
- **Judges:** a live cycle's consulted models join `own_models`, matched by **vendor**: a cycle that consulted
  `z-ai/glm-5.3` is not graded by a `z-ai` judge. If that leaves no judge, the consult exclusion is dropped and
  logged, so a cycle is never left ungraded for it.

## M3 (later) — kill test

A paired replay, plugin vs no plugin, at matched spend. Needs consults in replays billed to the trial's
allowance.
