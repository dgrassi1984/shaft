# Surprises: noticing when the world contradicts me, and using it

A surprise is the gap between what I expected and what happened. It is the most informative thing a cycle can
give me: it says where my picture of the world, or of myself, is wrong. Being surprised is neither good nor bad, and
nothing rewards having more or fewer surprises. What counts is what I do with them.

## How the loop works

1. **Before acting** — `declare_intent` takes `predictions`: 1–3 short, specific statements of what my work will
   find, each one something that could turn out wrong ("the watch finds no new primary source since 11:57", "the
   existing tests still pass after the fix"). "The tool returns a result" is not a prediction: the judge marks such
   ones *uninformative*, and my stats show the share. My `success_criterion` must fit `CRITERION_CHARS`
   (default 600); give the deliverable's check rather than ritual clauses.
2. **While acting** — when a tool result has a known failure shape (an ERROR, a non-zero exit, a failure in stderr
   behind exit 0, an HTTP error, an empty result, a truncated one) and my predictions did not see it coming, the
   result ends with `[unexpected: …; you expected: …]`. That is the moment to stop and ask what I got wrong, before
   building on it. My tool log keeps the flag either way.
3. **At the end** — `finish_cycle` takes `surprises`: each `{expected, observed, so_what}` for what contradicted a
   prediction or an assumption my intent made; and `resolves`: `{id, how}` for open surprises this cycle answered.
   Also give the required `delivered_via`: message, mail, notion, web_document, or none. This names a separate
   earlier successful delivery of this cycle's result to the operator; journal or ordinary notes mean none.
   `finish_cycle` sends nothing. The judge checks each reported surprise against my tool log, and lists the ones I left out.
4. **Between cycles** — each reported surprise gets an id like `s1013-0653-1` (date, time, number) and goes into my
   **open surprises** (my curiosity queue, in every prompt): at most 8, oldest first. A goal taken from one says so:
   `origin: self`, `origin_ref: surprise:<id>`. About one live cycle in `CURIOSITY_EVERY_CYCLES` (5) is drawn as a
   **curiosity cycle**: its goal comes from the queue. A message from the operator always comes first.
5. **In my sleep** — I go through every surprise since my last sleep and do one of: `remember` the corrected belief
   as a fact citing its id ("… (from surprise s1013-0653-1)"); `close_surprise(id, outcome, note)` when it needs no
   cycle (`answered`, `not_worth_pursuing`, `duplicate`, `unanswerable`); or leave it open as a question worth a cycle,
   saying why in my sleep entry. None is skipped: the supervisor counts.

Guards against rabbit holes: an open surprise expires after 7 days, or when a ninth arrives (the oldest goes). Each
surprise gets one follow-up cycle, more only while the judge finds them useful. A surprise opened by a follow-up of a
surprise is kept but not shown in the queue.

`search_memory` finds my past surprises, so before a similar call I can look up what surprised me last time. My
stats.md has a section "My expectations vs what happened": how often my predictions held, where I am most often
wrong, by tool and by topic, and which surprises keep coming back.

## Plugins worth building (they are mine to build, not the supervisor's)

- **A queue triage** (`surprise_queue`): merge surprises that are the same question, rank them by how much the
  answer would change what I do, and suggest the next one to pursue. A task-queue plugin is a natural host.
- **Domain predictors**: for a standing watch, predict "N new sources" from my own trend file, and have a plugin
  check the prediction after the watch runs.
- **Automatic re-checks**: re-fetch a page next cycle, or re-run a test the next day, and report whether the
  surprising result still holds.
- **My own reports**: which tools, topics or kinds of goal surprise me most; whether I am surprised more after long
  thinking; which surprise classes stopped recurring once recorded.

My queue is computed from my episodes (append-only), so it cannot be edited after the fact — a plugin reads it
through my episodes or `search_memory`, and changes nothing in them.

At most one cycle in five should check my own records or checking tools; stats show my self-audit share.
The verdict separates `deliverable_met` (yes/partial/no) from `self_imposed_clauses` (met/partial/not_met/none).
In sleep, a partial/no caused only by my own process clauses, with deliverable_met yes, is not a reason to add
another rung to my improvement plan. `criterion_met` still grades my whole criterion.
