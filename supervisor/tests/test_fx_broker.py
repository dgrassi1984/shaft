"""E7 / row 18 and C8 / row 24: retry searches and retain tool output."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, gate, pyenv, sandbox

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft.tools import MAX_OUTPUT_CHARS, MAX_OUTPUT_FILES, MAX_RESULT_CHARS, Toolbox


class SearchRetryTest(unittest.TestCase):
    def setUp(self):
        self.b = broker.Broker.__new__(broker.Broker)
        self.b.name = "test"
        self.b.secrets = {"SCRAPEBADGER_API_KEY": "test-key"}
        self.b._log = mock.Mock()
        self.sleep = self.enterContext(mock.patch.object(broker.time, "sleep"))

    def response(self):
        return io.StringIO(json.dumps({"organic_results": [{"title": "Found", "link": "https://example.com",
                                                           "snippet": "Answer"}]}))

    def test_e7_transient_errors_retry_and_log(self):
        for error in [urllib.error.HTTPError("https://example.com", code, "busy", {}, None)
                      for code in (502, 503, 504)] + [TimeoutError("read timed out"),
                                                    urllib.error.URLError(TimeoutError("connect timed out"))]:
            with self.subTest(error=error), mock.patch.object(broker.urllib.request, "urlopen",
                    side_effect=[error, error, self.response()]) as opened:
                self.sleep.reset_mock()
                self.b._log.reset_mock()
                self.assertEqual(self.b._web_search("query"), "1. Found\n   https://example.com\n   Answer")
                self.assertEqual(opened.call_count, 3)
                self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(5)])
                self.assertIn("retry 2/2 in 5s", self.b._log.call_args.args[0])
                self.assertLess(sum(c.kwargs["timeout"] for c in opened.call_args_list) + 7, 120)   # the instance's call timeout

    def test_e7_exhaustion_raises_last_error(self):
        error = TimeoutError("read timed out")
        with mock.patch.object(broker.urllib.request, "urlopen", side_effect=error) as opened:
            with self.assertRaises(TimeoutError):
                self.b._web_search("query")
        self.assertEqual(opened.call_count, 3)

    def test_e7_read_timeout_retries_the_response_body(self):
        stalled = mock.MagicMock()
        stalled.__enter__.return_value.read.side_effect = TimeoutError("body timed out")
        with mock.patch.object(broker.urllib.request, "urlopen", side_effect=[stalled, self.response()]) as opened:
            self.assertIn("Found", self.b._web_search("query"))
        self.assertEqual(opened.call_count, 2)
        self.sleep.assert_called_once_with(2)

    def test_e7_nontransient_errors_are_immediate(self):
        for error in [urllib.error.HTTPError("https://example.com", code, "refused", {}, None)
                      for code in (400, 401, 429, 500)] + [urllib.error.URLError("DNS failure")]:
            with self.subTest(error=error), mock.patch.object(broker.urllib.request, "urlopen",
                                                            side_effect=error) as opened:
                with self.assertRaises(urllib.error.URLError):
                    self.b._web_search("query")
                self.assertEqual(opened.call_count, 1)
        self.sleep.assert_not_called()


class OutputTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.box = Toolbox(self.root)

    def save(self, text):
        with mock.patch.object(self.box, "_dispatch", return_value=text):
            shown = self.box.run("example", "{}")
        self.assertLessEqual(len(shown), MAX_RESULT_CHARS)
        self.assertEqual(self.box.log[-1]["result"], shown)
        rel = re.search(r"saved to ([^ ]+)\.", shown).group(1)
        return shown, rel

    def test_c8_full_remote_result_can_be_read_without_rerunning(self):
        text = "first\n" + "report α\n" * 7000 + "last"
        shown, rel = self.save(text)
        self.assertEqual((self.root / rel).read_text(), text)
        offset = int(re.search(r"offset=(\d+)", shown).group(1))
        self.assertEqual(shown[:offset], text[:offset])
        self.assertIn(text[offset:offset + 100], self.box.run("read_file", json.dumps({"path": rel, "offset": offset})))
        self.assertTrue(self.box.run("read_file", json.dumps({"path": rel, "offset": -4})).endswith("last"))

    def test_c8_storage_cap_cleanup_and_short_results(self):
        shown, rel = self.save("x" * (MAX_OUTPUT_CHARS + 1))
        self.assertIn("storage cap", shown)
        self.assertEqual(len((self.root / rel).read_text()), MAX_OUTPUT_CHARS)
        os.utime(self.root / rel, (1, 1))
        for _ in range(MAX_OUTPUT_FILES + 1):
            self.save("x" * 6001)
        self.assertFalse((self.root / rel).exists())
        self.assertEqual(len(list((self.root / ".tmp/outputs").glob("*.txt"))), MAX_OUTPUT_FILES)
        with mock.patch.object(self.box, "_dispatch", return_value="short"):
            self.assertEqual(self.box.run("example", "{}"), "short")

    def test_c8_save_failure_is_visible(self):
        (self.root / ".tmp").write_text("blocked")
        with mock.patch.object(self.box, "_dispatch", return_value="x" * 6001):
            self.assertIn("could not save output", self.box.run("example", "{}"))

    def test_c8_save_survives_a_lone_surrogate_and_any_save_error(self):
        # Grok review: the save caught only OSError, outside run()'s own catch; a JSON-decoded broker result with
        # a lone surrogate raised UnicodeEncodeError (a ValueError) there and killed the cycle.
        text = "\ud800" + "x" * 6001
        shown, rel = self.save(text)
        self.assertEqual((self.root / rel).read_text(), "?" + "x" * 6001)
        with mock.patch.object(self.box, "_dispatch", return_value="x" * 6001), \
                mock.patch("tempfile.NamedTemporaryFile", side_effect=ValueError("boom")):
            shown = self.box.run("example", "{}")
        self.assertIn("could not save output: ValueError: boom", shown)
        self.assertEqual(self.box.log[-1]["result"], shown)


class RunOutputTest(unittest.TestCase):
    """C8 / row 24, saved by the broker: an instance whose own shaft/tools.py predates the template's saving (it
    cuts every result at 6,000 characters) keeps the tail and stderr of a long `run` (Grok review)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.b = broker.Broker.__new__(broker.Broker)
        self.b.root, self.b.env_root, self.b.name, self.b.counts = self.root, self.root, "test", {}
        self.b.shared, self.b.run_confined_to, self.b.egress = None, None, mock.Mock(port=0)
        self.b.sandbox_env, self.b.mail_env, self.b.calendar_env, self.b.gdrive_env = lambda: {}, lambda: "", lambda: "", lambda: ""
        self.b._log = mock.Mock()

    def run_with(self, stdout, stderr):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = stdout, stderr
        with mock.patch.object(sandbox, "run_profile", return_value=""), \
                mock.patch.object(pyenv, "path_dirs", return_value=[]), \
                mock.patch.object(pyenv, "uv_env", return_value={}), \
                mock.patch.object(broker.subprocess, "Popen", return_value=proc), \
                mock.patch.object(gate, "stop_group"):
            return self.b._run("example")

    def saved(self):
        return sorted((self.root / ".tmp/outputs").glob("run-*.txt"))

    def test_c8_long_run_is_saved_and_its_cuts_name_their_offsets(self):
        stdout = "".join(f"out {i}\n" for i in range(2000))
        stderr = "".join(f"err {i}\n" for i in range(2000))
        result = self.run_with(stdout, stderr)
        self.assertLessEqual(len(result), broker.RUN_SHOWN_CHARS)
        self.assertLess(len(result), MAX_RESULT_CHARS)          # the old template's cut leaves it whole
        self.assertTrue(result.startswith("exit 0 · "))          # what runner and remember's web: check read
        [path] = self.saved()
        rel = path.relative_to(self.root).as_posix()
        full = path.read_text()
        self.assertIn(f"saved to {rel}", result.splitlines()[1])
        self.assertIn("\n--- stdout ---\n" + stdout + "\n--- stderr ---\n" + stderr, full)
        self.assertTrue(full.endswith(stderr))
        for stream, body in (("stdout", stdout), ("stderr", stderr)):
            offset = int(re.search(rf"{stream} was [\d,]+ characters: [\d,]+ cut here; "
                                   rf"read_file\('{re.escape(rel)}', offset=(\d+)\)", result).group(1))
            head = result.split(f"--- {stream} ---\n")[1].split("\n[... ")[0]
            self.assertTrue(body.startswith(head) and head)
            self.assertEqual(full[offset - len(head):offset], head)   # the offset reads on where the head stops
            self.assertTrue(result.split(f"--- {stream} ---\n")[1].split(" ...]\n")[1].split("\n--- ")[0]
                            .endswith(body[-50:]))                      # its tail is shown
        box = Toolbox(self.root)
        with mock.patch.object(box, "_dispatch", return_value=result):
            self.assertEqual(box.run("run", "{}"), result)           # no second save by the template
        self.assertFalse(list((self.root / ".tmp/outputs").glob("output-*.txt")))

    def test_c8_short_run_is_returned_whole_and_not_saved(self):
        result = self.run_with("hello\n", "warn\n")
        self.assertEqual(result.split("\n", 1)[1], "--- stdout ---\nhello\n\n--- stderr ---\nwarn\n")
        self.assertFalse(self.saved())

    def test_c8_short_stream_shown_whole_beside_a_long_one(self):
        result = self.run_with("x" * 20000, "the error\n")
        self.assertTrue(result.endswith("--- stderr ---\nthe error\n"))
        self.assertGreater(result.count("x"), 3000)               # the long stream gets the rest of the room

    def test_c8_storage_cap(self):
        result = self.run_with("x" * (broker.RUN_SAVED_CHARS + 10), "e" * 9000)
        [path] = self.saved()
        self.assertEqual(len(path.read_text()), broker.RUN_SAVED_CHARS)
        self.assertIn("(storage cap)", result)
        self.assertIn("stderr was 9,000 characters", result)
        self.assertIn("past the storage cap, not saved", result)

    def test_c8_save_failure_still_shows_head_and_tail(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".tmp").mkdir()
        (self.root / ".tmp/outputs").symlink_to(outside)              # never written through a link
        result = self.run_with("a" * 9000 + "TAIL", "")
        self.assertIn("could not save them (UnsafePath", result)
        self.assertIn("> .tmp/out.txt 2>&1", result)
        self.assertTrue(result.endswith("TAIL"))
        self.assertLessEqual(len(result), broker.RUN_SHOWN_CHARS)
        self.assertFalse(list(outside.iterdir()))
        self.b._log.assert_called_once()

    def test_c8_old_and_surplus_outputs_are_pruned(self):
        folder = self.root / ".tmp/outputs"
        folder.mkdir(parents=True)
        stale = folder / "run-20000101-000000-abcdef.txt"
        recent = [folder / f"run-{(datetime.now() - timedelta(minutes=i)):%Y%m%d-%H%M%S}-{i:06x}.txt"
                  for i in range(1, broker.RUN_SAVED_FILES + 2)]
        other = folder / "output-x.txt"
        for f in [stale, other, *recent]:
            f.write_text("old")
        self.run_with("x" * 9000, "")
        names = {p.name for p in self.saved()}
        self.assertEqual(len(names), broker.RUN_SAVED_FILES)
        self.assertNotIn(stale.name, names)
        self.assertNotIn(recent[-1].name, names)                     # the oldest recent ones go first
        self.assertIn(recent[0].name, names)
        self.assertTrue(other.exists())                              # the template's files are its own to prune
