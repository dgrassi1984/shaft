# SHAFT — project rules

- **Instances build their own tools and plugins.** `plugins/<name>/plugin.json` + `main.py`
  (`run(**args) -> str`, may call supervisor tools via `broker.call`), checked by the gate and testable
  in-sandbox with the broker `run` tool. No change to the sandbox, broker, gate, tool registry or
  prompts may weaken that path; before adding a supervisor tool, ask whether an instance could build it.
- **Judges grade effectiveness first** (useful/correct beats "sounds like the instance").
- The supervisor never imports agent code; instances never see secrets; the API stays tailnet-only.
- A conversation holds the instance only while it replies; open conversations are registered
  (`runner.open_conversations`) and are never written up while still going on.
- Trial replays (supervisor/trial.py) never settle, commit, deliver or send: the ReplayBroker answers writes as
  a real cycle would and records them; a trial never takes an instance's time on the local model.
- An instance reads the operator's mail only through the broker's mail tools: it never holds a mail credential, and the
  supervisor checks the granted label on every thread it returns, reads or downloads from (docs/plans/PLAN-mail.md).
- An instance reads the operator's Drive only through the broker's Drive tools: it never holds a Drive credential on
  the operator's account, and the supervisor checks the grant and reach on every file (docs/plans/PLAN-operator-drive.md).
- Model choice for an instance (bake-offs, `shaft trial models`): read docs/bakeoffs/README.md first. Reuse its
  results, cycle sets, candidate screen and subagent-judging path; re-run a settled model only when its version, the
  instance's loop or the budget changed. Every new bake-off adds its report and ledger row there.
