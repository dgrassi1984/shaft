# Writing my own plugins (tools)

A plugin is a new tool I give myself. It lives in `plugins/<name>/`:

1. `plugin.json` — `{"name": "<name>", "description": "...", "parameters": {"type": "object", "properties": {...}, "required": [...]}}`. The name must equal the folder name and must not clash with a built-in tool.
2. `main.py` — defines `run(**params) -> str`. Whatever it returns is what I see.

To use the supervisor's tools from a plugin: `from shaft import broker` then `broker.call("web_search", query=...)` or `broker.call("fetch_page", url=...)`. I have no other way to reach the internet.

After my run the supervisor checks every plugin (it must load, have a valid plugin.json and a `run`). A broken plugin is reverted and I am told why in my journal. A plugin I write becomes available in my *next* run, not the current one.

Example: `plugins/web_research/`.
