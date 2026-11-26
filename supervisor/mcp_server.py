"""SHAFT's local MCP server for the operator's own Claude (`shaft mcp serve`; docs/integrations.md "Notes from Claude").

Claude Desktop and Claude Code start it as a stdio child on this Mac (`shaft mcp install` registers it with both), so
it runs as the operator and reaches nothing over a network. Its tools only write: a note into an instance's inbox
(claude_notes.send). It never reads anything of an instance back, and never wakes one.

  serve()      JSON-RPC 2.0 over stdin/stdout, one message per line (the MCP stdio transport); stdout carries only
               protocol messages.
  install()    registers `shaft mcp serve` in Claude Code (user scope, `claude mcp add`) and in Claude Desktop
               (claude_desktop_config.json, mcpServers.shaft); idempotent. uninstall() removes both.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import claude_notes
from . import core
from .core import log

NAME = "shaft"
PROTOCOL = "2025-06-18"
DESKTOP_CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"

TOOLS = [
    {"name": "shaft_instances",
     "description": "The SHAFT instances (the operator's own autonomous agents on this Mac) that accept notes from "
                    "Claude. Call it before shaft_send when you do not know the instance's exact name.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "shaft_send",
     "description": (
         "Leave a note in a SHAFT instance's inbox, which it reads at its next wake. Use it only when the operator asks "
         "you to send or tell something to an instance (\"send this to Thelos\", \"let Thelos know\"). Write the note "
         "for a reader who saw none of this conversation: what was done and why, what was decided, what is still open, "
         "and where the results are (file paths, branches, PRs, links). Facts, not instructions — the instance treats "
         "the note as information written by Claude, not as the operator's orders. Optionally attach files on this "
         "Mac by absolute path (documents, a transcript); text in the note and in text files is scrubbed of secrets."),
     "inputSchema": {"type": "object", "properties": {
         "instance": {"type": "string", "description": "the instance's name (shaft_instances lists them)"},
         "title": {"type": "string", "description": "a few words naming the work"},
         "summary": {"type": "string", "description": "the note itself, up to 20,000 characters"},
         "files": {"type": "array", "items": {"type": "string"},
                   "description": "absolute paths of files on this Mac to attach (at most 10, 20 MB each)"}},
         "required": ["instance", "summary"], "additionalProperties": False}},
]


def _call(name: str, args: dict) -> str:
    if name == "shaft_instances":
        names = claude_notes.instances()
        return ("Instances that accept notes: " + ", ".join(names)) if names else \
            "No instance accepts notes from Claude yet: the operator turns it on per instance (Access page in the SHAFT " \
            "app, or `shaft claude-notes <instance> on`)."
    if name == "shaft_send":
        files = args.get("files") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise claude_notes.Refused("files is a list of absolute paths")
        r = claude_notes.send(str(args.get("instance") or ""), str(args.get("title") or ""),
                              str(args.get("summary") or ""), files)
        said = f"Left in {r['instance']}'s inbox as {r['file']}; it reads it at its next wake."
        if r["attachments"]:
            said += f" Attached: {', '.join(r['attachments'])}."
        if r["secrets_removed"]:
            said += f" {r['secrets_removed']} secret(s) were removed before it was written."
        return said
    raise KeyError(name)


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                    # a notification (notifications/initialized, cancelled, …)
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        from .version import supervisor_commit
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": supervisor_commit()},
                   "instructions": "SHAFT runs the operator's own autonomous agents (instances) on this Mac. When he "
                                   "asks you to let one of them know about work done here, write it a self-contained "
                                   "note with shaft_send."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text, error = _call(name, args if isinstance(args, dict) else {}), False
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"no tool {name!r}"}}
        except claude_notes.Refused as e:
            text, error = f"Not sent: {e}", True
        except Exception as e:                        # reported to Claude, logged here: never a silent failure
            log(f"mcp: {name} failed: {type(e).__name__}: {e}")
            text, error = f"Not sent: SHAFT failed ({type(e).__name__}: {e})", True
        return ok({"content": [{"type": "text", "text": text}], "isError": error})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


# -- registration ------------------------------------------------------------------------------------------------------
def _entry() -> dict:
    """How Claude starts it: the primary checkout's launcher, with this Python (a GUI app's PATH may have none)."""
    return {"command": str(core.PROJECT / "bin" / "shaft"), "args": ["mcp", "serve"],
            "env": {"SHAFT_PYTHON": os.environ.get("SHAFT_PYTHON") or sys.executable}}


def _desktop(change) -> str:
    path = DESKTOP_CONFIG
    if not path.parent.is_dir():
        return "Claude Desktop: not installed (no config folder)"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as e:
        return f"Claude Desktop: NOT changed, {path} is not valid JSON ({e})"
    servers = data.setdefault("mcpServers", {})
    said = change(servers)
    if not servers:
        data.pop("mcpServers")
    tmp = path.with_name(path.name + ".shaft-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return f"Claude Desktop: {said} ({path}; restart Claude Desktop to load it)"


def _claude_code(args: list[str]) -> str:
    exe = shutil.which("claude")
    if not exe:
        return "Claude Code: not found on PATH"
    r = subprocess.run([exe, "mcp", *args], capture_output=True, text=True, timeout=60)
    return f"Claude Code: {'ok' if r.returncode == 0 else 'FAILED'} — {(r.stdout or r.stderr).strip()[:300]}"


def install() -> list[str]:
    entry = _entry()

    def put(servers: dict) -> str:
        was = servers.get(NAME)
        servers[NAME] = entry
        return "unchanged" if was == entry else ("updated" if was else "added")
    lines = [_desktop(put)]
    _claude_code(["remove", "--scope", "user", NAME])           # absent the first time: its failure is expected
    lines.append(_claude_code(["add-json", "--scope", "user", NAME, json.dumps({"type": "stdio", **entry})]))
    return lines


def uninstall() -> list[str]:
    def drop(servers: dict) -> str:
        return "removed" if servers.pop(NAME, None) else "was not registered"
    return [_desktop(drop), _claude_code(["remove", "--scope", "user", NAME])]


def command(action: str) -> int:
    if action == "serve":
        return serve()
    if action in ("install", "uninstall"):
        lines = install() if action == "install" else uninstall()
        print("\n".join(lines))
        return 1 if any("FAILED" in l or "NOT changed" in l for l in lines) else 0
    names = claude_notes.instances()
    print("instances that take notes from Claude: " + (", ".join(names) or "none (shaft claude-notes <name> on)"))
    print(f"Claude Desktop config: {DESKTOP_CONFIG}")
    return 0
