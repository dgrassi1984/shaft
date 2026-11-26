"""Notes from the operator's Claude sessions (claude_notes.py) and SHAFT's local MCP server (mcp_server.py)."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from supervisor import claude_notes, core, mcp_server, messages
from supervisor.__main__ import main

SECRET = "supervisor-only-secret-value-123"


class NotesTest(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="shaft-claude-notes-")).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        self.sup, self.inst, self.home = base / "sup", base / "instances", base / "home"
        self.project = base / "project"
        for p in (self.sup, self.home, self.project):
            p.mkdir()
        for name in ("example", "other"):
            for d in ("shaft", "inbox"):
                (self.inst / name / d).mkdir(parents=True)
        self.woke = []
        for p in (mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(core, "INSTANCES", self.inst),
                  mock.patch.object(core, "PROJECT", self.project),
                  mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(messages, "INSTANCES", self.inst),
                  mock.patch.object(messages, "PROJECT", self.project),
                  mock.patch.object(claude_notes, "SUP_DIR", self.sup),
                  mock.patch.object(core, "instance_names", return_value=["other", "example"]),
                  mock.patch.object(claude_notes, "instance_names", return_value=["other", "example"]),
                  mock.patch.object(claude_notes, "instance_root", lambda n: self.inst / n),
                  mock.patch.object(messages, "instance_root", lambda n: self.inst / n),
                  mock.patch.object(core, "secrets_env", return_value={"SHAFT_API_KEY": SECRET, "SHORT": "abc"}),
                  mock.patch.object(claude_notes, "log"), mock.patch.object(messages, "log"),
                  mock.patch("supervisor.autonomy.event_wake", side_effect=lambda *a, **k: self.woke.append(a))):
            p.start()
            self.addCleanup(p.stop)

    def note(self, name="example"):
        files = sorted((self.inst / name / "inbox").glob("*-from-claude.md"))
        self.assertEqual(len(files), 1, files)
        return files[0].read_text()

    def test_off_until_the_operator_turns_it_on(self):
        with self.assertRaises(claude_notes.Refused) as e:
            claude_notes.send("example", "t", "hello")
        self.assertIn("shaft claude-notes example on", str(e.exception))
        self.assertEqual(list((self.inst / "example" / "inbox").iterdir()), [])
        core.set_claude_notes("example", True)
        self.assertEqual(claude_notes.instances(), ["example"])
        claude_notes.send("example", "Refactor", "Moved the parser out.")
        core.set_claude_notes("example", False)
        with self.assertRaises(claude_notes.Refused):
            claude_notes.send("example", "t", "again")

    def test_the_note_says_who_wrote_it_and_never_wakes(self):
        core.set_claude_notes("example", True)
        r = claude_notes.send("example", "Parser refactor", "Moved the parser out of agent.py.")
        text = self.note()
        self.assertIn("From: the operator's Claude session\n", text)
        self.assertIn("Kind: claude\n", text)
        self.assertIn("Title: Parser refactor\n", text)
        self.assertIn("Trust: written by Claude", text)
        self.assertTrue(text.rstrip().endswith("Moved the parser out of agent.py."))
        self.assertEqual(self.woke, [], "a Claude note is read at the next wake; it never starts one")
        row = messages.history()[-1]
        self.assertEqual((row["from"], row["to"], row["kind"], row["file"]), ("claude", "example", "claude", r["file"]))

    def test_secrets_are_scrubbed_from_the_note_and_text_files(self):
        # Bug guard: a summary pasting a key from a session would put it in the instance's inbox in clear.
        core.set_claude_notes("example", True)
        doc = self.home / "transcript.jsonl"
        doc.write_text(f'{{"msg": "key is {SECRET}", "token": "sk-abcdefghijkl"}}\n')
        r = claude_notes.send("example", "Deploy", f"Used {SECRET} and Bearer abcdef123456; GH ghp_12345678abcd",
                              [str(doc)])
        text = self.note()
        self.assertNotIn(SECRET, text)
        self.assertNotIn("abcdef123456", text)
        self.assertNotIn("ghp_12345678abcd", text)
        self.assertIn("[secret removed]", text)
        copied = self.inst / "example" / r["attachments"][0]
        self.assertNotIn(SECRET, copied.read_text())
        self.assertNotIn("sk-abcdefghijkl", copied.read_text())
        self.assertGreaterEqual(r["secrets_removed"], 5)
        self.assertNotIn(SECRET, (self.sup / "messages.jsonl").read_text())

    def test_attachments_land_in_the_inbox_never_committed(self):
        core.set_claude_notes("example", True)
        pic = self.home / "diagram.png"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        r = claude_notes.send("example", "", "See the diagram.", [str(pic)])
        rel = r["attachments"][0]
        self.assertTrue(rel.startswith("inbox/from-claude/"), rel)
        self.assertEqual((self.inst / "example" / rel).read_bytes(), pic.read_bytes())   # binary: copied as is
        self.assertIn(rel, self.note())
        self.assertEqual(len(list((self.inst / "example" / "inbox").glob("*.md"))), 1, "one message, files beside it")

    def test_files_that_may_never_go(self):
        core.set_claude_notes("example", True)
        (self.home / ".ssh").mkdir()
        cases = {"relative.txt": "absolute", str(self.home / "missing.txt"): "No such file"}
        for rel in (".env", ".env~", ".ssh/config", "server.pem", "id_ed25519"):
            path = self.home / rel
            path.write_text("x")
            cases[str(path)] = "never sent"
        inside = self.project / "instances" / "example" / "notes.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("x")
        cases[str(inside)] = "inside SHAFT"
        # the same file spelled in another case: this Mac's disk is case-insensitive, so it is the same folder
        upper = Path(str(inside).replace("/project/", "/PROJECT/"))
        if upper.exists():
            cases[str(upper)] = "inside SHAFT"
        for path, why in cases.items():
            with self.subTest(path=path), self.assertRaises(claude_notes.Refused) as e:
                claude_notes.send("example", "t", "body", [path])
            self.assertIn(why, str(e.exception))
        self.assertEqual(list((self.inst / "example" / "inbox").glob("*.md")), [], "a refused file sends nothing")

    def test_binary_files_holding_a_secret_are_refused(self):
        core.set_claude_notes("example", True)
        blob = self.home / "dump.bin"
        blob.write_bytes(b"\xff\xfe\x00" + SECRET.encode() + b"\x00")
        with self.assertRaises(claude_notes.Refused) as e:
            claude_notes.send("example", "t", "see", [str(blob)])
        self.assertIn("SHAFT's own secrets", str(e.exception))

    def test_a_secret_in_a_file_name_or_across_the_title_cut_is_removed(self):
        core.set_claude_notes("example", True)
        named = self.home / f"notes-{SECRET}.txt"
        named.write_text("plain")
        title = "x" * (claude_notes.TITLE_CHARS - 5) + " " + SECRET
        r = claude_notes.send("example", title, "body", [str(named)])
        listing = " ".join(p.name for p in (self.inst / "example" / "inbox").rglob("*"))
        for where in (listing, self.note(), (self.sup / "messages.jsonl").read_text(), json.dumps(r)):
            self.assertNotIn(SECRET, where)
            self.assertNotIn(SECRET[:10], where)

    def test_delivered_even_when_the_log_fails(self):
        core.set_claude_notes("example", True)
        with mock.patch.object(messages, "record", side_effect=OSError("disk full")):
            r = claude_notes.send("example", "t", "body")
        self.assertIn("disk full", r["logged"])
        self.assertIn("body", self.note())

    def test_the_note_appears_whole_or_not_at_all(self):
        core.set_claude_notes("example", True)
        seen = []
        real = core.safe_write_bytes
        def spy(root, rel, data, exclusive=False):
            out = real(root, rel, data, exclusive)
            seen.append(sorted(p.name for p in (self.inst / "example" / "inbox").glob("*.md")))
            return out
        with mock.patch.object(claude_notes, "safe_write_bytes", side_effect=spy):
            claude_notes.send("example", "t", "body")
        self.assertEqual(seen, [[]], "no .md is listed while the note is being written")
        self.assertIn("body", self.note())

    def test_limits(self):
        core.set_claude_notes("example", True)
        with self.assertRaises(claude_notes.Refused):
            claude_notes.send("example", "t", "   ")
        with self.assertRaises(claude_notes.Refused):
            claude_notes.send("nobody", "t", "x")
        big = self.home / "big.bin"
        with big.open("wb") as f:
            f.truncate(claude_notes.FILE_MAX + 1)
        with self.assertRaises(claude_notes.Refused):
            claude_notes.send("example", "t", "x", [str(big)])
        claude_notes.send("example", "t", "y" * (claude_notes.MAX_CHARS + 50))
        self.assertIn(f"[… cut at {claude_notes.MAX_CHARS} characters]", self.note())
        with mock.patch.object(claude_notes, "PER_HOUR", 1), self.assertRaises(claude_notes.Refused) as e:
            claude_notes.send("example", "t", "one more")
        self.assertIn("last hour", str(e.exception))

    def test_cli_turns_it_on_and_off(self):
        with redirect_stdout(io.StringIO()) as out, mock.patch("supervisor.__main__.instance_root",
                                                               lambda n: self.inst / n), \
                mock.patch("supervisor.__main__.log"):
            self.assertEqual(main(["claude-notes", "example", "on"]), 0)
            self.assertTrue(core.claude_notes("example"))
            self.assertEqual(main(["claude-notes", "example", "off"]), 0)
        self.assertFalse(core.claude_notes("example"))
        self.assertIn("example: no notes from Claude", out.getvalue())

    def test_web_access_page_switch(self):
        from supervisor import web_config
        with mock.patch.object(web_config, "_ran", return_value=(200, "ok")) as ran:
            web_config.run("claude-notes", "example", {"op": "on"})
        ran.assert_called_once_with(["claude-notes", "example", "on"])
        from supervisor.web_actions import Refused
        with self.assertRaises(Refused):
            web_config.run("claude-notes", "example", {"op": "maybe"})


class McpTest(NotesTest):
    def rpc(self, method, params=None, mid=1):
        return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_handshake_and_tools(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "claude", "version": "x"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["shaft_instances", "shaft_send"])
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        odd = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(odd["protocolVersion"], mcp_server.PROTOCOL)
        bad = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": ["x"]})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_send_through_the_tool(self):
        call = lambda **a: self.rpc("tools/call", {"name": "shaft_send", "arguments": a})["result"]
        refused = call(instance="example", summary="hi")
        self.assertTrue(refused["isError"])
        self.assertIn("Not sent", refused["content"][0]["text"])
        core.set_claude_notes("example", True)
        listed = self.rpc("tools/call", {"name": "shaft_instances", "arguments": {}})["result"]
        self.assertIn("example", listed["content"][0]["text"])
        sent = call(instance="example", title="Work", summary="Done the thing.")
        self.assertFalse(sent["isError"], sent)
        self.assertIn("example's inbox", sent["content"][0]["text"])
        self.assertIn("Done the thing.", self.note())
        bad = call(instance="example", summary="x", files="not-a-list")
        self.assertTrue(bad["isError"])

    def test_stdout_carries_only_protocol_messages(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}), "",
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), "not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
        out = io.StringIO()
        self.assertEqual(mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out), 0)
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)

    def test_install_registers_with_desktop_and_code_idempotently(self):
        config = self.home / "Claude" / "claude_desktop_config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"preferences": {"x": 1}}))
        runs = []
        fake = lambda args, **k: runs.append(args) or mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(mcp_server, "DESKTOP_CONFIG", config), \
                mock.patch.object(mcp_server.shutil, "which", return_value="/usr/local/bin/claude"), \
                mock.patch.object(mcp_server.subprocess, "run", side_effect=fake):
            first = mcp_server.install()
            again = mcp_server.install()
            data = json.loads(config.read_text())
            self.assertEqual(data["preferences"], {"x": 1}, "the rest of his Desktop config is kept")
            entry = data["mcpServers"]["shaft"]
            self.assertEqual(entry["command"], str(self.project / "bin" / "shaft"))
            self.assertEqual(entry["args"], ["mcp", "serve"])
            self.assertIn("added", first[0])
            self.assertIn("unchanged", again[0])
            self.assertIn(["/usr/local/bin/claude", "mcp", "add-json", "--scope", "user", "shaft",
                           json.dumps({"type": "stdio", **entry})], runs)
            config.chmod(0o600)
            before = config.stat().st_mtime_ns
            self.assertIn("unchanged", mcp_server.install()[0])
            self.assertEqual(config.stat().st_mtime_ns, before, "an unchanged registration is not rewritten")
            mcp_server.uninstall()
            self.assertEqual(config.stat().st_mode & 0o777, 0o600, "his config keeps its permissions")
            self.assertEqual(json.loads(config.read_text()), {"preferences": {"x": 1}})
            self.assertEqual(runs[-1], ["/usr/local/bin/claude", "mcp", "remove", "--scope", "user", "shaft"])


if __name__ == "__main__":
    unittest.main()
