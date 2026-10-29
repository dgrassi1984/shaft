"""Release notes from the supervisor's tags, and the paired web fragment."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, version, web, web_pages
from supervisor.tests.test_web import WebTest

from supervisor.tests.node_helpers import NODE_TIMEOUT_S


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-changelog-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "test@example.com")
        for n in range(1, 4):
            self.git("commit", "--allow-empty", "-qm", f"release {n}")
            self.tag(n)
        for patch in (mock.patch.object(core, "PROJECT", self.repo),
                      mock.patch.object(version, "shown_commit", side_effect=lambda: self.git("rev-parse", "HEAD")),
                      mock.patch.object(version, "service", return_value={"describe": "v1.0.0", "commit": "unused"}),
                      mock.patch.object(version, "_changelog", {"at": -1e9, "head": "", "by": None, "data": None})):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args, env=None):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              text=True, env={**os.environ, **(env or {})}).stdout.strip()

    def tag(self, n):
        self.git("tag", "-a", f"v{n}.0.0", "-m", f"v{n}.0.0\n\nRelease {n}.\n\n- A change",
                 env={"GIT_COMMITTER_DATE": f"2026-01-{n:02d}T12:00:00+0000"})

    def test_order_dates_body_and_running_release(self):
        data = version.changelog()
        self.assertEqual([r["tag"] for r in data["releases"]], ["v3.0.0", "v2.0.0", "v1.0.0"])
        self.assertEqual([r["date"] for r in data["releases"]], ["2026-01-03", "2026-01-02", "2026-01-01"])
        self.assertEqual(data["releases"][0]["body"], "Release 3.\n\n- A change")
        self.assertEqual([r["tag"] for r in data["releases"] if r["running"]], ["v1.0.0"])
        self.assertEqual(data["since"], [])
        with mock.patch.object(version, "service", return_value={"describe": "v2.0.0-3-gabc1234+changes"}):
            self.assertEqual([r["tag"] for r in version.changelog()["releases"] if r["running"]], ["v2.0.0"])
        with mock.patch.object(version, "service", return_value={"commit": self.git("rev-parse", "v2.0.0^{}")}), \
                mock.patch.object(version, "commit_describe", wraps=version.commit_describe):
            self.assertEqual([r["tag"] for r in version.changelog()["releases"] if r["running"]], ["v2.0.0"])

    def test_release_cap(self):
        for n in range(4, 24):
            self.tag(n)
        releases = version.changelog()["releases"]
        self.assertEqual(len(releases), 20)
        self.assertEqual((releases[0]["tag"], releases[-1]["tag"]), ("v23.0.0", "v4.0.0"))

    def test_since_release_and_cap(self):
        branch = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "topic")
        self.git("commit", "--allow-empty", "-qm", "Topic detail excluded from first-parent notes")
        self.git("checkout", "-q", branch)
        self.git("merge", "--no-ff", "topic", "-m", "Merge pull request #42 from team/changelog")
        self.git("commit", "--allow-empty", "-qm", "A direct fix")
        self.assertEqual(version.changelog()["since"], ["A direct fix", "#42 team/changelog"])
        for n in range(16):
            self.git("commit", "--allow-empty", "-qm", f"fix {n}")
        self.assertEqual(version.changelog()["since"], [f"fix {n}" for n in range(15, 0, -1)])
        self.git("checkout", "-q", "v3.0.0")
        self.assertEqual(version.changelog()["since"], [])

    def test_cache_expires_for_new_tags_at_same_head(self):
        with mock.patch.object(version.time, "monotonic", return_value=100), \
                mock.patch.object(version, "git", wraps=version.git) as read:
            version.changelog()
            calls = read.call_count
            version.changelog()
            self.assertEqual(read.call_count, calls)
            self.tag(4)
            self.assertEqual(version.changelog()["releases"][0]["tag"], "v3.0.0")
        with mock.patch.object(version.time, "monotonic", return_value=106):
            self.assertEqual(version.changelog()["releases"][0]["tag"], "v4.0.0")


class ChangelogWebTest(WebTest):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(web, "_names", return_value=[]),
                      mock.patch.object(core, "INSTANCES", self.sup / "empty"),
                      mock.patch.object(core, "SUP_DIR", self.sup)):
            patch.start()
            self.addCleanup(patch.stop)
        self.data = {"releases": [{"tag": "v1.0.0", "date": "2026-01-01", "running": True,
                                  "body": "<script>alert(1)</script>\n\n- Safe bullet"}], "since": []}

    def test_fragment_escapes_notes_and_is_not_cached(self):
        with mock.patch.object(version, "changelog", return_value=self.data):
            r = self.req("/fragment/changelog", cookie=self.paired())
        self.assertEqual(r.status, 200)
        body = r.body.decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("<li>Safe bullet</li>", body)
        self.assertIn("(running web)", body)
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_fragment_requires_login_and_pairing_like_rail(self):
        with mock.patch.object(version, "changelog", return_value=self.data) as read:
            for path in ("/fragment/rail", "/fragment/changelog"):
                self.assertEqual(self.req(path, login=None).status, 403)
                r = self.req(path)
                self.assertEqual(r.status, 403)
                self.assertEqual(r.headers["X-Shaft-Pair"], "required")
            read.assert_not_called()
            self.assertEqual(self.req("/fragment/changelog", cookie=self.paired()).status, 200)

    def test_nav_version_button_preserves_reload(self):
        with mock.patch.object(version, "service", return_value={"commit": "abc1234", "describe": "v1.0.0"}):
            body = web_pages.nav([])
        self.assertIn('type="button" data-changelog title="What&#39;s new" aria-haspopup="dialog">v1.0.0</button>', body)
        self.assertIn('data-hard-reload title="Clear the app cache and load the current version">Reload / update</button>', body)


class ChangelogClientTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_shipped_dialog_load_close_backdrop_and_retry(self):
        source = Path(web_pages.__file__).parent / "web_static" / "web.js"
        harness = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const handlers = {}, dialogs = [], requests = [];
let focused = 0, fail = false;
const trigger = { focus() { focused++; } };
const document = {
  readyState: 'loading', addEventListener(k, f) { (handlers[k] ||= []).push(f); },
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  body: { appendChild() {}, classList: { contains: () => false } },
  createElement(tag) {
    assert.equal(tag, 'dialog');
    const events = {}, body = {}, closeButton = {};
    closeButton.addEventListener = (k, f) => { closeButton[k] = f; };
    const d = { body, closeButton, events, setAttribute() {},
      querySelector: q => q === '.changelog-body' ? body : closeButton,
      addEventListener(k, f) { events[k] = f; },
      showModal() { this.open = true; },
      close() { this.open = false; events.close(); },
      remove() { this.removed = true; },
      getBoundingClientRect: () => ({ left: 10, right: 300, top: 10, bottom: 500 }) };
    dialogs.push(d); return d;
  }
};
const context = { document, window: { addEventListener() {} },
  setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
  fetch: async (url, options) => {
    requests.push({ url, options });
    if (fail) throw Error('offline');
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '<p>release notes</p>' };
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const open = () => Promise.all(handlers.click.map(f => f({ target: {
  closest: q => q === '[data-changelog]' ? trigger : null
} })));
(async () => {
  await open();
  const first = dialogs[0];
  assert.equal(first.open, true);
  assert.equal(first.body.innerHTML, '<p>release notes</p>');
  assert.equal(requests[0].url, '/fragment/changelog');
  assert.equal(requests[0].options.cache, 'no-store');
  first.events.click({ target: first, clientX: 50, clientY: 50 });
  assert.equal(first.open, true);
  let stopped = false;
  first.events.keydown({ key: 'Escape', stopPropagation() { stopped = true; } });
  assert.equal(stopped, true);
  first.closeButton.click();
  assert.equal(first.removed, true);
  assert.equal(focused, 1);
  await open();
  assert.equal(requests.length, 2);
  dialogs[1].events.click({ target: dialogs[1], clientX: 0, clientY: 0 });
  assert.equal(dialogs[1].open, false);
  fail = true;
  await open();
  assert.equal(dialogs[2].body.textContent, 'Could not load release notes. Close and try again.');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", harness, str(source)], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
