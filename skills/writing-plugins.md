# Writing my own plugins (tools)

A plugin is a new tool I give myself. It lives in `plugins/<name>/`:

1. `plugin.json` — `{"name": "<name>", "description": "...", "parameters": {"type": "object", "properties": {...}, "required": [...]}}`. The name must equal the folder name and must not clash with a built-in tool.
2. `main.py` — defines `run(**params) -> str`. Whatever it returns is what I see.

To use the supervisor's tools from a plugin: `from shaft import broker` then `broker.call("web_search", query=...)` or `broker.call("fetch_page", url=...)`. I have no other way to reach the internet.

## Building and testing one in the same run

With the `run` tool (shell commands in my folder, no network):

- check every plugin the way the supervisor will: `python3 -m shaft.plugin_runner --check`
- try mine with real arguments: `echo '{"text": "hello"}' | python3 -m shaft.plugin_runner <name>`
  (a plugin that calls `broker.call` cannot reach the broker from `run`; test its own logic instead —
  e.g. put that logic in a function and call it from a small script or a test in `tests/`)
- tests I write in `tests/` (`test_*.py`, unittest) also run in the supervisor's gate after my run.

When I have `delegate_coding` (the operator can switch it off for me), anything beyond a few lines
goes to it: an Opus-class coder writes it faster and more reliably than I do on the local model. I
decide what the plugin should do and why, name the files and how to tell it works; the supervisor
applies its change only if all tests pass, and I check the result with `run`. Small edits I still
make myself.

After my run the supervisor checks every plugin (it must load, have a valid plugin.json and a `run`). A broken plugin is reverted and I am told why in my journal. A plugin I write becomes available as a tool in my *next* run, not the current one.

Example: `plugins/web_research/`.
