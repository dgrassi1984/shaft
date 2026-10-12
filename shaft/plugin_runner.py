"""Run one plugin in its own process: `python -m shaft.plugin_runner <name>` with JSON args on stdin.

A plugin is plugins/<name>/plugin.json (name, description, JSON-schema `parameters`)
plus plugins/<name>/main.py defining `run(**args) -> str`. Plugins may call the
supervisor's tools through `from shaft import broker`.

`python -m shaft.plugin_runner --check` validates every plugin (used by the supervisor's gate).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path.cwd()


def load(name: str):
    folder = ROOT / "plugins" / name
    spec = json.loads((folder / "plugin.json").read_text())
    for key in ("name", "description", "parameters"):
        if key not in spec:
            raise ValueError(f"plugins/{name}/plugin.json: missing {key!r}")
    if spec["name"] != name:
        raise ValueError(f"plugins/{name}/plugin.json: name {spec['name']!r} must match folder")
    if spec["parameters"].get("type") != "object":
        raise ValueError(f"plugins/{name}/plugin.json: parameters must be a JSON-schema object")
    module_spec = importlib.util.spec_from_file_location(f"plugin_{name}", folder / "main.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise ValueError(f"plugins/{name}/main.py: no callable run()")
    return module


def check() -> int:
    bad = 0
    for manifest in sorted((ROOT / "plugins").glob("*/plugin.json")):
        try:
            load(manifest.parent.name)
            print(f"ok   {manifest.parent.name}")
        except Exception as e:
            bad += 1
            print(f"BAD  {manifest.parent.name}: {e}", file=sys.stderr)
    return 1 if bad else 0


def main() -> int:
    sys.path.insert(0, str(ROOT))
    if sys.argv[1:] == ["--check"]:
        return check()
    name = sys.argv[1]
    args = json.loads(sys.stdin.read() or "{}")
    result = load(name).run(**args)
    sys.stdout.write(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
