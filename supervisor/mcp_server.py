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
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")   # what initialize may answer: the asked one if here
DESKTOP_CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"

TOOLS = [
    {"name": "shaft_instances",
     "description": "The SHAFT instances (the operator's own autonomous agents on this Mac) that accept notes from "
                    "Claude. Call it before shaft_send when you do not know the instance's exact name.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "shaft_send",
     "description": (
         "Leave a note in a SHAFT instance's inbox, which it reads at its next wake. Use it only when the operator asks "
         "you to send or tell something to an instance (\"send this to <instance>\", \"let <instance> know\"). Write the note "
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
        if r.get("logged"):
            said += f" It is delivered, but SHAFT could not add it to its message log ({r['logged']})."
        return said
    raise KeyError(name)


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                    # a notification (notifications/initialized, cancelled, …)
        return None
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object"}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        from .version import supervisor_commit
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in SUPPORTED else PROTOCOL,
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
            try:
                reply = handle(msg) if isinstance(msg, dict) else \
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
            except Exception as e:                     # one bad message never ends the server Claude talks to
                reply = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                         "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


# -- registration ------------------------------------------------------------------------------------------------------
def _entry() -> dict:
    """How Claude starts it: the primary checkout's launcher, with this Python (a GUI app's PATH may have none)."""
    return {"command": str(core.PROJECT / "bin" / "shaft"), "args": ["mcp", "serve"],
            "env": {"SHAFT_PYTHON": sys.executable}}


def _desktop(change) -> str:
    """Change Claude Desktop's config (the rest of it kept): written only when it changes, through a private
    temporary file that keeps the config's own permissions, and refused if the file changed while it was read."""
    import tempfile
    path = DESKTOP_CONFIG
    if not path.parent.is_dir():
        return "Claude Desktop: not installed (no config folder)"
    try:
        raw = path.read_bytes() if path.exists() else b"{}"
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        return f"Claude Desktop: NOT changed, cannot read {path} ({e})"
    if not isinstance(data, dict):
        return f"Claude Desktop: NOT changed, {path} is not a JSON object"
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    before = json.dumps(servers, sort_keys=True)
    said = change(servers)
    if json.dumps(servers, sort_keys=True) == before:
        return f"Claude Desktop: {said} ({path})"
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(json.dumps(data, indent=2) + "\n")
        if (path.read_bytes() if path.exists() else b"{}") != raw:
            return f"Claude Desktop: NOT changed, {path} changed while SHAFT edited it; run it again"
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
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
