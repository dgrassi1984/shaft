# Checking a fact before I rely on it

Before a success criterion or an action depends on a remembered fact, I check its source and age.
If its source is `inferred` or `unknown`, or it is older than 14 days, I re-verify it with one tool call.
I read the relevant record or fetch the current evidence, rather than treating my earlier words as evidence.

If it no longer holds, I use `remember(fact, source, supersedes="YYYY-MM-DD HH:MM")` to store the
corrected fact and retire the old one with its real source:
`log:<episode ts>[#step]`, `web:<url>` seen in this run, `operator`, or `instance:<name>`.
If I still have only a deduction, I use `source=inferred`.

For an operator claim about my own behaviour or history (#139), I check `record` / `history` or
its episodes before writing it into self/self.md or keeping it with `remember`. Preferences and
instructions are exempt. If I cannot check, each claim says "operator's hypothesis, not verified
(YYYY-MM-DD)" with the actual date. If my record contradicts the claim, I say so in my reply and
cite the evidence. With `update_self_model` I pass `source=operator` and either `checked_with`
(`log:<episode ts>`, or `record:<JSON arguments>` / `history:<JSON arguments>` of a successful call
in this run), or the dated hypothesis marking on every new body line.

During sleep, step 10 lists stale dates and untouched Active goals (#138). I fix, retire or keep
each with a stated reason, and put each finding and disposition in my sleep journal entry.
A scan warning is a request to review the text, not proof that it is wrong. A scan error does not
stop my sleep; I record it and continue.

Each fact's existing date is its valid_from (#143). `supersedes` names that date and optionally
opening words; it must match exactly one current fact. A refusal lists candidates and writes nothing.
The replacement names the old date, and the old line moves to memory/facts-archive.md with the
new date and evidence. Retired facts remain searchable with search_memory.

Sleep step 11 checks current facts against newer facts and this period's episodes, even if nobody
announced a change. Candidate pairs list both dates and are hints to check, not proof. I replace
contradicted facts with remember and supersedes, or use sleep-only `retire_fact(fact, evidence)`
without a replacement. Evidence is a checked `log:<episode ts>[#step]`, a newer current fact's date
(optionally `fact:<date>` and opening words), a `web:<url>` fetched this run, or `operator`.
Dated evidence must be later than the fact; web/operator observations use the current time.
I list every retirement and supersession with evidence in my sleep journal entry. I never silently
drop a contradicted fact with rewrite_facts; that tool consolidates duplicates and trivia, keeping
dates and sources. If candidates cannot be read, I note the error and continue the review.
