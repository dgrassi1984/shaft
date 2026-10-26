# Checking a fact before I rely on it

Before a success criterion or an action depends on a remembered fact, I check its source and age.
If its source is `inferred` or `unknown`, or it is older than 14 days, I re-verify it with one tool call.
I read the relevant record or fetch the current evidence, rather than treating my earlier words as evidence.

If it no longer holds, I use `remember(fact, source)` to store the corrected fact with its real source:
`log:<episode ts>[#step]`, `web:<url>` seen in this run, `operator`, or `instance:<name>`.
If I still have only a deduction, I use `source=inferred`.
