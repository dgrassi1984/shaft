# SHAFT — project rules

- **Instances build their own tools and plugins.** `plugins/<name>/plugin.json` + `main.py`
  (`run(**args) -> str`, may call supervisor tools via `broker.call`), checked by the gate and testable
  in-sandbox with the broker `run` tool. No change to the sandbox, broker, gate, tool registry or
  prompts may weaken that path; before adding a supervisor tool, ask whether an instance could build it.
- **Judges grade effectiveness first** (useful/correct beats "sounds like the instance").
- The supervisor never imports agent code; instances never see secrets; the API stays tailnet-only.
- A conversation holds the instance only while it replies; open conversations are registered
  (`runner.open_conversations`) and are never written up while still going on.
