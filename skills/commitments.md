# Promises to others

Use `commit_to(promise, due, criterion, made_to)` for a promise to the operator (`made_to="operator"`)
or another agent. For a peer, use its name from `list_instances`. Unknown names are refused.
If the peer list cannot be checked, retry later. An operator promise needs a source, and I do not name it: in a
conversation it is that conversation; in a live cycle it is the last message I delivered to the operator with
`send_message` in this cycle (its result names the `message_file`). A note I write myself, or an earlier
conversation, is not a source. Without one the promise is recorded as self with a note; if the broker cannot
confirm the message, nothing is recorded and I try again later. The same rule applies to `commitments` in
`finish_cycle`.

Own plans belong in goals/notes. Commitments to self fed the self-audit loop; the ledger keeps
only promises to others. This does not limit building tools or plugins for your work.

Give a deadline and a criterion someone else can check. Sleep settles overdue promises with
`settle_commitment`; kept promises need an episode as evidence. Old self promises remain in
`memory/commitments.jsonl`, but do not appear in prompts, automatic recall or sleep settlement.
They expire 24 hours after due, with status expired and their history kept. The supervisor counts them
and their expirations separately as "to self (no longer kept)".
