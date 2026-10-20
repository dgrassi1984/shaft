"""Issues #30 and #31: notice recovery and room for chat plus Documents."""
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from supervisor import web, web_pages
from supervisor.tests.test_web import WebTest

STATIC = Path(web.__file__).parent / "web_static"
NODE = shutil.which("node")


class RecoveryTest(WebTest):
    def test_issue_30_navless_pages_have_one_column_and_a_way_home(self):
        """Reverting #30 puts notices in the rail column and removes their way back."""
        pages = [web_pages.message_page("Not allowed", "No"), web_pages.pair_page("t", "phone"),
                 web_pages.unpaired_page(), web_pages.offline_page()]
        for page in pages:
            with self.subTest(page=page[:80]):
                self.assertIn('class="shell single"', page)
                self.assertIn('href="/"', page)
        self.assertIn('class="shell"', web_pages.page("App", web_pages.Html(""), web_pages.nav([])))
        self.assertIn('.shell.single { grid-template-columns: minmax(0, 1fr); }', (STATIC / "web.css").read_text())

    def test_issue_30_expired_get_and_post_offer_a_new_link(self):
        """Reverting #30 leaves both expired-link entry points without the paste form."""
        token = web.new_pairing("phone").split("t=")[1]
        with mock.patch.object(web.time, "time", return_value=web.time.time() + 601):
            for method in ("GET", "POST"):
                with self.subTest(method=method):
                    r = self.req(f"/pair?t={token}", method, body=f"t={token}".encode(),
                                 headers={"sec-fetch-site": "same-origin"})
                    self.assertEqual(r.status, 403)
                    self.assertIn(b"Link expired", r.body)
                    self.assertIn(b'<form method="post" action="/pair" class="pairform"><input name="link"', r.body)
                    self.assertIn(b'href="/"', r.body)

    def test_issue_30_revocation_marks_poll_and_post_but_not_other_refusals(self):
        """Reverting #30 makes a revoked cookie indistinguishable from ordinary 403 refusals."""
        cookie = self.paired()
        self.assertNotIn("X-Shaft-Pair", self.req("/unpair", "POST", cookie=cookie).headers)
        web.unpair(web.device_from_cookie(cookie)[0])
        for path, method in (("/fragment/rail", "GET"), ("/chat/wren", "POST")):
            r = self.req(path, method, cookie=cookie)
            self.assertEqual(r.status, 403)
            self.assertEqual(r.headers.get("X-Shaft-Pair"), "required")
        self.assertNotIn("X-Shaft-Pair", self.req(login=None).headers)

    def test_issue_31_documents_sheet_uses_available_chat_width(self):
        """Reverting #31 keeps two minimum-width columns in a narrower tablet chat area."""
        css = (STATIC / "web.css").read_text()
        self.assertIn("container: chat-area / inline-size", css)
        sheet = css.split("@container chat-area (max-width: 38.8rem) {")[-1]
        self.assertNotEqual(sheet, css)
        self.assertIn(".chatgrid { display: flex; flex-direction: column; }", sheet)
        self.assertIn(".chatgrid > aside.docs { display: none; }", sheet)
        self.assertRegex(sheet, r"\.chatgrid.showdocs > aside.docs \{ display: block; position: fixed;")
        self.assertIn("230 + (20 + 18 + .8 + 2 * 1.2) * 15 = 848px", css)


