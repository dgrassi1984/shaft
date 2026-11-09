"""JSONL record boundaries must follow LF, even when JSON strings contain Unicode separators."""
import ast
import json
import tempfile
import unittest
from pathlib import Path

from supervisor import broker, core, delete, gate_suite, trial


class JsonlLinesTests(unittest.TestCase):
    text = "page\u2028paragraph\u2029next\x85record\x1eend"

    def test_unicode_separators_stay_in_one_record(self):
        record = {"text": self.text}
        line = json.dumps(record, ensure_ascii=False)
        self.assertEqual(core.jsonl_lines(line + "\n"), [line])
        self.assertEqual(json.loads(core.jsonl_lines(line + "\n")[0]), record)

    def test_line_boundaries(self):
        self.assertEqual(core.jsonl_lines(""), [])
        self.assertEqual(core.jsonl_lines('one\r\n\n two\n'), ["one", "", " two"])
        self.assertEqual(core.jsonl_lines("one\ntwo"), ["one", "two"])
        self.assertEqual(core.jsonl_lines("", keepends=True), [])
        self.assertEqual(core.jsonl_lines("one\r\n\ntwo", keepends=True), ["one\r\n", "\n", "two"])

    def test_jsonl_rewrite_preserves_other_records_and_endings(self):
        line = json.dumps({"text": self.text}, ensure_ascii=False)
        text = line + '\r\n{"remove": true}\n' + line
        kept, removed = delete._jsonl(lambda row: row.get("remove"))(text)
        self.assertEqual(kept, line + "\r\n" + line)
        self.assertEqual(removed, ['{"remove": true}\n'])

    def test_shared_tail_splits_jsonl_but_keeps_plain_log_behavior(self):
        line = json.dumps({"text": self.text}, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(line + "\n")
            self.assertEqual(broker._tail_lines(path, 1), [line])
            path = Path(tmp) / "supervisor.log"
            path.write_text("first\u2028second\n")
            self.assertEqual(broker._tail_lines(path, 1), ["second"])

    def test_gate_json_per_line_reports_keep_unicode(self):
        report = {"part": "shared", "tests_run": 1, "ok": True, "nonce": self.text}
        self.assertEqual(gate_suite.report(gate_suite.REPORT + json.dumps(report, ensure_ascii=False)), report)
        timing = {"text": self.text}
        self.assertEqual(gate_suite.times(gate_suite.TIMES + json.dumps(timing, ensure_ascii=False)), timing)

    def test_replay_keeps_unicode_episode(self):
        """bake-off replay lost its episode to str.splitlines on U+2028, 2026-11-09."""
        episode = {"ts": "2026-11-08T12:00:00", "kind": "live",
                   "intent": {"goal": self.text}, "text": self.text}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "memory/episodes/2026-11-08.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(episode, ensure_ascii=False) + "\n")
            self.assertEqual(trial._new_episodes(root, set()), [episode])
            self.assertEqual(trial.live_episodes(root), [episode])
            self.assertEqual(trial._new_episodes(root, trial._episode_lines(root)), [])

    def test_jsonl_helper_has_no_supervisor_imports(self):
        path = Path(__file__).resolve().parents[1] / "jsonl.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"{path.name}:{node.lineno}")
                self.assertNotEqual((node.module or "").split(".")[0], "supervisor")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "supervisor")

    def test_jsonl_leaves_do_not_add_core_imports(self):
        supervisor = Path(__file__).resolve().parents[1]
        violations = []
        for name in ("bonsai_lora", "egress", "ledger", "gate_suite"):
            for n, line in enumerate((supervisor / f"{name}.py").read_text().split("\n"), 1):
                # log_path's pre-existing lazy settings lookup is unrelated to JSONL parsing.
                if name == "egress" and line == "    from .core import SUP_DIR":
                    continue
                if (name == "gate_suite" and line.lstrip().startswith("from .")) or any(
                        form in line for form in ("from .core", "from . import core", "supervisor.core")):
                    violations.append(f"{name}.py:{n}")
        self.assertEqual(violations, [])

    def test_no_obvious_jsonl_splitlines(self):
        supervisor = Path(__file__).resolve().parents[1]
        violations = [
            f"{path.name}:{n}"
            for path in sorted(supervisor.glob("*.py"))
            for n, line in enumerate(path.read_text().split("\n"), 1)
            if "splitlines()" in line and ("json.loads" in line or ".jsonl" in line)
        ]
        self.assertEqual(violations, [])
