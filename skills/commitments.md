# Promises to others

Use `commit_to(promise, due, criterion, made_to)` for a promise to Daniele (`made_to="operator"`)
or another agent. For a peer, use its name from `list_instances`. Unknown names are refused.
If the peer list cannot be checked, retry later. Operator promises do not need the broker.
The same rule applies to `commitments` in `finish_cycle`.

Own plans belong in goals/notes. Commitments to self fed the self-audit loop; the ledger keeps
only promises to others. This does not limit building tools or plugins for your work.

Give a deadline and a criterion someone else can check. Sleep settles overdue promises with
`settle_commitment`; kept promises need an episode as evidence. Old self promises remain in
`memory/commitments.jsonl`, but do not appear in prompts, automatic recall or sleep settlement.
The supervisor counts them separately as "to self (no longer kept)".