@unittest.skipUnless(NODE, "node is not installed")
class RecoveryJsTest(unittest.TestCase):
    def node(self, script, *args):
        r = subprocess.run([NODE, "-e", script, *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_issue_30_gateway_navigation_uses_offline_cache_only(self):
        """Reverting #30 shows raw proxy errors on navigation instead of the cached offline page."""
        self.node(r'''
const vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, offline = { offline: true };
let status = 200, reject = false;
vm.runInNewContext(process.argv[1], {
  URL, self: { location: { origin: "https://shaft.test" }, addEventListener(k, fn) { handlers[k] = fn; } },
  caches: { match: async () => offline },
  fetch: async () => { if (reject) throw Error("offline"); return { status }; },
});
(async () => {
  for (const code of [200, 401, 403, 500, 502, 503, 504]) {
    status = code;
    for (const mode of ["navigate", "cors"]) {
      let response;
      handlers.fetch({ request: { method: "GET", mode, url: "https://shaft.test/i/wren/chat" },
        respondWith(p) { response = p; } });
      const got = await response;
      if (mode !== "navigate") assert.equal(got, undefined);
      else if (code >= 502 && code <= 504) assert.equal(got, offline);
      else assert.equal(got.status, code);
    }
  }
  reject = true;
  let response;
  handlers.fetch({ request: { method: "GET", mode: "navigate", url: "https://shaft.test/" },
    respondWith(p) { response = p; } });
  assert.equal(await response, offline);
  handlers.fetch({ request: { method: "POST" }, respondWith() { throw Error("intercepted POST"); } });
})().catch(e => { console.error(e); process.exitCode = 1; });
''', web_pages.service_worker())

    def test_issue_30_revoked_post_and_poll_keep_chat_draft(self):
        """Reverting #30 leaves fetch callers on the revoked page; redirect must never clear the draft."""
        self.node(r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
(async () => {
 for (const status of [401, 403]) for (const kind of ["post", "diff", "poll"]) {
  const handlers = {}, storage = new Map([["shaft.draft.wren", "unsent message"]]);
  const box = { value: "unsent message" };
  const form = { dataset: { post: "/chat/wren", draft: "wren", reset: "1", ...(kind === "diff" ? { diff: "/preview/x" } : {}) },
    querySelector(q) { return q.startsWith("textarea") ? box : null; }, reset() { throw Error("cleared draft"); } };
  const document = { readyState: "loading", visibilityState: "visible", body: { scrollHeight: 0, appendChild() {} },
    createElement: () => ({ setAttribute() {}, classList: { add() {} } }),
    addEventListener(k, fn) { (handlers[k] ||= []).push(fn); }, querySelector: () => null,
    querySelectorAll: () => [], getElementById: () => null, dispatchEvent() {} };
  let destination = "", reads = 0;
  const context = { document, window: { addEventListener() {} }, navigator: { onLine: true },
    location: { assign(url) { destination = url; } }, AbortController, URLSearchParams, Event,
    DOMParser: class { parseFromString(text) { return { body: { textContent: text } }; } },
    FormData: class { *[Symbol.iterator]() {} }, setTimeout() {}, clearTimeout() {}, setInterval() {},
    localStorage: { getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k) },
    fetch: async () => ({ status, ok: false, headers: { get: k => k === "X-Shaft-Pair" ? "required" : "" },
      text: async () => { reads++; return "pair again"; } }) };
  vm.runInNewContext(source, context);
  if (kind === "poll") {
    // Same shipped poll function, with its dependencies in the original closure.
    const injected = source.replace('if (document.readyState === "loading")', 'window.testPoll = poll; if (document.readyState === "loading")');
    context.window.__shaftWeb = false;
    vm.runInNewContext(injected, context);
    const el = { dataset: { poll: "/fragment/rail" }, isConnected: true, getClientRects: () => [1],
      querySelector: () => null, contains: () => false, classList: { add() {}, remove() {} },
      previousElementSibling: { classList: { contains: () => true } } };
    context.window.getSelection = () => null;
    context.window.testPoll(el);
    await el._tick();
  } else {
    await handlers.submit[0]({ target: { closest: () => form }, preventDefault() {} });
  }
  assert.equal(destination, "/", `${kind} ${status}`);
  assert.equal(storage.get("shaft.draft.wren"), "unsent message");
  assert.equal(box.value, "unsent message");
  assert.equal(reads, 0);
 }
})().catch(e => { console.error(e); process.exitCode = 1; });
''', str(STATIC / "web.js"))
