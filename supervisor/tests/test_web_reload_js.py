"""#66: run the shipped reload control against browser APIs in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"

HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const mode = process.argv[1], source = fs.readFileSync(process.argv[2], 'utf8');
const saved = new Map([['other', 'keep']]), removed = [], updated = [];
let reloads = 0;
function page() {
  const handlers = {}, els = [], notices = [];
  const document = {
    readyState: 'loading',
    body: { appendChild() {}, classList: { contains: () => false } },
    addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
    querySelector: q => q === 'meta[name="static-version"]' ? { content: 'current' } : null,
    querySelectorAll: () => [],
    getElementById: id => els.find(e => e.id === id) || null,
    createElement() {
      const el = { dataset: {}, children: [], addEventListener() {}, setAttribute(k, v) { this[k] = v; },
        set textContent(v) { this.text = v; notices.push(v); }, get textContent() { return this.text; },
        appendChild(c) { this.children.push(c); } };
      els.push(el); return el;
    },
  };
  const context = {
    document, window: { addEventListener() {} },
    setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
    navigator: { serviceWorker: {
      register: async () => {},
      getRegistrations: async () => [0, 1].map(i => ({
        async update() { updated.push(i); if (mode === 'update' && i === 0) throw Error('refused'); },
        async unregister() { throw Error('unregistered: that drops the push subscription'); } })),
    } },
    caches: { keys: async () => ['shaft-old', 'other-cache', 'shaft-current'],
      async delete(k) { removed.push(k); if (mode === 'cache' && k === 'shaft-old') throw Error('refused'); return true; } },
    localStorage: new Proxy({}, { get() { throw Error('localStorage touched'); } }),
    sessionStorage: {
      getItem: k => saved.get(k) || null,
      setItem(k, v) { if (mode === 'storage') throw Error('blocked'); saved.set(k, v); },
      removeItem: k => saved.delete(k),
    },
    location: { reload() { reloads++; } },
  };
  if (mode === 'missing' || mode === 'storage') { context.navigator = {}; delete context.caches; }
  vm.createContext(context);
  vm.runInContext(source, context);
  return { handlers, els, context, notices, toast: () => document.getElementById('toast')?.textContent || '' };
}
(async () => {
  const p = page();
  let button = {};
  if (mode === 'update') {
    const fn = source.slice(source.indexOf('  function checkVersion('), source.indexOf('  function toBottom('));
    vm.runInContext(fn + '\ncheckVersion({headers: {get: () => "new"}});', p.context);
    button = p.els.find(e => e.id === 'updated').children[0];
    assert.ok('data-hard-reload' in button || 'hardReload' in button.dataset);
  }
  const target = { closest: q => q === '[data-hard-reload]' ? button : null };
  const pending = p.handlers.click.map(fn => fn({ target, preventDefault() {} }));
  assert.ok(p.notices.includes("Clearing the app's cache…"));
  await Promise.all(pending);
  assert.equal(reloads, 1);
  if (mode !== 'missing' && mode !== 'storage') {
    assert.deepEqual(updated, [0, 1]);
    assert.deepEqual(removed, ['shaft-old']);
  }
  assert.equal(saved.get('other'), 'keep');
  if (['update', 'cache', 'missing'].includes(mode)) {
    assert.equal(saved.size, 2);
    const next = page();
    for (const fn of next.handlers.DOMContentLoaded) fn();
    assert.match(next.toast(), mode === 'update' ? /service worker/i : mode === 'cache' ? /cache/i : /service worker.*cache/i);
    assert.equal(saved.size, 1);
    const again = page();
    for (const fn of again.handlers.DOMContentLoaded) fn();
    assert.equal(again.toast(), '');
  } else assert.equal(saved.size, 1);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "node is not installed")
class HardReloadJsTest(unittest.TestCase):
    def run_mode(self, mode):
        result = subprocess.run([NODE, "-e", HARNESS, mode, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_b7_reload_preserves_the_active_offline_cache(self):
        """B7: unchanged sw.js must retain its installed cache and /offline after Reload / update."""
        self.run_mode("success")

    def test_issue_66_reload_clears_only_app_caches(self):
        """Reverting #66 leaves registrations and SHAFT caches in place instead of reloading once."""
        self.run_mode("success")

    def test_issue_66_update_failure_survives_reload(self):
        """Reverting #66 loses the failure notice and skips cache cleanup after a worker update fails. The worker is
        never unregistered: that would drop this device's push subscription (the stub throws if it is)."""
        self.run_mode("update")

    def test_issue_66_cache_failure_does_not_skip_other_caches(self):
        """Reverting #66 leaves later caches untouched and loses the cache failure notice."""
        self.run_mode("cache")

    def test_issue_66_missing_apis_still_reload(self):
        """Reverting #66 prevents reload and feedback when browser cleanup APIs are missing."""
        self.run_mode("missing")

    def test_issue_66_blocked_storage_still_reloads(self):
        """Reverting #66 prevents reload when storing the cleanup failure is refused."""
        self.run_mode("storage")

    def test_issue_66_update_bar_uses_full_reload(self):
        """Reverting #66 makes the update bar reload without clearing the app caches."""
        self.run_mode("update")
