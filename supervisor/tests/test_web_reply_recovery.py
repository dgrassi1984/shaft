"""Issues #9 and #11: failure recovery and truthful lifecycle wording, without live services."""
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import api, web_chat, web_views

from supervisor.tests.node_helpers import NODE_TIMEOUT_S


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shaft-recovery-")
        self.addCleanup(self.temp.cleanup)
        for patch in (mock.patch.object(web_chat, "SUP_DIR", Path(self.temp.name)),
                      mock.patch.object(web_chat, "_docs_now", return_value={}),
                      mock.patch.object(web_chat, "_sync_copies"),
                      mock.patch.object(web_chat, "_headers", return_value={}),
                      mock.patch.object(web_chat, "log"), mock.patch("supervisor.webpush.notify")):
            patch.start()
            self.addCleanup(patch.stop)

    def chat(self):
        chat = web_chat.new_chat("wren", "operator")
        chat.update(state="replying", reply={"interjections": 0, "turn_start": 1}, turns=[
            {"role": "user", "content": "original request\n\n" + web_chat.DOCS_NOTE,
             "shown": 'original <request> "please"', "ts": "2026-10-20T12:00:00"}])
        web_chat.save(chat)
        return chat

    def run_reply(self, *, exc=None, status=200, text="", tools=None, answer="", stopped=False, legacy=False, complete=True, interjected=False):
        chat = self.chat()
        if interjected:
            chat["reply"]["interjections"] = 1
            chat["turns"].append({"role": "user", "content": "also this", "interjected": True, "ts": ""})
            web_chat.save(chat)
        relay = web_chat.Relay(chat["id"])
        if stopped:
            web_chat._relays[chat["id"]] = relay
            web_chat.stop(chat["id"])
        events = []
        if answer:
            events.append(api._chunk("test", "wren", {"content": answer}))
        if text:
            if legacy:
                events.append(api._chunk("test", "wren", {"content": f"[error: {text}]"}))
            else:
                events.append(api._chunk("test", "wren", {}, reply_error={"message": text, "tools_ran": tools}))
        response = mock.MagicMock(status=status)
        response.__iter__.return_value = iter(events + ([b"data: [DONE]\n\n"] if complete else []))
        response.read.return_value = json.dumps({"error": {"message": text}}).encode()
        conn = mock.Mock()
        conn.request.side_effect = exc
        conn.getresponse.return_value = response
        with mock.patch.object(web_chat, "_api_conn", return_value=conn):
            web_chat._run(relay)
        return web_chat.load(chat["id"])

    def test_issue_9_failure_causes_and_safe_retry_through_relay(self):
        """Reverting #9 loses structured causes and offers no evidence-based recovery for saved failed replies."""
        cases = [({"exc": ConnectionRefusedError("refused")}, "connection", True),
                 ({"exc": TimeoutError("send timed out")}, "connection", False),
                 ({"exc": ConnectionResetError("reset")}, "connection", False),
                 ({"status": 503, "text": "unavailable"}, "service", False),
                 ({"exc": web_chat.Refused("API not set up")}, "service", False),
                 ({"text": "CloudError: HTTP 503 unavailable", "tools": False}, "model", True),
                 ({"text": "CloudError: HTTP 599 unavailable", "tools": False}, "model", True),
                 ({"text": "CloudError: HTTP 429 limit", "tools": False}, "model", False),
                 ({"text": "CloudError: HTTP 503 unavailable", "tools": True}, "model", False),
                 ({"text": "CloudError: HTTP 503 unavailable", "legacy": True}, "model", False),
                 ({"text": "mystery <failure>"}, "unknown", False),
                 ({"stopped": True, "answer": "half"}, "stopped", False)]
        for args, kind, retry in cases:
            with self.subTest(args=args):
                chat = self.run_reply(**args)
                turn = chat["turns"][-1]
                self.assertEqual((turn.get("note_kind"), turn.get("note_retry")), (kind, retry))
                self.assertEqual(chat["state"], "idle")
                rendered = str(web_views.chat_feed("wren", chat, []))
                # Every failure but Stop offers Retry: in one tap when safe, after a confirm when tools may have run.
                self.assertEqual("data-retry=" in rendered, kind != "stopped")
                self.assertEqual("data-retry-of=" in rendered, retry)
                self.assertEqual("data-retry-confirm=" in rendered, kind != "stopped" and not retry)
                if kind == "stopped":
                    self.assertIn("You stopped this reply.", rendered)
                else:
                    self.assertIn("Technical details", rendered)
                    self.assertIn("Retry sends it again" if retry else "Check Activity and Services", rendered)
                    if not retry:
                        self.assertIn('href="/i/wren/activity"', rendered)
                        self.assertIn('href="/services"', rendered)
                    self.assertEqual(turn["retry_text"], 'original <request> "please"')
                    self.assertIn('data-retry="original &lt;request&gt; &quot;please&quot;"', rendered)
                    self.assertNotIn("data-retry=", str(web_views.chat_feed("wren", chat, [], archived=True)))

    def test_issue_9_partial_answer_and_legacy_note(self):
        """Reverting #9 hides partial failure recovery and exposes raw legacy errors without a disclosure."""
        chat = self.run_reply(text="CloudError: HTTP 503 <bad>", tools=True, answer="Kept partial answer")
        rendered = str(web_views.chat_feed("wren", chat, []))
        self.assertIn("Kept partial answer", rendered)
        self.assertIn("incomplete</span>", rendered)
        self.assertIn("&lt;bad&gt;", rendered)
        self.assertNotIn("data-retry-of=", rendered)              # tools ran: never folded, Retry asks first
        self.assertIn("data-retry-confirm=", rendered)
        turn = chat["turns"][-1]
        turn.clear()
        turn.update(role="assistant", content="", ts="", note="error: HTTP 503 <bad>")
        rendered = str(web_views.chat_feed("wren", chat, []))
        self.assertIn("Technical details", rendered)
        self.assertNotIn("(no answer)", rendered)
        self.assertNotIn("data-retry-of=", rendered)

    def test_a_failure_that_may_have_run_tools_offers_a_confirmed_retry(self):
        """The operator asked for Retry on "Actions may already have run" (2026-10-29): it showed only Activity and
        Services, and the message had to be typed again. Retry asks first and sends a new message, unfolded."""
        chat = self.run_reply(exc=ConnectionResetError("reset"))
        rendered = str(web_views.chat_feed("wren", chat, []))
        self.assertIn("Actions may already have run", rendered)
        self.assertIn('data-retry="original &lt;request&gt; &quot;please&quot;" data-retry-confirm="Actions may '
                      'already have run. Send it again anyway?">Retry</button>', rendered)
        self.assertNotIn("data-retry-of=", rendered)
        self.assertFalse(web_chat.mark_retried(chat, len(chat["turns"]) - 1, 'original <request> "please"'))
        # a failure saved before retry_text was kept for it (thelos 10:39): Retry sends the message it answered
        turn = chat["turns"][-1]
        turn["retry_text"] = ""
        self.assertIn('data-retry="original &lt;request&gt; &quot;please&quot;" data-retry-confirm=',
                      str(web_views.chat_feed("wren", chat, [])))

    def test_issue_9_stream_cut_short_preserves_partial_without_retry(self):
        """Reverting #9 treats an EOF before [DONE] as a successful answer, hiding uncertain external actions."""
        chat = self.run_reply(answer="half", complete=False)
        turn = chat["turns"][-1]
        self.assertEqual(turn.get("note_kind"), "connection")
        self.assertFalse(turn.get("note_retry"))
        self.assertEqual(turn["content"], "half")
        self.assertIn("incomplete</span>", str(web_views.chat_feed("wren", chat, [])))

    def test_issue_9_interjections_do_not_retry_the_wrong_request(self):
        """Reverting #9 loses structured recovery; retrying the last interjection alone changes the request."""
        chat = self.run_reply(text="CloudError: HTTP 503", tools=False, interjected=True)
        turn = chat["turns"][-1]
        self.assertEqual(turn.get("note_kind"), "model")
        self.assertFalse(turn.get("note_retry"))
        self.assertEqual(turn["retry_text"], "")
        self.assertNotIn("data-retry=", str(web_views.chat_feed("wren", web_chat.load(chat["id"]), [])))

    def test_issue_9_restart_is_service_failure(self):
        """Reverting #9 leaves a restarted relay without a cause or an incomplete marker."""
        chat = self.chat()
        chat["reply"].update(answer="half", activity=[])
        web_chat.save(chat)
        web_chat.reap()
        chat = web_chat.load(chat["id"])
        self.assertEqual(chat["turns"][-1].get("note_kind"), "service")
        self.assertFalse(chat["turns"][-1].get("note_retry"))
        rendered = str(web_views.chat_feed("wren", chat, []))
        self.assertIn("incomplete</span>", rendered)
        self.assertIn("data-retry-confirm=", rendered)            # the restart may have cut it after tools ran

    def test_issue_11_end_requested_clear_retained_and_failures_preserved(self):
        """Reverting #11 claims journal completion as soon as End is accepted, before recovery finishes."""
        chat = self.chat()
        chat.update(state="idle", reply=None)
        web_chat.save(chat)
        with mock.patch.object(web_chat, "_post", return_value=(503, {"error": {"message": "unavailable"}})):
            with self.assertRaises(web_chat.Refused):
                web_chat.clear("wren", chat["id"])
        self.assertEqual(web_chat.shown("wren")["state"], "idle")
        with mock.patch.object(web_chat, "_post", return_value=(202, {"ended": "api-1"})):
            self.assertIn("journal write-up requested", web_chat.end(chat["id"]))
        ended = web_chat.shown("wren")
        rendered = str(web_views.chat_feed("wren", ended, []))
        self.assertIn("journal write-up requested", rendered)
        self.assertNotIn("written up in its journal", rendered)
        self.assertIn("original", rendered)
        web_chat.clear("wren", chat["id"])
        self.assertIsNone(web_chat.shown("wren"))
        self.assertEqual(web_chat.load(chat["id"])["turns"], chat["turns"])

    def test_issue_11_confirmations(self):
        """Reverting #11 hides Clear's full label and leaves End/Stop consequences unexplained."""
        chat = self.chat()
        header = str(web_views.chat_header("wren", {}, chat, []))
        # Clear is New chat (PLAN-pwa-market): an icon named for what it does; its confirm says what is kept
        self.assertIn('title="New chat" aria-label="New chat" data-focus="clear">', header)
        self.assertIn("Start a new chat? This one is kept in Earlier chats and Transcripts", header)
        self.assertIn("Only the reply stops. The conversation stays open.", str(web_views.chat_feed("wren", chat, [])))
        chat["state"] = "idle"
        rendered = str(web_views.chat_feed("wren", chat, []))
        self.assertIn("The messages stay on this page.", rendered)
        self.assertIn("Your next message starts a new conversation.", rendered)


