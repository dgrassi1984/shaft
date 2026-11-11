"""Claim lint checks real temporary records without model calls or instance code imports."""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import claimlint, commitments, core, metrics

CID = "c1111-1000-1"
TS = "2026-11-11T11:00:00"


def call(tool, args=None, result="done"):
    return {"tool": tool, "args": args or {}, "result": result}


def episode(channel=None, calls=(), ts=TS):
    return {"kind": "live", "ts": ts, "run_started": "2026-11-11T10:30:00",
            "finish": {} if channel is None else {"delivered_via": channel}, "tools": list(calls)}


class ClaimlintTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-claimlint-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "ember"
        (self.root / "memory/episodes").mkdir(parents=True)
        for module in (core, commitments, metrics):
            self.enterContext(mock.patch.object(module, "INSTANCES", self.root.parent))
            self.enterContext(mock.patch.object(module, "SUP_DIR", self.root.parent / "sup"))

    def write_episodes(self, episodes):
        (self.root / "memory/episodes/day.jsonl").write_text(
            "".join(json.dumps(ep) + "\n" for ep in episodes))

    def settle(self, evidence=TS + "#1", at="2026-11-11T10:45:00"):
        made = {"event": "made", "id": CID, "at": "2026-11-11T10:00:00", "source": "cycle",
                "promise": "Deliver the report", "criterion": "The report is delivered", "made_to": "operator",
                "due": "2026-11-12T10:00:00"}
        settled = {"event": "settled", "id": CID, "at": at, "status": "kept", "evidence": evidence}
        (self.root / commitments.FILE).write_text(json.dumps(made) + "\n" + json.dumps(settled) + "\n")

    def test_message_requires_successful_send_to_operator(self):
        for calls, expected in (([], 1), ([call("send_message", {"to": "ember"})], 1),
                                ([call("send_message", {"to": "operator"})], 0),
                                ([call("send_message", '{"to":"operator"}')], 0)):
            with self.subTest(calls=calls):
                self.assertEqual(len(claimlint.lint("ember", episode("message", calls))), expected)
        for result in (*commitments.FAILED, "ERROR: refused", None, {}):
            with self.subTest(result=result):
                ep = episode("message", [call("send_message", {"to": "operator"}, result)])
                findings = claimlint.lint("ember", ep)
                self.assertEqual(findings[0]["check"], "delivered_via")
                self.assertEqual(findings[0]["ref"], "message")
                self.assertEqual(findings[0]["episode_ts"], TS)

    def test_none_absent_and_unknown_delivery_claims_are_ignored(self):
        for channel in (None, "none", "unknown"):
            with self.subTest(channel=channel):
                self.assertEqual(claimlint.lint("ember", episode(channel)), [])

    def test_open_candidates_are_conservative_and_reads_do_not_count(self):
        for channel in ("mail", "notion", "web_document"):
            for tool in ("report_plugin", "http_request", "run"):
                with self.subTest(channel=channel, tool=tool):
                    self.assertEqual(claimlint.lint("ember", episode(channel, [call(tool)])), [])
            reads = [call("mail_read"), call("read_file"), call("view_image"), call("finish_cycle")]
            self.assertEqual(len(claimlint.lint("ember", episode(channel, reads))), 1)
            self.assertEqual(len(claimlint.lint("ember", episode(channel, [call("report_plugin", result="ERROR")]))), 1)

    def test_mail_drafts_only_support_mail(self):
        for tool in ("mail_draft", "mail_draft_update"):
            self.assertEqual(claimlint.lint("ember", episode("mail", [call(tool)])), [])
            self.assertEqual(len(claimlint.lint("ember", episode("notion", [call(tool)]))), 1)

    def test_web_document_requires_successful_write_under_docs(self):
        for tool in ("write_file", "append_file"):
            ep = episode("web_document", [call(tool, {"path": "notes/docs/report.md"})])
            self.assertEqual(claimlint.lint("ember", ep), [])
            ep = episode("web_document", [call(tool, {"path": "notes/report.md"})])
            self.assertEqual(len(claimlint.lint("ember", ep)), 1)

    def test_kept_evidence_rejects_bookkeeping_but_needs_no_judge(self):
        self.settle()
        for tool, expected in (("finish_cycle", 1), ("send_message", 0)):
            with self.subTest(tool=tool):
                ep = episode(calls=[call(tool)])
                self.write_episodes([ep])
                findings = claimlint.lint("ember", ep)
                self.assertEqual(len(findings), expected)
                if findings:
                    self.assertEqual(findings[0]["check"], "kept_commitment")
                    self.assertEqual(findings[0]["ref"], CID)
                    self.assertIn("no successful tool call", findings[0]["what"])

    def test_kept_settlement_window_excludes_earlier_cycle_and_start(self):
        ep = episode(calls=[call("finish_cycle")])
        self.write_episodes([ep])
        for at, expected in (("2026-11-11T10:20:00", 0), (ep["run_started"], 0), (TS, 1),
                             ("2026-11-11T11:01:00", 0)):
            with self.subTest(at=at):
                self.settle(at=at)
                self.assertEqual(len(claimlint.lint("ember", ep)), expected)

    def test_missing_run_start_uses_settlement_call_id(self):
        self.settle()
        ep = episode(calls=[call("settle_commitment", {"id": CID})])
        del ep["run_started"]
        self.write_episodes([ep])
        self.assertEqual(len(claimlint.lint("ember", ep)), 1)
        ep["tools"] = [call("settle_commitment", {"id": "c1111-1000-2"})]
        self.assertEqual(claimlint.lint("ember", ep), [])

    def test_run_is_idempotent_and_records_honour_as_of(self):
        self.write_episodes([episode("message"), episode("none", ts="2026-11-11T12:00:00")])
        before = datetime.now() - timedelta(seconds=1)
        self.assertEqual(claimlint.run("ember"), 1)
        path = claimlint._path("ember")
        original = path.read_bytes()
        self.assertEqual(claimlint.run("ember"), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(claimlint.records("ember")), 2)
        self.assertEqual(claimlint.records("ember", before), [])
        self.assertEqual(len(claimlint.records("ember", datetime.now())), 2)
        with path.open("a") as out:
            out.write('{"at":')
        self.assertEqual(len(claimlint.records("ember")), 2)
        self.write_episodes([episode("message", ts="2026-11-11T13:00:00")])
        self.assertEqual(claimlint.run("ember"), 1)
        self.assertEqual(len(claimlint.records("ember")), 3)

    def test_first_run_only_lints_last_twenty_then_advances(self):
        episodes = [episode("message", ts=f"2026-11-10T{i:02}:00:00") for i in range(23)]
        self.write_episodes(list(reversed(episodes)))
        self.assertEqual(claimlint.run("ember"), 20)
        self.assertEqual(claimlint.records("ember")[0]["episode_ts"], episodes[3]["ts"])
        self.write_episodes(episodes + [episode("message")])
        self.assertEqual(claimlint.run("ember"), 1)

    def test_head_lines_filters_since_and_last_day_newest_five_first(self):
        now = datetime.now()
        episodes = [episode("message", ts=(now - timedelta(hours=i)).isoformat()) for i in (30, 6, 5, 4, 3, 2, 1)]
        self.write_episodes(episodes)
        claimlint.run("ember")
        lines = claimlint.head_lines("ember")
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith(f"- {episodes[-1]['ts'][:16]}: said delivered via message;"))
        self.assertTrue(lines[-1].startswith(f"- {episodes[2]['ts'][:16]}:"))
        self.assertEqual(claimlint.head_lines("ember", since=episodes[-1]["ts"]), [])
        self.assertEqual(len(claimlint.head_lines("ember", since=episodes[-3]["ts"])), 2)
        self.assertEqual(claimlint.head_lines("missing"), [])

    def test_builtin_names_cover_template_specs_without_importing_agent_code(self):
        tree = ast.parse((core.PROJECT / "template" / "shaft" / "tools.py").read_text())
        builtins = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "BUILTINS" for target in node.targets))
        names = {node.args[0].value for node in ast.walk(builtins) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_spec"}
        self.assertTrue(names)
        self.assertEqual(names - claimlint.BUILTIN, set())
        self.assertTrue(set(commitments.BOOKKEEPING) | {"view_image", "commit_to"} <= claimlint.BUILTIN)


if __name__ == "__main__":
    unittest.main()