class ApiFailureTest(unittest.TestCase):
    def test_issue_9_api_stream_preserves_failure_with_partial_content_and_tool_evidence(self):
        """Reverting #9 drops the error after partial content and gives the relay no reliable tool evidence."""
        for partial in ("", "half an answer"):
            for tools in (False, True):
                with self.subTest(partial=partial, tools=tools):
                    events = ([{"type": "tool_start", "name": "send_message"}] if tools else [])
                    events += ([{"type": "content", "text": partial}] if partial else [{"type": "turn_end"}])
                    events += [{"type": "error", "message": "CloudError: HTTP 503"}]
                    proc = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(e)+"\n" for e in events)))
                    handler = api.Handler.__new__(api.Handler)
                    handler.wfile = io.BytesIO()
                    handler.conversations = mock.Mock()
                    handler.conversations.talk_id.return_value = "api-test"
                    handler.live = mock.Mock()
                    handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
                    def run(*args, driver, **kwargs):
                        driver(proc)
                        return 0
                    with mock.patch.object(api, "run_agent", side_effect=run), \
                         mock.patch.object(api, "Reply"), mock.patch.object(api, "busy_with", return_value=None):
                        handler._chat("wren", [{"role": "user", "content": "hi"}], True, "test")
                    chunks = [json.loads(line[6:]) for line in handler.wfile.getvalue().splitlines() if line.startswith(b"data: {")]
                    self.assertEqual(chunks[-1].get("reply_error"), {"message": "CloudError: HTTP 503", "tools_ran": tools})
                    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
                    self.assertEqual(content, partial or "[error: CloudError: HTTP 503]")


@unittest.skipUnless(shutil.which("node"), "Node is required")
class RetryJsTest(unittest.TestCase):
    def test_issue_9_retry_sends_once_and_protects_draft(self):
        """Reverting #9 loses the Retry handler; replacing drafts without consent discards the operator's work.
        PLAN-pwa-market (iPhone review I14): Retry sends in one tap — it is offered only when the failed request ran no
        tools (classify_reply), and a repeated tap carries the same message id (no new input), so it is not sent twice."""
        src = (Path(__file__).resolve().parents[1] / "web_static" / "web.js").read_text()
        handlers = re.findall(r'  document.addEventListener\("click", \(ev\) => \{\n.*?^  \}\);', src, re.S | re.M)
        handler = next((h for h in handlers if '[data-retry]' in h), "")
        script = r'''
let listener = () => {}, inputs = 0, prompts = 0, allow = false, sends = 0, chips = 0;
const retryOf = { value: "" };
const form = { querySelector: q => q === "input[name=retry_of]" ? retryOf : null };
const quoteChip = () => { chips += 1; };
const box = { value: "draft", focus() {}, setSelectionRange() {}, dispatchEvent() { inputs++; }, closest: () => form };
const document = { addEventListener: (_, f) => listener = f, querySelector: () => box };
const confirm = () => { prompts++; return allow; };
const sendComposer = (f, queued) => { if (f !== form || queued) throw new Error("wrong send"); sends++; };
//HANDLER//
const button = { dataset: { retry: "original request", retryOf: "3" } };
const event = { target: { closest: () => button } };
listener(event);
const kept = box.value, sentFirst = sends;
allow = true;
listener(event);
listener(event);
// Codex review: a quote waiting in the composer went out with the failed message, unasked
box.value = "original request"; form._quote = "> another message"; allow = false;
listener(event);
const quoteKept = form._quote, sendsAfterRefusal = sends;
allow = true;
listener(event);
console.log(JSON.stringify({ kept, sentFirst, value: box.value, inputs, prompts, sends, disabled: !!button.disabled,
                             quoteKept, sendsAfterRefusal, quote: form._quote, chips, retryOf: retryOf.value }));
'''
        result = subprocess.run([shutil.which("node"), "-e", script.replace("//HANDLER//", handler)],
                                capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
        # not disabled: a resend that failed can be retried (Codex review: it stayed off for good)
        self.assertEqual(json.loads(result.stdout), {"kept": "draft", "sentFirst": 0, "value": "original request",
                                                     "inputs": 1, "prompts": 4, "sends": 3, "disabled": False,
                                                     "quoteKept": "> another message", "sendsAfterRefusal": 2,
                                                     "quote": "", "chips": 1, "retryOf": "3"})

    def test_a_retry_that_may_rerun_actions_asks_first_and_sends_unfolded(self):
        """Retry on "Actions may already have run" (2026-10-29): declined, nothing is sent and the draft is kept;
        accepted, the message goes as a new one (no retry_of), so the failed try is not folded away."""
        src = (Path(__file__).resolve().parents[1] / "web_static" / "web.js").read_text()
        handlers = re.findall(r'  document.addEventListener\("click", \(ev\) => \{\n.*?^  \}\);', src, re.S | re.M)
        handler = next((h for h in handlers if '[data-retry]' in h), "")
        script = r'''
let listener = () => {}, asked = [], allow = false, sends = 0;
const retryOf = { value: "stale" };
const form = { querySelector: q => q === "input[name=retry_of]" ? retryOf : null };
const quoteChip = () => {};
const box = { value: "", dispatchEvent() {}, closest: () => form };
const document = { addEventListener: (_, f) => listener = f, querySelector: () => box };
const confirm = (m) => { asked.push(m); return allow; };
const sendComposer = () => { sends++; };
//HANDLER//
const button = { dataset: { retry: "what it did", retryConfirm: "Actions may already have run. Send it again anyway?" } };
const event = { target: { closest: () => button } };
listener(event);
const declined = { sends, value: box.value };
allow = true;
listener(event);
console.log(JSON.stringify({ declined, asked, sends, value: box.value, retryOf: retryOf.value }));
'''
        result = subprocess.run([shutil.which("node"), "-e", script.replace("//HANDLER//", handler)],
                                capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "declined": {"sends": 0, "value": ""}, "sends": 1, "value": "what it did", "retryOf": "",
            "asked": ["Actions may already have run. Send it again anyway?"] * 2})
