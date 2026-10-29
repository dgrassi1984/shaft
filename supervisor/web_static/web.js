// SHAFT web UI behaviour (docs/plans/PLAN-webui.md). No inline script anywhere (the CSP forbids it): pages declare what they
// need with data- attributes, and this file binds it once.
//   data-poll="/fragment/…"  the element's content is refreshed from that URL every 2 s while the tab is visible
//   <form data-post="/…">    sent with fetch as a POST carrying the CSRF token; data-confirm asks first (data-confirm-type:
//                            the operator types that word instead of clicking OK), data-after="home" goes to / after; data-reset
//                            clears it once sent, data-diff="/preview/…" shows the diff with Apply and Back (nothing
//                            is saved until Apply); data-timeout is seconds to wait, from the server's own limit;
//                            data-check names the page a lost answer tells you to look at. The composer (its
//                            input[name=mid]) can be sent again; data-guard asks before leaving with an unsaved edit,
//                            data-keep keeps its text in this browser until it is saved. The reply is a toast and
//                            the polled parts refresh
//   data-row                 a saved form replaces only its nearest row with the server's new value and token
// Listeners are delegated from the document, so content swapped in by a poll needs no re-binding.
(() => {
  "use strict";
  if (window.__shaftWeb) return;          // loaded once per page, whatever happens
  window.__shaftWeb = true;

  const csrf = () => (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function pairAgain(r) {
    if ((r.status !== 401 && r.status !== 403) || r.headers.get("X-Shaft-Pair") !== "required") return false;
    location.assign("/");                         // the pairing form; local drafts stay until a successful send
    return true;
  }

  function fullText(html) {
    return new DOMParser().parseFromString(html, "text/html").body.textContent.trim();
  }

  function textOf(html) {                    // for a toast or a note: the first 300 characters
    return fullText(html).slice(0, 300);
  }

  // Live regions (docs/plans/PLAN-pwa P5) exist, empty, from the start: a region created already filled is not read out.
  function region(id, role) {
    let box = document.getElementById(id);
    if (!box) {
      box = document.createElement("div");
      box.id = id;
      box.setAttribute("role", role);
      box.setAttribute("aria-live", role === "alert" ? "assertive" : "polite");
      document.body.appendChild(box);
    }
    return box;
  }
  function announce(text) {                       // for VoiceOver only (visually hidden): a reply that arrived
    const box = region("announce", "status");
    box.textContent = "";
    clearTimeout(box._t);                         // two at once: the newer one is read, not whichever timer ran last
    box._t = setTimeout(() => { box.textContent = text; }, 50);
  }

  function toast(text, bad) {
    const box = region("toast", "status");
    box.setAttribute("role", bad ? "alert" : "status");
    box.setAttribute("aria-live", bad ? "assertive" : "polite");
    box.textContent = text;
    box.className = bad ? "bad show" : "show";
    clearTimeout(box._t);
    box._t = setTimeout(() => { box.className = ""; }, bad ? 10000 : 5000);
  }

  document.addEventListener("click", async (ev) => {
    const button = ev.target.closest("[data-changelog]");
    if (!button) return;
    const dialog = document.createElement("dialog");
    dialog.className = "changelog";
    dialog.setAttribute("aria-labelledby", "changelog-title");
    dialog.innerHTML = '<header><h1 id="changelog-title">What\'s new</h1>' +
      '<button type="button" data-close-changelog autofocus>Close</button></header>' +
      '<div class="changelog-body" aria-live="polite"><p>Loading…</p></div>';
    const body = dialog.querySelector(".changelog-body");
    dialog.querySelector("[data-close-changelog]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (e) => {
      if (e.target !== dialog) return;
      const r = dialog.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dialog.close();
    });
    dialog.addEventListener("keydown", (e) => e.stopPropagation());   // the rail's sheet must not trap Tab or Escape
    dialog.addEventListener("close", () => { dialog.remove(); syncSheets(); button.focus(); });
    document.body.appendChild(dialog);
    dialog.showModal();
    try {
      const r = await fetch("/fragment/changelog", { cache: "no-store" });
      if (pairAgain(r)) return;
      if (!r.ok) throw new Error("changelog unavailable");
      body.innerHTML = await r.text();
    } catch (_) {
      body.textContent = "Could not load release notes. Close and try again.";
    }
  });

  let reloading = false;
  const reloadFailure = "shaft.reload.failure";
  async function hardReload() {
    if (reloading) return;
    reloading = true;
    toast("Clearing the app's cache…");
    const failed = new Set();
    // The worker is updated, not unregistered: unregistering it would drop this device's push subscription
    // (notifications), which belongs to the registration. Pages come from the network anyway (sw.js).
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      for (const reg of registrations) {
        try { await reg.update(); } catch (e) { failed.add("service worker update"); }
      }
    } catch (e) { failed.add("service worker update"); }
    try {
      const keys = await caches.keys();
      // An unchanged worker does not reinstall: its shell (especially /offline) must survive the reload.
      const active = "shaft-" + (document.querySelector('meta[name="static-version"]') || {}).content;
      for (const key of keys.filter(k => k.startsWith("shaft-") && k !== active)) {
        try { await caches.delete(key); } catch (e) { failed.add("app cache deletion"); }
      }
    } catch (e) { failed.add("app cache deletion"); }
    if (failed.size) {
      const message = `Reloaded, but failed: ${[...failed].join("; ")}.`;
      try { sessionStorage.setItem(reloadFailure, message); }
      catch (e) { toast(`${message} Could not save this notice for after reload.`, true); }
    }
    location.reload();
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-hard-reload]");
    if (!b) return;
    ev.preventDefault();
    return hardReload();
  });

  // A failed refresh is said, above the content it would have replaced (which stays, marked stale): an empty or
  // frozen panel must never pass for "nothing to show".
  function stale(el, why) {
    let note = el.previousElementSibling;
    if (!note || !note.classList.contains("stale-note")) {
      note = document.createElement("p");
      note.className = "stale-note bad";
      note.setAttribute("role", "status");
      el.parentNode.insertBefore(note, el);
    }
    note.textContent = why;
    el.classList.add("stale");
  }

  function fresh(el) {
    const note = el.previousElementSibling;
    if (note && note.classList.contains("stale-note")) note.remove();
    el.classList.remove("stale");
  }

  // The server was updated while this page stayed open (a phone keeps an app's page for days): it says so, once.
  function checkVersion(r) {
    const mine = (document.querySelector('meta[name="static-version"]') || {}).content;
    const theirs = r.headers.get("X-Shaft-Static");
    if (!mine || !theirs || mine === theirs || document.getElementById("updated")) return;
    const bar = document.createElement("div");
    bar.id = "updated";
    bar.setAttribute("role", "status");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "SHAFT was updated — reload";
    b.setAttribute("data-hard-reload", "");
    bar.appendChild(b);
    document.body.appendChild(bar);
  }

  function toBottom(el) {
    el._atBottom = true;
    const box = el.closest("[data-scroll]");
    if (box) { box.scrollTop = box.scrollHeight; el._top = box.scrollTop; }
    else window.scrollTo(0, document.body.scrollHeight);
  }

  // What a refresh must not replace: text being selected (to quote it) — except in a keyed feed, whose morph leaves the
  // message holding the selection as it is (a selection left in the chat stopped its refresh for good) —, a document
  // being edited (Save's check says if it changed), a field being typed in (a new document's name).
  // An editor counts while it is on screen or holds unsaved text: one left open, unchanged, in a closed documents
  // sheet stopped the panel's refresh for the rest of the visit (no new-document badge, nothing opened on its own).
  function busyIn(el) {
    if (selectingIn(el) && !el.dataset.keyed) return true;
    if ([...el.querySelectorAll(".editing")].some(v => v.getClientRects().length || editDirty(v))) return true;
    const typing = document.activeElement;
    return !!(typing && el.contains(typing) && typing.matches("input, textarea"));
  }
  function editDirty(view) {
    const box = view.querySelector("form.docedit textarea"), raw = view.querySelector("textarea.raw");
    return !!box && !!raw && box.value !== raw.value;
  }

  // The network (docs/plans/PLAN-pwa P3). A refresh sends the version it has (If-None-Match) and an unchanged one costs a 304;
  // it gives up after 15 s; a failure backs off (up to 30 s) and is said once, at the top, not under every panel;
  // the connection coming back or the app coming to the front refreshes at once.
  const POLL_LIMIT_MS = 15000;
  const net = { down: new Set() };
  function netNote(text) {
    let n = document.getElementById("netnote");
    if (!text) { if (n) n.remove(); return; }
    if (!n) {
      n = document.createElement("div");
      n.id = "netnote";
      n.setAttribute("role", "status");
      document.body.appendChild(n);
    }
    n.textContent = text;
  }

  // Replace a keyed feed's items one by one (data-k): what did not change stays in place, with its scroll, its open
  // strips and anything the page added to it (copy buttons); a turn being sent stays until its own turn arrives —
  // the one with its message id (data-mid): one whose turn came in a refresh before its send returned stayed beside
  // it, "sending…", for good. An item holding the selection is not replaced: false then, and the caller asks again.
  function morph(el, html) {
    const next = new DOMParser().parseFromString(`<!doctype html><body>${html}`, "text/html").body;
    const children = [...el.children];
    const arrived = new Set([...next.children].map(n => n.dataset.mid).filter(Boolean));
    const sending = new Set(children.filter(c => c.classList.contains("sending") && !c.dataset.done
      && !arrived.has(c.dataset.mid)));
    const keyed = new Map(children.filter(c => !c.classList.contains("sending") && c.dataset.k)
      .map(c => [c.dataset.k, c]));
    const held = new Set(children.filter(c => selectingIn(c)));
    const kept = new Set();
    let complete = true;
    let cursor = el.firstElementChild;
    [...next.children].forEach(n => {
      const cur = keyed.get(n.dataset.k);
      const src = n.outerHTML;
      let node = cur;
      if (cur && cur._src !== src && held.has(cur)) complete = false;   // selected: left as it is until it lets go
      else if (!cur || cur._src !== src) {
        node = document.importNode(n, true);
        node._src = src;
        if (cur) {
          const details = new Map([...cur.querySelectorAll("details[data-key]")].map(d => [d.dataset.key, d.open]));
          node.querySelectorAll("details[data-key]").forEach(d => {
            if (details.has(d.dataset.key)) d.open = details.get(d.dataset.key);
          });
          // Where each scrolling part was, and whether at its end: one at the end follows what is added (a reply's
          // activity), one scrolled up stays where its reader left it
          const scrolls = new Map([...cur.querySelectorAll("[data-scroll-key]")].map(d => [d.dataset.scrollKey,
            { top: d.scrollTop, end: d.scrollTop + d.clientHeight >= d.scrollHeight - 4 }]));
          // Restore after insertion: detached elements have no scrollable layout.
          node._scrolls = scrolls;
          const clocks = new Map([...cur.querySelectorAll(".elapsed[data-since]")].map(d => [d.dataset.since, d.textContent]));
          node.querySelectorAll(".elapsed[data-since]").forEach(d => {
            if (clocks.has(d.dataset.since)) d.textContent = clocks.get(d.dataset.since);
          });
        } else if (node.classList.contains("turn") && node.classList.contains("it") && !node.classList.contains("live")) {
          const who = (node.querySelector(".who") || {}).firstChild;
          const first = (node.querySelector(".md p, .md li, .md h2, .md h3, p.warn") || {}).textContent || "";
          announce(`${who ? who.textContent.trim() : "It"} replied: ${first.slice(0, 200)}`);
        }
      }
      if (node !== cursor) el.insertBefore(node, cursor);
      cursor = node.nextElementSibling;
      kept.add(node);
      if (node._scrolls) {
        node.querySelectorAll("[data-scroll-key]").forEach(d => {
          const was = node._scrolls.get(d.dataset.scrollKey);
          if (was) d.scrollTop = was.end ? d.scrollHeight : was.top;
        });
        delete node._scrolls;
      }
    });
    children.forEach(c => {
      if (kept.has(c) || sending.has(c)) return;
      if (held.has(c)) { complete = false; return; }  // gone from the chat, but selected: it goes once let go
      c.remove();
    });
    sending.forEach(c => el.appendChild(c));          // kept last, until the server's turn replaces it
    return complete;
  }

  function poll(el) {
    const every = Number(el.dataset.every || 2000);
    // again: an action asked for a refresh while one was on its way. That one may hold the state from before the
    // action, so it is not shown, and the next one goes at once. Without it the refresh the action asked for was
    // cancelled by the one in flight (its finally reschedules), and dismissed alerts stayed up to 2 s more (2026-10-20).
    let running = false, timer = null, fails = 0, again = false;
    if (el.dataset.keyed) [...el.children].forEach(c => { c._src = c.outerHTML; });
    const later = (ms) => { clearTimeout(timer); timer = setTimeout(tick, ms); };
    async function tick() {
      const url = el.dataset.poll;                  // read each time: the documents panel changes what it shows
      if (!el.isConnected) { unbind(); return; }    // a poll inside one that replaced it (a reply's activity)
      if (running) { again = true; return; }
      const hidden = document.visibilityState !== "visible";
      // Slower, not stopped: a hidden tab (the rail keeps the unread marks and the badge), a panel not on screen
      // (the documents: an instance's first document must still make it appear).
      const slow = (hidden ? 5 : 1) * (el.dataset.lazy && !el.getClientRects().length ? 6 : 1);
      if ((hidden && !el.dataset.hidden) || busyIn(el)) { later(every); return; }
      running = true;
      const ctl = new AbortController();
      const limit = setTimeout(() => ctl.abort(), POLL_LIMIT_MS);
      try {
        // The feed's poll says when the page sent it: one that arrives after the page said it went away is not
        // watching (web_chat.watching)
        const headers = { ...(el.dataset.etag && el.dataset.etagUrl === url ? { "If-None-Match": el.dataset.etag } : {}),
                          ...(el.dataset.follow ? { "X-Shaft-At": String(Date.now()), "X-Shaft-Watching": sheet ? "0" : "1" } : {}) };
        const r = await fetch(url, { credentials: "same-origin", cache: "no-store", signal: ctl.signal, headers });
        if (pairAgain(r)) return;
        checkVersion(r);
        if (r.status >= 502 && r.status <= 504) {      // the proxy answered, SHAFT did not (Tailscale's 503): unreachable
          throw new Error(`its web service is not answering, HTTP ${r.status}`);
        }
        if (r.status === 304) {
          if (!again && el.dataset.poll === url) { fails = 0; recovered(); settled(el, url); }
          return;
        }
        const body = await r.text();
        if (r.ok) {
          fails = 0;
          // Asked again now: an edit or a selection begun while this refresh was on its way must survive it (Codex)
          if (busyIn(el) || el.dataset.poll !== url || again) return;
          el.dataset.etag = r.headers.get("ETag") || "";
          el.dataset.etagUrl = url;
          if (body !== el.dataset.last) {               // unchanged: nothing replaced, nothing collapsed
            const focused = document.activeElement;
            const controls = 'button, a[href], summary';
            const focusKey = n => n.dataset.focus || n.getAttribute("href")
              || (n.closest("form[data-post]") || { dataset: {} }).dataset.post;
            const restoreFocus = focused && el.contains(focused) && focused.matches(controls);
            const key = restoreFocus ? focusKey(focused) : null;
            const open = new Set([...el.querySelectorAll("details[data-key][open]")].map(d => d.dataset.key));
            const hover = new Set([...el.querySelectorAll("details[data-key][open]")].filter(d => d._hover).map(d => d.dataset.key));
            const shut = new Set([...el.querySelectorAll("details[data-key]:not([open])")].map(d => d.dataset.key));
            const box = el.closest("[data-scroll]");      // a feed that scrolls in its own box (the chat), else the page
            const atBottom = box && box.scrollHeight > box.clientHeight
              ? box.scrollTop + box.clientHeight >= box.scrollHeight - 40
              : window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
            const oldKeys = new Set([...el.querySelectorAll(".turn[data-k]")].map(n => n.dataset.k));
            // The feed this follows to its end: its own, or the one it sits in (the running reply's activity)
            const follower = el.dataset.follow ? el : el.closest && el.closest("[data-follow]");
            // A feed in its own box knows whether it is followed (the scroll listener): near the end is not enough —
            // a reader who scrolled up a little is reading, and was pulled back down.
            const following = follower && box && typeof follower._atBottom === "boolean" ? follower._atBottom : atBottom;
            if (follower) follower._atBottom = following;
            const whole = el.dataset.keyed ? morph(el, body) !== false : (el.innerHTML = body, true);
            if (whole) el.dataset.last = body;
            else { delete el.dataset.last; delete el.dataset.etag; }   // a selected item kept: the next one brings it
            el.querySelectorAll("[data-poll]").forEach(inner => { if (!inner._tick) poll(inner); });
            el.querySelectorAll("details[data-key]").forEach(d => {
              if (open.has(d.dataset.key)) d.open = true;
              if (hover.has(d.dataset.key)) d._hover = true;
              if (shut.has(d.dataset.key)) d.open = false;
            });
            if (restoreFocus) {
              const target = [...el.querySelectorAll(controls)].find(n => key && focusKey(n) === key)
                || el.querySelector('details[data-key] summary');
              if (target) target.focus({ preventScroll: true });
            }
            if (following && follower) toBottom(follower);
            // A poll inside the feed (the reply's activity) that says the reply ended: the feed brings the answer now
            if (follower && follower !== el && follower._tick && el.querySelector("[data-ended]")) follower._tick();
            const newBelow = !!box && [...el.querySelectorAll(".turn[data-k]")].some(n =>
              !oldKeys.has(n.dataset.k) && n.getBoundingClientRect().top >= box.getBoundingClientRect().bottom);
            el.dispatchEvent(new CustomEvent("shaft:swapped", { bubbles: true, detail: { newBelow } }));
          }
          settled(el, url);
          recovered();
        } else {
          fails += 1;
          net.down.add(el);
          stale(el, `Not refreshed: HTTP ${r.status} — ${textOf(body) || "no reason given"}`);
        }
      } catch (e) {                                    // no answer at all: the network, not this panel
        fails += 1;
        net.down.add(el);
        el.classList.add("stale");
        netNote(navigator.onLine === false ? "Offline — it refreshes when the connection is back"
          : `Cannot reach SHAFT: ${e.name === "AbortError" ? "no answer in 15 s" : e.message} — retrying`);
      } finally {
        clearTimeout(limit);
        running = false;
        if (again) { again = false; later(0); }
        else later(Math.min(every * slow * 2 ** Math.min(fails, 4), 15000 * slow));   // back within 15 s
      }
    }
    function settled(el, url) {               // the documents show the one asked for: loading is over (a 304 too)
      if (el.id !== "docs" || el.dataset.poll !== url) return;
      el.dataset.shown = url;
      el.classList.remove("loading");
      el.removeAttribute("aria-busy");
    }
    function recovered() {
      fresh(el);
      net.down.delete(el);
      for (const panel of net.down) { if (!panel.isConnected) net.down.delete(panel); }
      if (!net.down.size) netNote("");
    }
    el._tick = tick;
    later(el.dataset.now ? 0 : every);                 // a first render without live data (the rail): fill it now
    const onVisible = () => { if (document.visibilityState === "visible") tick(); };
    // Back from the back-forward cache only: the first pageshow is the load itself, whose page is the newest there is
    // (it fetched every panel again at once, the whole chat included — 310 KB for a long one).
    const onShow = (ev) => { if (ev && ev.persisted) tick(); };
    const onOnline = () => { fails = 0; tick(); };
    const onRefresh = () => { if (running) again = true; else later(300); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", onShow);
    window.addEventListener("online", onOnline);
    document.addEventListener("shaft:refresh", onRefresh);
    function unbind() {                                // gone from the page: nothing of it stays behind
      clearTimeout(timer);
      if (!document.removeEventListener) return;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", onShow);
      window.removeEventListener("online", onOnline);
      document.removeEventListener("shaft:refresh", onRefresh);
    }
  }


  // data-timeout is seconds and comes from the server's own limit (plus a grace the page added). Without it, a
  // chat with files waits longer than a plain form.
  function waitMsFor(form) {
    const asked = Number(form.dataset.timeout);
    if (asked > 0) return asked * 1000;
    return form.dataset.multipart ? 180000 : 30000;
  }

  // A lost answer. Only a chat send (the composer) carries an id the server will not apply twice. Every other action
  // may have happened — Cancel on a queued message carries its id too, and still may have —: say so, and where to
  // look, and never that it was not sent.
  const chatSend = (form) => /^\/chat\/[^/]+\/send$/.test(form.dataset.post || "");
  function lostAnswer(form, err, waitMs) {
    if (chatSend(form) && form.querySelector("input[name=mid]")) {
      return err.name === "AbortError" ? "No answer: sending it again is safe (it is not sent twice)"
        : `Not sent: ${err.message} — sending it again is safe`;
    }
    const where = form.dataset.check || "this page";
    if (err.name === "AbortError") {
      return `No answer within ${Math.round(waitMs / 1000)}s: it may still have happened — check ${where} before trying again`;
    }
    return `No answer (${err.message}): it may still have happened — check ${where} before trying again`;
  }

  // What a refusal box shows: this server's own refusal fragment, as it wrote it (escaped) — or, for a whole page (a
  // 403's notice page, the proxy's error) or plain text (413), only its words: a whole page landed inside the
  // document view, its navigation and all (W15, review 2026-10-24).
  function refusalNodes(r, body) {
    const html = (r.headers.get("Content-Type") || "text/html").toLowerCase().startsWith("text/html");
    if (html && !/^\s*<!doctype|<html[\s>]/i.test(body)) {
      return [...new DOMParser().parseFromString(body, "text/html").body.childNodes];
    }
    const doc = html ? new DOMParser().parseFromString(body, "text/html") : null;
    const notice = doc && doc.querySelector ? doc.querySelector("section.notice") : null;
    const words = (notice ? [...notice.querySelectorAll("h1, p")].map(n => n.textContent).join(" — ")
      : doc ? doc.body.textContent : body).replace(/\s+/g, " ").replace(/ — Back to app$/, "").trim();
    const p = document.createElement("p");
    p.textContent = words.slice(0, 600) || `HTTP ${r.status}`;
    return [p];
  }

  function showDiff(form, text) {
    const out = form.querySelector("[data-diff-out]");
    if (out) out.textContent = text;
    const step = form.querySelector("[data-diff-step]");
    if (step) step.hidden = false;
    const ask = form.querySelector("[data-diff-ask]");
    if (ask) ask.hidden = true;
    const show = form.querySelector("[data-diff-ask] button");
    if (show) show.disabled = true;             // Enter now submits Apply; the diff stays the one just shown
  }

  function hideDiff(form) {                     // Back, or the text changed: the box is kept, nothing was saved
    if (!form) return;
    const step = form.querySelector("[data-diff-step]");
    if (step) step.hidden = true;
    const out = form.querySelector("[data-diff-out]");
    if (out) out.textContent = "";
    const ask = form.querySelector("[data-diff-ask]");
    if (ask) ask.hidden = false;
    const show = form.querySelector("[data-diff-ask] button");
    if (show) show.disabled = false;
  }

  document.addEventListener("click", (ev) => {
    const back = ev.target.closest && ev.target.closest("[data-diff-back]");
    if (!back) return;
    hideDiff(back.closest("form"));
  });

  document.addEventListener("submit", async (ev) => {
    const form = ev.target.closest("form[data-post]");
    if (!form) return;
    ev.preventDefault();
    const applying = ev.submitter && ev.submitter.hasAttribute && ev.submitter.hasAttribute("data-diff-apply");
    if (form.dataset.diff && !applying) {       // the diff first; Apply is what saves. No confirm over it.
      try {
        const d = await fetch(form.dataset.diff, {
          method: "POST", credentials: "same-origin",
          headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(new FormData(form)),
        });
        if (pairAgain(d)) return;
        const text = fullText(await d.text());        // the whole diff, never cut like a toast
        if (!d.ok) { toast(`No diff: HTTP ${d.status} — ${text}`, true); return; }
        if (text === "(no change)") { toast("Nothing to save: no change"); return; }
        showDiff(form, text);
      } catch (e) {
        toast(`No diff: ${e.message}`, true);
      }
      return;
    }
    if (form.dataset.busy) return;                  // one at a time: a second Enter sent it twice (Codex review)
    const tooBig = filesRefused(form);
    if (tooBig) { toast(tooBig, true); return; }      // before uploading it all to hear so (a phone's video)
    if (form.dataset.confirmType) {                 // a word to type, not a click (shaft delete): the server checks it too
      const typed = window.prompt(`${form.dataset.confirm || ""}\n\nType ${form.dataset.confirmType} to confirm:`);
      if (typed === null) return;
      if (typed.trim() !== form.dataset.confirmType) { toast(`Not done: you typed "${typed}", not ${form.dataset.confirmType}`, true); return; }
      form.querySelector("input[name=confirm]").value = typed.trim();
    } else if (form.dataset.confirm && !form.dataset.diff && !window.confirm(form.dataset.confirm)) return;
    const buttons = [...form.querySelectorAll("button[type=submit]")].filter(b => !b.disabled);
    const queue = form.querySelector("input[name=queue]");
    if (queue && ev.submitter?.dataset.queue !== undefined) queue.value = ev.submitter.dataset.queue;
    // Next cycle: to its inbox, read in its next cycle — only from its button; Enter and Cmd-Enter never send so
    const background = form.querySelector("input[name=background]");
    if (background) background.value = ev.submitter?.dataset.background || "";
    if (form.dataset.draft) takeQuote(form);
    buttons.forEach(b => { b.disabled = true; });
    form.dataset.busy = "1";
    const box = form.querySelector("textarea[name=text]");
    const typed = box ? box.value : null;            // what this send carries
    const mid = chatSend(form) ? form.querySelector("input[name=mid]") : null;
    if (mid && !mid.value) mid.value = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const sentMid = mid ? mid.value : "";
    const payload = new FormData(form);
    if (queue) queue.value = "";              // the next send chooses its own mode
    const retryOf = form.querySelector("input[name=retry_of]");
    if (retryOf) retryOf.value = "";           // only the Retry that set it
    if (background) background.value = "";
    const sentFiles = [...(form.querySelector("input[type=file]") || { files: [] }).files];
    if (form.dataset.draft && mid) {
      try {
        localStorage.setItem(draftKey(form), typed);
        // files: their names — a reload cannot keep the files themselves, so it says to attach them again
        localStorage.setItem(pendingKey(form), JSON.stringify({ mid: sentMid, text: typed,
                                                                files: sentFiles.map(f => f.name) }));
      } catch (e) { /* no storage: retry still works in this page */ }
    }
    const shown = form.dataset.draft ? pending(form, sentMid) : null;
    // The field empties as the "sending…" bubble appears: for a second the text was in both (iPad review P8). Its draft
    // and the attempt stay saved; a send that fails puts the text back (unsent()).
    if (shown && box) { box.value = ""; grow(box); }
    const unsent = () => {
      if (!shown || !box || box.value === typed) return;
      box.value = box.value ? `${typed}\n\n${box.value}` : typed;   // what was typed meanwhile stays after it
      grow(box);
    };
    const multipart = !!form.dataset.multipart;          // files go with it: the browser sets the boundary
    const row = form.closest("[data-row]");
    const waitMs = waitMsFor(form);
    const ctl = new AbortController();
    const limit = setTimeout(() => ctl.abort(), waitMs);
    try {
      const r = await fetch(form.dataset.post, {
        method: "POST", credentials: "same-origin", signal: ctl.signal,
        headers: { "X-CSRF-Token": csrf(),
                   ...(!multipart ? { "Content-Type": "application/x-www-form-urlencoded" } : {}),
                   ...(row || form.dataset.addRow ? { "X-Row": "1" } : {}) },
        body: multipart ? payload : new URLSearchParams(payload),
      });
      if (pairAgain(r)) { if (shown) shown.remove(); unsent(); return; }
      const body = await r.text();
      // A message sent shows itself in the chat: its toast covered the header (iPad review P7). Anything else says so.
      if (!(r.ok && shown)) toast(textOf(body) || (r.ok ? "done" : `HTTP ${r.status}`), !r.ok);
      const refusal = form.querySelector("[data-refusal]") || (form.dataset.after === "doc-deleted"
                      ? form.closest(".docview").querySelector("[data-delete-refusal]") : null);
      if (refusal) refusal.replaceChildren(...(r.ok ? [] : refusalNodes(r, body)));
      const now = r.headers.get("X-Doc-Sha");         // refused as stale: Save again replaces it, knowingly
      if (!r.ok && now && form.querySelector("input[name=sha]")) form.querySelector("input[name=sha]").value = now;
      if (r.ok && r.headers.get("X-Row") === "1") {
        const next = new DOMParser().parseFromString(body, "text/html")
          .querySelector("template[data-saved-row]").content.firstElementChild;
        // rows the same save also changed (a model switch writes its base URL): swapped by id, after the first
        for (let more = next.nextElementSibling; more; more = more.nextElementSibling) {
          const shownNow = more.id && document.getElementById(more.id);
          if (shownNow) shownNow.replaceWith(more.cloneNode(true));
        }
        if (row) row.replaceWith(next);
        else if (form.dataset.addRow) {
          const existing = document.getElementById(next.id);   // Add can set a key already shown with its default
          if (existing) existing.replaceWith(next);
          else document.querySelector(form.dataset.addRow).appendChild(next);
        }
      }
      if (r.ok && form.dataset.after === "doc-saved") docSaved(form, r.headers.get("X-Doc"), typed, now);
      if (r.ok && form.dataset.after === "doc-deleted") {   // back to the folder it was in
        docsList(form.querySelector("input[name=path]").value.split("/").slice(0, -1).join("/"));
      }
      if (r.ok && form.dataset.after === "comment-saved") refreshDocs();
      if (r.ok && mid && mid.value === sentMid) mid.value = "";               // sent: the next message gets an id of its own
      if (r.ok && shown) {                            // goes when the feed brings the turn as the server saved it —
        shown.dataset.done = "1";                     // now, if a refresh brought it while this was on its way
        if ([...document.querySelectorAll(".feed [data-mid]")].some(n => n !== shown && n.dataset.mid === sentMid)) {
          shown.remove();
        }
      }
      // what it holds now is what is saved (a data-reset form goes back to its defaults below instead)
      if (r.ok && form.dataset.guard !== undefined && !form.dataset.reset) markSaved(form);
      if (r.ok && form.dataset.keep) dropKept(form);   // saved: the copy kept in this browser goes
      if (r.ok && form.dataset.reset) {
        const later = box && box.value && box.value !== typed ? box.value : null;   // typed while it was sending: kept
        if (form.dataset.draft) {
          box.value = later === null ? "" : later;
          const input = form.querySelector("input[type=file]");
          setFiles(form, [...input.files].filter(f => !sentFiles.includes(f)));
        } else {
          form.reset();
          if (later !== null) box.value = later;
        }
        picked(form);
        if (form.dataset.draft && later === null) sent(form);
        else if (box) grow(box);
        if (box && form.dataset.draft && !touchOnly()) box.focus();   // write the next one (a phone keeps its keyboard down)
      }
      if (!r.ok && shown) { shown.remove(); unsent(); }
      if (r.ok && form.dataset.diff) hideDiff(form);   // saved: back to the text, which is kept
      if (r.ok && form.dataset.after === "reload") setTimeout(() => location.reload(), 800);   // Unpair this browser
      if (r.ok && form.dataset.after === "home") setTimeout(() => { location.href = "/"; }, 1500);   // it is gone
      document.dispatchEvent(new Event("shaft:refresh"));      // show what the action changed, now
    } catch (e) {                                  // the answer was lost. A chat id is safe to repeat; anything else may have happened
      if (shown) { shown.remove(); unsent(); }
      toast(lostAnswer(form, e, waitMs), true);
    } finally {
      clearTimeout(limit);
      buttons.forEach(b => { b.disabled = false; });
      delete form.dataset.busy;
      delete form._afterDocSave;
    }
  });

  // A composer (form[data-files]): files picked, pasted or dropped — anywhere on its page — go with the message, each
  // shown as a chip (a thumbnail for an image) that × takes out again; Enter sends. Its draft is kept per instance in
  // this browser (data-draft), so a reload or a closed tab does not lose it; the server's chat is the record.
  const composer = () => document.querySelector("form[data-files]");

  function touchOnly() {                         // a phone or tablet: its primary pointer is a finger (iPadOS says so
    return window.matchMedia("(hover: none) and (pointer: coarse)").matches;   // even with a keyboard and trackpad)
  }
  // The on-screen keyboard is up: it hides much of the page (measured 340 of 1078 px on an iPad, 153 of 537 on an
  // iPhone), a hardware keyboard's shortcut bar far less. null where the browser cannot tell (no visualViewport).
  function keyboardUp() {
    const vv = window.visualViewport;
    return vv ? vv.height < window.innerHeight - 120 : null;
  }

  function filesRefused(form) {                  // why the picked files cannot go, or ""
    const input = form.querySelector("input[type=file]");
    if (!input || !input.files.length) return "";
    const files = [...input.files];
    const mb = (n) => `${(n / 1048576).toFixed(0)} MB`;
    const each = Number(form.dataset.maxFile || 0), total = Number(form.dataset.maxTotal || 0);
    const count = Number(form.dataset.maxFiles || 0);
    const big = files.find(f => each && f.size > each);
    if (big) return `${big.name} is larger than ${mb(each)}: not sent`;
    if (total && files.reduce((a, f) => a + f.size, 0) > total) return `together larger than ${mb(total)}: not sent`;
    if (count && files.length > count) return `at most ${count} files at a time`;
    return "";
  }

  function picked(form) {
    const input = form.querySelector("input[type=file]");
    const box = form.querySelector("[data-picked]");
    if (!input || !box) return;
    box.querySelectorAll("img").forEach(img => URL.revokeObjectURL(img.src));
    box.textContent = "";
    [...input.files].forEach((f, k) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      if (f.type.startsWith("image/")) {
        const img = document.createElement("img");
        img.src = URL.createObjectURL(f);
        img.alt = "";
        chip.appendChild(img);
      }
      const name = document.createElement("span");
      name.textContent = `${f.name} · ${f.size < 1024 * 1024 ? Math.ceil(f.size / 1024) + " KB" : (f.size / 1048576).toFixed(1) + " MB"}`;
      chip.appendChild(name);
      const x = document.createElement("button");
      x.type = "button";
      x.className = "unpick";
      x.title = `remove ${f.name}`;
      x.setAttribute("aria-label", `remove ${f.name}`);
      x.textContent = "×";
      x.dataset.unpick = String(k);
      chip.appendChild(x);
      if (Number(form.dataset.maxFile || 0) && f.size > Number(form.dataset.maxFile)) {
        chip.classList.add("over");
        chip.title = "too large to send";
      }
      box.appendChild(chip);
    });
  }

  function setFiles(form, files) {
    const input = form.querySelector("input[type=file]");
    if (!input) return;
    const all = new DataTransfer();
    for (const f of files) all.items.add(f);
    form._attachments = all;
    input.files = all.files;
    composerChanged(form);
    picked(form);
  }

  function addFiles(form, files) {
    const input = form.querySelector("input[type=file]");
    if (!input || !files.length) return;
    setFiles(form, [...(form._attachments ? form._attachments.files : []), ...files]);
  }

  document.addEventListener("click", (ev) => {
    const x = ev.target.closest && ev.target.closest("[data-unpick]");
    if (!x) return;
    const form = x.closest("form[data-files]");
    const input = form.querySelector("input[type=file]");
    setFiles(form, [...input.files].filter((_, k) => k !== Number(x.dataset.unpick)));
  });
  document.addEventListener("change", (ev) => {
    const form = ev.target.closest("form[data-files]");
    if (form && ev.target.type === "file") addFiles(form, [...ev.target.files]);
  });
  document.addEventListener("paste", (ev) => {
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    const files = [...(ev.clipboardData && ev.clipboardData.files || [])];
    if (form && files.length) { ev.preventDefault(); addFiles(form, files); }
  });

  // Drop anywhere on a page with a composer: a file dropped beside it must attach, never open in the tab instead.
  let dragDepth = 0;
  const hasFiles = (ev) => ev.dataTransfer && [...ev.dataTransfer.types].includes("Files");
  function zone(show) {
    const z = document.querySelector("[data-dropzone]");
    if (z) z.hidden = !show;
  }
  document.addEventListener("dragenter", (ev) => {
    if (!composer() || !hasFiles(ev)) return;
    dragDepth += 1;
    zone(true);
  });
  document.addEventListener("dragleave", () => {
    if (!composer()) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) zone(false);
  });
  document.addEventListener("dragover", (ev) => {
    if (composer() && hasFiles(ev)) ev.preventDefault();
  });
  document.addEventListener("drop", (ev) => {
    const form = composer();
    if (!form || !hasFiles(ev)) return;
    ev.preventDefault();
    dragDepth = 0;
    zone(false);
    addFiles(form, [...ev.dataTransfer.files]);
    form.querySelector("textarea").focus();
  });

  // The text box grows with what is written in it (up to a third of the window), and keeps its draft.
  function grow(box, part = 1 / 3) {           // at most `part` of the window
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight + 2, Math.round((window.visualViewport?.height || window.innerHeight) * part))}px`;
  }
  const draftKey = (form) => `shaft.draft.${form.dataset.draft}`;
  const pendingKey = (form) => `shaft.pending.${form.dataset.draft}`;
  function composerChanged(form) {
    const mid = form.querySelector("input[name=mid]");
    if (mid) mid.value = "";
    if (form.dataset.draft) {
      try { localStorage.removeItem(pendingKey(form)); } catch (e) { /* none */ }
    }
  }
  window.addEventListener("beforeunload", (ev) => {
    const form = composer();
    if (!form || !form.querySelector("input[type=file]").files.length) return;
    ev.preventDefault();
    ev.returnValue = "";
  });
  // An edit of a page's own value (drive.md, a Config or Settings value): leaving with it unsaved asks first, and a
  // form with data-keep keeps its text in this browser until it is saved — an iOS back-swipe lost a long drive.md
  // edit (W14, review 2026-10-24). Unsaved is unlike what the page was drawn with (or the saved row it got since).
  function fieldDirty(f) {
    if (f.type === "hidden" || f.type === "file" || f.type === "submit" || f.disabled) return false;
    if (f.type === "checkbox" || f.type === "radio") return f.checked !== f.defaultChecked;
    if (f.tagName === "SELECT") return [...f.options].some(o => o.selected !== o.defaultSelected);
    return f.value !== f.defaultValue;
  }
  const guardDirty = (form) => [...form.querySelectorAll("input, textarea, select")].some(fieldDirty);
  // A save that went through: the form's values are its saved ones. A saved row usually replaces the form; when none
  // came back (or for a form that is not a row), the old values still read as unsaved: leaving warned, and the next
  // input kept the "draft" again (Grok review of 8735c62). A data-reset form (Add a key) clears itself instead.
  function markSaved(form) {
    for (const f of form.querySelectorAll("input, textarea, select")) {
      if (f.type === "checkbox" || f.type === "radio") f.defaultChecked = f.checked;
      else if (f.tagName === "SELECT") [...f.options].forEach(o => { o.defaultSelected = o.selected; });
      else if (f.type !== "file") f.defaultValue = f.value;
    }
  }
  window.addEventListener("beforeunload", (ev) => {
    if (![...document.querySelectorAll("form[data-guard]")].some(guardDirty)) return;
    document.querySelectorAll("form[data-keep]").forEach(keepText);
    ev.preventDefault();
    ev.returnValue = "";
  });
  const keptKey = (form) => `shaft.draft.${form.dataset.keep}`;
  function keepText(form) {
    const box = form.querySelector("textarea");
    try {
      if (box.value === box.defaultValue) localStorage.removeItem(keptKey(form));
      else localStorage.setItem(keptKey(form), JSON.stringify({ text: box.value,
        sha: form._keptSha || (form.querySelector("input[name=sha]") || {}).value || "" }));
    } catch (e) { /* no storage: the leave warning still asks */ }
  }
  function dropKept(form) {
    try { localStorage.removeItem(keptKey(form)); } catch (e) { /* none */ }
  }
  function restoreKept(form) {
    const box = form.querySelector("textarea"), sha = form.querySelector("input[name=sha]");
    let kept = null;
    try { kept = JSON.parse(localStorage.getItem(keptKey(form)) || "null"); } catch (e) { /* none kept */ }
    if (!box || !kept || typeof kept.text !== "string" || kept.text === box.value) return;
    box.value = kept.text;
    form._keptSha = typeof kept.sha === "string" ? kept.sha : "";
    // The page's sha stays: Apply is checked against what is there now, and its diff, shown first, says what changes
    const older = sha && form._keptSha && form._keptSha !== sha.value;
    const note = form.querySelector("[data-keep-note]");
    if (note) note.textContent = older ? "Unsaved edit restored — begun on an older version: check the diff before Apply"
      : "Unsaved edit restored";
  }
  document.addEventListener("input", (ev) => {
    const reviewing = ev.target.closest && ev.target.closest("form[data-diff]");
    if (reviewing && ev.target.name === "text") hideDiff(reviewing);   // the shown diff is no longer this text
    const keeping = ev.target.closest && ev.target.closest("form[data-keep]");
    if (keeping && ev.target.tagName === "TEXTAREA") keepText(keeping);
    const docform = ev.target.closest && ev.target.closest("form.docedit");
    if (docform) { grow(ev.target, 0.7); keepDocDraft(docform); return; }
    const midf = ev.target.closest && ev.target.closest("form[data-files]");
    if (midf) composerChanged(midf);
    const form = ev.target.closest && ev.target.closest("form[data-draft]");
    if (!form || ev.target.tagName !== "TEXTAREA") return;
    grow(ev.target);
    try { localStorage.setItem(draftKey(form), ev.target.value); } catch (e) { /* no storage: no draft kept */ }
  });
  // An attempt whose answer was lost: delivered if the chat has its id — the ids the page was drawn with
  // (data-sent-mids), or a turn the feed brought since (data-mid: the page's list is never refreshed by the poll).
  function delivered(form, mid) {
    return JSON.parse(form.dataset.sentMids || "[]").includes(mid)
      || [...document.querySelectorAll(".feed [data-mid]")].some(n => !n.classList.contains("sending") && n.dataset.mid === mid);
  }
  function restoreDraft() {
    const form = document.querySelector("form[data-draft]");
    if (!form) return;
    const box = form.querySelector("textarea");
    try { form._quote = localStorage.getItem(quoteKey(form)) || ""; } catch (e) { /* none kept */ }
    if (form._quote) quoteChip(form);
    try {
      if (!box.value) box.value = localStorage.getItem(draftKey(form)) || "";
      const attempt = JSON.parse(localStorage.getItem(pendingKey(form)) || "null");
      if (attempt && attempt.text === box.value && typeof attempt.mid === "string") {
        if (delivered(form, attempt.mid)) {
          box.value = "";
          localStorage.removeItem(draftKey(form));
          localStorage.removeItem(pendingKey(form));
        } else {
          form.querySelector("input[name=mid]").value = attempt.mid;
          const files = Array.isArray(attempt.files) ? attempt.files.filter(f => typeof f === "string") : [];
          // Files cannot outlive a reload: sent again as it stands, it would go without them, unsaid
          if (files.length) toast(`Not sent yet, and its files could not be kept: attach ${files.join(", ")} again`, true);
        }
      }
    } catch (e) { /* no usable saved attempt */ }
    grow(box);
  }
  // The feed brought the turn of an attempt still in the composer (its answer was lost, it landed after all): the
  // composer lets it go, as a successful send does, unless it was changed since.
  function reconcileSent() {
    const form = document.querySelector("form[data-draft]");
    const mid = form && form.querySelector("input[name=mid]");
    if (!mid || !mid.value || form.dataset.busy || !delivered(form, mid.value)) return;
    const box = form.querySelector("textarea");
    let attempt = null;
    try { attempt = JSON.parse(localStorage.getItem(pendingKey(form)) || "null"); } catch (e) { /* none */ }
    if (attempt && attempt.mid === mid.value && attempt.text !== box.value) return;   // edited since: a new message
    box.value = "";
    if (form.querySelector("input[type=file]").files.length) setFiles(form, []);   // they went with it
    mid.value = "";
    sent(form);
  }
  function sent(form) {                          // the attempt went: neither its draft nor its retry id is kept
    try {
      localStorage.removeItem(draftKey(form));
      localStorage.removeItem(pendingKey(form));
    } catch (e) { /* none */ }
    grow(form.querySelector("textarea"));
  }

  // What you send shows in the feed at once, marked "sending", until the feed's next refresh replaces it with the
  // chat as the server saved it; if it fails, it goes and your text and files stay in the composer.
  function pending(form, mid) {
    const feed = document.querySelector(".feed[data-follow]");
    const text = (form.querySelector("textarea") || {}).value || "";
    const files = [...(form.querySelector("input[type=file]") || { files: [] }).files].map(f => f.name);
    if (!feed || (!text.trim() && !files.length)) return null;
    const turn = document.createElement("div");
    turn.className = "turn me sending";
    if (mid) turn.dataset.mid = mid;                 // the server's turn carries the same: then this one goes
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = "you · sending…";
    const body = document.createElement("div");
    body.className = "wrap";
    body.textContent = text.trim() + (files.length ? `\n📎 ${files.join(", ")}` : "");
    turn.append(who, body);
    feed.appendChild(turn);
    toBottom(feed);
    return turn;
  }

  document.addEventListener("mouseover", (ev) => {
    const d = ev.target && ev.target.closest && ev.target.closest('details[data-key="busy-state"]');
    if (d && !d.open && !touchOnly()) { d.open = true; d._hover = true; }
  });
  document.addEventListener("mouseout", (ev) => {
    const d = ev.target && ev.target.closest && ev.target.closest('details[data-key="busy-state"]');
    if (d && d._hover && (!ev.relatedTarget || !d.contains || !d.contains(ev.relatedTarget))) {
      d.open = false;
      delete d._hover;
    }
  });
  document.addEventListener("click", (ev) => {
    const s = ev.target && ev.target.closest && ev.target.closest('details[data-key="busy-state"] > summary');
    const busy = s && s.closest('details[data-key="busy-state"]');
    if (busy) {
      ev.preventDefault();
      if (busy._hover) delete busy._hover;
      else busy.open = !busy.open;
    }
    document.querySelectorAll("details.menu[open]").forEach(d => {
      if (d !== busy && (!ev.target || !d.contains || !d.contains(ev.target))) {
        d.open = false;
        delete d._hover;
      }
    });
  });

  // A tap on Send (or its menu) must not take focus from the text: the keyboard closing first moved the button out from
  // under the finger, and the tap only closed the keyboard — nothing was sent (iPhone review I2, 3 of 3 in Safari).
  // mousedown: iOS sends it after the touch, where focus would move; the click still comes. The keyboard stays up, as
  // in Messages.
  document.addEventListener("mousedown", (ev) => {
    const b = ev.target.closest && ev.target.closest("form.composer button.send, form.composer .sendmenu button, "
      + "form.composer .sendmenu > summary");
    const box = b && b.closest("form.composer").querySelector("textarea");
    if (b && box && document.activeElement === box) ev.preventDefault();
  });
  // Long-press the send arrow: the other ways to send (the ⌃ beside it opens the same menu). Pointer events, and the
  // finger must still be down when the time is up: with touch events a plain tap opened the menu after sending.
  let sendPress = null;
  document.addEventListener("pointerdown", (ev) => {
    const b = ev.pointerType === "touch" && ev.target.closest && ev.target.closest("form.composer button.send");
    if (!b) return;
    clearTimeout(sendPress && sendPress.timer);
    delete b._pressed;                         // a new press: an old one's "no send" must not eat this one's click
    sendPress = { id: ev.pointerId, timer: setTimeout(() => {
      if (!sendPress || sendPress.id !== ev.pointerId) return;
      sendPress = null;
      const menu = b.closest("form.composer").querySelector(".sendmenu");
      if (menu) { menu.open = true; b._pressed = ev.pointerId; }
    }, 500) };
  });
  for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
    document.addEventListener(type, (ev) => {
      if (sendPress && sendPress.id === ev.pointerId) { clearTimeout(sendPress.timer); sendPress = null; }
      // cancelled after the menu opened (a drag away): no click comes to clear it, so it goes now
      if (type === "pointercancel") document.querySelectorAll("form.composer button.send").forEach(b => {
        if (b._pressed === ev.pointerId) delete b._pressed;
      });
    }, true);
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("form.composer button.send");
    if (b && b._pressed !== undefined) { ev.preventDefault(); delete b._pressed; }   // the long press opened the menu: no send
  }, true);
  document.addEventListener("submit", (ev) => {        // chosen from the menu: it closes
    const menu = ev.target.querySelector && ev.target.querySelector(".sendmenu[open]");
    if (menu) menu.open = false;
  }, true);

  let hardwareKeys = false;                      // a hardware keyboard has typed in this page (M18, the keydown below)
  // The ⋯ menu opened from the keyboard: focus goes to its first item (a click leaves focus where it is)
  document.addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (!d.matches || !d.matches("details.chatmenu") || !d.open || !d._byKey) return;
    delete d._byKey;
    const first = d.querySelector(".menubody a, .menubody button:not(:disabled)");
    if (first) first.focus();
  }, true);
  document.addEventListener("keydown", (ev) => {
    const s = (ev.key === "Enter" || ev.key === " ") && ev.target.closest && ev.target.closest("details.chatmenu > summary");
    if (s) s.parentElement._byKey = true;
  }, true);
  function sendComposer(form, queued) {
    const queue = form.querySelector("input[name=queue]");
    if (queue) queue.value = queued ? "1" : "";
    try { form.requestSubmit(); } finally { if (queue) queue.value = ""; }
  }
  document.addEventListener("beforeinput", (ev) => {      // the touch Return the keydown above marked: send, no new line
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    if (!form || !form._returnSends || (ev.inputType !== "insertLineBreak" && ev.inputType !== "insertParagraph")) return;
    form._returnSends = false;
    ev.preventDefault();
    sendComposer(form, false);
  });

  document.addEventListener("keydown", (ev) => {
    const busySummary = ev.target && ev.target.closest && ev.target.closest('details[data-key="busy-state"] > summary');
    if (busySummary && (ev.key === "Enter" || ev.key === " ")) {
      ev.preventDefault();
      const d = busySummary.closest('details[data-key="busy-state"]');
      if (d) { delete d._hover; d.open = !d.open; }
      return;
    }
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    const inBox = form && ev.target.tagName === "TEXTAREA";
    // A hardware keyboard proves itself: a key with a physical code (KeyA, Enter), or a modifier or arrow an on-screen
    // keyboard has not. A floating iPad keyboard leaves the page its height, so "no keyboard up" alone took it for a
    // hardware one and Return sent (review C6/M18). Until proven, Return on a touch device is a new line.
    if (inBox && touchOnly() && keyboardUp() === false && ((ev.code && ev.code !== "Unidentified")
        || ["Shift", "Meta", "Control", "Alt", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape"].includes(ev.key))) {
      hardwareKeys = true;
      document.body.classList.add("hwkbd");
    }
    // On a touch device Return sends only while no on-screen keyboard is up (a hardware keyboard is typing, as on an
    // iPad's), and then when its line break arrives (beforeinput below): iOS applies the Return's autocorrection after
    // this keydown, so sending here would send the uncorrected word. With the on-screen keyboard it stays a new line.
    if (inBox) form._returnSends = ev.key === "Enter" && !ev.isComposing && touchOnly() && !ev.metaKey && !ev.ctrlKey
      && !ev.altKey && !ev.shiftKey && keyboardUp() === false && hardwareKeys;
    // Cmd/Ctrl+Enter queues on hardware keyboards, touch devices too.
    if (inBox && ev.key === "Enter" && !ev.isComposing && (!touchOnly() || ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      if (ev.altKey || ev.shiftKey) {
        const box = ev.target, at = box.selectionStart;
        box.value = box.value.slice(0, at) + "\n" + box.value.slice(box.selectionEnd);
        box.setSelectionRange(at + 1, at + 1);
        box.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        sendComposer(form, ev.metaKey || ev.ctrlKey);
      }
      return;
    }
    if (sheet && ev.key !== "Escape") return;       // covered chat shortcuts stay inactive
    // Shortcuts on a chat: / writes, Esc closes a menu, Alt-↑ / Alt-↓ go to the chat above or below in the rail.
    const typing = ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable]");
    if (ev.key === "Escape") {
      document.querySelectorAll("details.menu[open]").forEach(d => {
        const inside = d.contains && d.contains(document.activeElement);
        d.open = false;
        delete d._hover;
        const summary = inside && d.querySelector("summary");   // focus back where it was opened (review C8/M13)
        if (summary) summary.focus();
      });
      return;
    }
    if (ev.key === "/" && !typing && composer()) {
      ev.preventDefault();
      composer().querySelector("textarea").focus();
      return;
    }
    if (ev.altKey && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
      const chats = [...document.querySelectorAll("nav .rail a[data-inst]")];
      const at = chats.findIndex(a => a.classList.contains("on"));
      const to = chats[at + (ev.key === "ArrowUp" ? -1 : 1)];
      if (at >= 0 && to) { ev.preventDefault(); location.href = to.href; }
    }
  });

  // Copy an answer (as the Markdown it was written in) or a code block.
  const icons = Object.fromEntries(Object.entries({
    copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H4v13h4"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
  }).map(([name, path]) => [name, '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + '</svg>']));
  function copyByHand(text) {                   // where the Clipboard API is refused (a browser's site setting)
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "sr";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
  async function copyText(text, button) {
    let why = "";
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      why = e.message;
      if (copyByHand(text)) why = "";
    }
    if (why) { toast(`Not copied: ${why}`, true); return; }
    clearTimeout(button._copied);
    button.innerHTML = icons.check;
    button._copied = setTimeout(() => { button.innerHTML = icons.copy; }, 1200);
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-copy]");
    if (!b) return;
    const pre = b.closest(".prewrap") ? b.closest(".prewrap").querySelector("pre") : b.closest("pre");
    copyText(pre ? pre.querySelector("code, span.code")?.textContent ?? pre.textContent
                 : (b.closest("[data-raw]") || {}).dataset?.raw || "", b);
  });
  // A copy button on each code block of an answer, a document or the operator's own message: beside the block, in a
  // wrapper, not inside it — the block scrolls sideways, and a button inside scrolled away with its first lines (W18,
  // review 2026-10-24).
  function decorate(root) {
    root.querySelectorAll(".turn.it pre, .turn.me pre.code, .docview pre").forEach(pre => {
      if (pre.parentElement && pre.parentElement.classList.contains("prewrap")) return;
      const wrap = document.createElement("div");
      wrap.className = "prewrap";
      pre.parentNode.insertBefore(wrap, pre);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "copy small icon";
      b.dataset.copy = "";
      b.title = "Copy the code";
      b.setAttribute("aria-label", "Copy the code");
      b.innerHTML = icons.copy;
      wrap.append(pre, b);
    });
  }

  // How long the reply has been going, by the second.
  setInterval(() => {
    document.querySelectorAll(".elapsed[data-since]").forEach(e => {
      const t = Date.parse(e.dataset.since);
      if (Number.isNaN(t)) return;
      const s = Math.max(0, Math.round((Date.now() - t) / 1000));
      e.textContent = ` · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    });
  }, 1000);

  // Scrolled up when something new arrives: say so, and one click goes down to it.
  document.addEventListener("shaft:swapped", (ev) => {
    const feed = ev.target;
    if (feed.querySelectorAll) decorate(feed);
    if (!feed.dataset || !feed.dataset.follow) return;
    syncQueue(feed);
    reconcileSent();
    const box = feed.closest("[data-scroll]");
    const nb = document.querySelector("[data-newbelow]");
    if (box && nb && ev.detail?.newBelow && !feed._atBottom) nb.hidden = false;
  });
  // Queue after reply is offered only while a reply runs: the feed shows it as its live turn (data-k="live").
  function syncQueue(feed) {
    const b = document.querySelector('form[data-files] button[data-queue="1"]');
    if (b && feed.querySelector) b.hidden = !feed.querySelector('[data-k="live"]');
  }
  // A scroll the reader made: a wheel, a finger, a scrollbar, a key. Only those stop the feed following: the keyboard
  // closing after Send also moves the box up, and your own message then waited under "New below" (iPhone review I8).
  // A finger or a scrollbar held down counts until it lifts: held, then dragged after 1.5 s, it did not count (Codex).
  let readerScrolled = 0, readerHolding = false;
  for (const type of ["wheel", "touchmove", "pointerdown", "keydown"]) {
    document.addEventListener(type, (ev) => {
      if (type === "keydown" && !["ArrowUp", "PageUp", "Home", " "].includes(ev.key)) return;
      if (ev.target && ev.target.closest && ev.target.closest("[data-scroll]")) {
        // a press only holds (a scrollbar dragged): a tap is not a scroll — Retry's tap, then the feed shortening,
        // stopped it following (found live, 2026-10-27)
        if (type === "pointerdown") readerHolding = true;
        else readerScrolled = Date.now();
      }
    }, { capture: true, passive: true });
  }
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => { readerHolding = false; }, { capture: true, passive: true });
  }
  document.addEventListener("scroll", (ev) => {
    const box = ev.target;
    if (box.matches && box.matches("[data-scroll]")) {
      // Following stops when the reader scrolls up, however little, and only then: a box made shorter under it (the
      // composer growing with a restored draft) also leaves the end out of view, and that stopped the feed from
      // following (2026-10-26). A box made taller moves it up too, but leaves it exactly at the end.
      const feed = box.querySelector("[data-follow]");
      const gap = box.scrollHeight - box.clientHeight - box.scrollTop;
      if (feed && gap <= 1) feed._atBottom = true;
      else if (feed && !(box.scrollTop >= feed._top) && (readerHolding || Date.now() - readerScrolled < 1500)) feed._atBottom = false;
      else if (feed && gap <= 40) feed._atBottom = true;     // came back down to it
      if (feed) feed._top = box.scrollTop;
    }
    const nb = document.querySelector("[data-newbelow]");
    if (nb && box.matches && box.matches("[data-scroll]") && box.scrollTop + box.clientHeight >= box.scrollHeight - 40) {
      nb.hidden = true;
    }
  }, true);
  document.addEventListener("click", (ev) => {
    const nb = ev.target.closest && ev.target.closest("[data-newbelow]");
    if (!nb) return;
    const feed = document.querySelector(".feed[data-follow]");
    if (feed) toBottom(feed);
    nb.hidden = true;
  });

  document.addEventListener("load", (ev) => {
    if (!ev.target.matches || !ev.target.matches("img")) return;
    const feed = ev.target.closest("[data-follow]");
    if (feed && feed._atBottom) toBottom(feed);
  }, true);

  // Reply to part of a message: select text in the chat's feed and a "Reply" button appears by it; it puts the text,
  // quoted ("> "), in the composer, where the rest of the message is written.
  function selectingIn(el) {
    const s = window.getSelection();
    return !!s && !s.isCollapsed && s.rangeCount > 0 && el.contains(s.getRangeAt(0).commonAncestorContainer);
  }

  function quoteButton() {
    let b = document.getElementById("quote-reply");
    if (!b) {
      b = document.createElement("button");
      b.id = "quote-reply";
      b.type = "button";
      b.textContent = "↩ Reply";
      b.hidden = true;
      // mousedown, not click: a click would first clear the selection being quoted
      b.addEventListener("mousedown", (ev) => { ev.preventDefault(); quoteSelection(); });
      b.addEventListener("touchend", (ev) => { ev.preventDefault(); quoteSelection(); });
      b.addEventListener("click", () => quoteSelection());   // the keyboard (Enter, Space); a mouse's has gone above
      document.body.appendChild(b);
    }
    return b;
  }

  function quotable() {                         // the selection, when it lies inside one message of a chat feed
    const s = window.getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return null;
    const range = s.getRangeAt(0);
    const node = range.commonAncestorContainer;
    const host = (node.nodeType === 1 ? node : node.parentElement);
    const feed = host && host.closest(".feed, .docview");
    const composer = document.querySelector("form.composer textarea");
    if (!feed || !composer || !s.toString().trim()) return null;
    return { text: s.toString(), range, composer, doc: feed.dataset.doc || "" };
  }

  function placeQuoteButton() {
    const b = quoteButton();
    const q = quotable();
    if (!q) { b.hidden = true; return; }
    const r = q.range.getBoundingClientRect();
    b.hidden = false;
    // In the window (position: fixed), below the selection — a phone shows its own copy/paste menu above it — and
    // kept inside the window when the selection runs to its bottom edge.
    const below = r.bottom + 6 + b.offsetHeight <= window.innerHeight - 8;
    b.style.top = `${below ? r.bottom + 6 : Math.max(8, r.top - b.offsetHeight - 6)}px`;
    b.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - b.offsetWidth - 8))}px`;
  }
  // The feed and the documents scroll in their own boxes: the button follows its selection when either moves.
  document.addEventListener("scroll", () => { if (!quoteButton().hidden) placeQuoteButton(); }, true);
  window.addEventListener("resize", () => { if (!quoteButton().hidden) placeQuoteButton(); });

  function quoteSelection() {
    const q = quotable();
    quoteButton().hidden = true;
    if (!q) return;
    // From a document shown over the chat (the phone's sheet, full screen): the composer behind it is inert, and the
    // quote went there unseen with the sheet left open. Out of full screen first, then the sheet closes (W2).
    if (q.doc && sheet && sheet.kind === "docs") {
      const grid = document.querySelector(".chatgrid");
      if (grid && grid.classList.contains("docfullscreen")) fullscreenDoc(false);
      if (sheet && sheet.kind === "docs") showDocs(false);
    }
    quoteText(q.text, q.composer, q.doc);
  }
  // A quote waits above the field as a chip, not as "> " lines typed into it (iPhone review I17). Sent, it goes first,
  // as the same Markdown quote (web_views._my_text shows it quoted); it is kept with the draft (shaft.quote.<name>).
  const quoteKey = (form) => `shaft.quote.${form.dataset.draft}`;
  function quoteChip(form) {
    let chip = form.querySelector(".quotechip");
    const quote = form._quote || "";
    try { if (quote) localStorage.setItem(quoteKey(form), quote); else localStorage.removeItem(quoteKey(form)); }
    catch (e) { /* no storage: the quote lives as long as the page */ }
    if (!quote) { if (chip) chip.remove(); return; }
    if (!chip) {
      chip = document.createElement("div");
      chip.className = "quotechip";
      form.insertBefore(chip, form.querySelector(".cbox") || form.querySelector("textarea"));
    }
    const shown = document.createElement("span");
    shown.textContent = quote.replace(/^> ?/gm, "").trim();
    const x = document.createElement("button");
    x.type = "button";
    x.className = "unquote";
    x.setAttribute("aria-label", "Remove the quote");
    x.textContent = "×";
    x.addEventListener("click", () => { form._quote = ""; quoteChip(form); form.querySelector("textarea").focus(); });
    chip.replaceChildren(shown, x);
  }
  function quoteText(text, box, doc = "") {
    const from = doc ? `> (from notes/docs/${doc})\n` : "";     // a quote from a document names it
    const quoted = from + text.trim().split(/\r?\n/).map(l => `> ${l}`.trimEnd()).join("\n");
    const form = box.closest("form[data-draft]");
    form._quote = form._quote ? `${form._quote}\n>\n${quoted}` : quoted;
    quoteChip(form);
    window.getSelection().removeAllRanges();
    box.focus();
    composerChanged(form);                                        // a new message: a new id
  }
  function takeQuote(form) {                     // at Send: the quote goes before the text, and the chip goes
    const box = form.querySelector("textarea[name=text]");
    if (!form._quote || !box) return;
    box.value = `${form._quote}\n\n${box.value}`;
    form._quote = "";
    quoteChip(form);
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-reply-message]");
    const box = document.querySelector("form.composer textarea");
    if (b && box) quoteText(b.closest("[data-raw]").dataset.raw, box);
  });
  // Retry sends the message that failed again, in one tap when the failed request cannot have run anything
  // (classify_reply: no tools ran); Retry then Send was two taps for one intent (iPhone review I14). When it may have
  // run something, Retry asks first (data-retry-confirm) and sends a new message, the failed try kept (no retry_of).
  // A draft in the composer is not replaced without asking.
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-retry]");
    if (!b) return;
    const box = document.querySelector("form.composer textarea");
    if (!box) return;
    const text = b.dataset.retry;
    if (!text) return;
    const form = box.closest("form");
    if (b.dataset.retryConfirm && !confirm(b.dataset.retryConfirm)) return;
    // A quote waiting in the composer is part of the draft: it is asked about and dropped, never sent with the failed
    // message (Codex review of the branch). The composer's busy guard and the message id keep a second tap from a
    // second send; Retry is not disabled, so a resend that failed can be tried again.
    if ((box.value && box.value !== text) || form._quote) {
      if (!confirm("Replace your draft with the failed message?")) return;
      if (form._quote) { form._quote = ""; quoteChip(form); }
    }
    if (box.value !== text) {                  // a second click must not append a copy
      box.value = text;
      box.dispatchEvent(new Event("input", { bubbles: true }));   // a draft, and a new message id
    }
    const of = form.querySelector("input[name=retry_of]");
    if (of) of.value = b.dataset.retryOf || "";   // in place: the failed try folds into one line (iPad review P9)
    sendComposer(form, false);
  });

  // Feedback stays in the supervisor. Only an explicit Send delivers it.
  const feedbackPending = new Set();
  async function keepFeedback(group, thumb, reason, clear, send = false) {
    const target = group.dataset.feedbackTarget;
    const name = group.dataset.feedbackName;
    const pending = name + ":" + target;
    if (feedbackPending.has(pending)) return false;
    feedbackPending.add(pending);
    const buttons = [...group.querySelectorAll("button")];
    buttons.forEach(b => { b.disabled = true; });
    try {
      const post = async op => {
        const body = new URLSearchParams(JSON.parse(target));
        body.set("op", op);
        body.set("id", group.dataset.feedbackId || "");
        body.set("thumb", thumb);
        body.set("reason", reason);
        return fetch("/feedback/" + encodeURIComponent(name), {
          method: "POST", credentials: "same-origin", body, headers: { "X-CSRF-Token": csrf() }
        });
      };
      // A new down rating must exist before Send can name its current id.
      if (send && group.dataset.feedbackThumb !== thumb) {
        const kept = await post("rate");
        if (pairAgain(kept)) return false;
        if (!kept.ok) { toast(textOf(await kept.text()), true); return false; }
        group.dataset.feedbackId = kept.headers.get("X-Feedback-Id");
        group.dataset.feedbackThumb = thumb;
        group.dataset.feedbackReason = reason;
      }
      const r = await post(clear ? "clear" : send ? "send" : "rate");
      if (pairAgain(r)) return false;
      toast(textOf(await r.text()) || `HTTP ${r.status}`, !r.ok);
      if (r.ok) {
        const feed = document.querySelector(".feed[data-poll]");
        if (feed) { if (feed._tick) feed._tick(); }
        else location.reload();
      }
      return r.ok;
    } catch (e) {
      toast(`Feedback could not be kept: ${e.message}`, true);
      return false;
    } finally {
      feedbackPending.delete(pending);
      buttons.forEach(b => { b.disabled = false; });
    }
  }
  function feedbackSheet(button, group, sending = false) {
    if (document.querySelector("dialog.feedback-sheet[open]")) return;   // one closing is removed on its close event
    const dialog = document.createElement("dialog");
    dialog.className = "feedback-sheet";
    dialog.setAttribute("aria-labelledby", "feedback-label");
    const thumb = sending ? group.dataset.feedbackThumb : "down";
    dialog.innerHTML = '<form><label id="feedback-label" for="feedback-reason">What was ' +
      (thumb === "up" ? 'good' : 'wrong') + '? (optional)</label>' +
      '<textarea id="feedback-reason" name="reason" maxlength="2000" rows="4"></textarea>' +
      '<div class="feedback-actions"><button type="submit" class="primary" value="send">Send to ' +
      group.dataset.feedbackName + '</button>' +
      (sending ? '' : '<button type="submit" value="keep">Just keep it</button>') +
      '<button type="button" data-feedback-cancel>Cancel</button></div></form>';
    const form = dialog.querySelector("form");
    const reason = dialog.querySelector("textarea");
    reason.value = group.dataset.feedbackReason || "";
    let saving = false;
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      if (saving) return;
      saving = true;
      const save = form.querySelector("button[type=submit]");
      save.disabled = true;
      // Only the Send button sends: a submit with no known button (Enter, an extension) just keeps it.
      if (await keepFeedback(group, thumb, reason.value, false, !!ev.submitter && ev.submitter.value === "send"))
        dialog.close();
      saving = false;
      save.disabled = false;
    });
    dialog.querySelector("[data-feedback-cancel]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("keydown", ev => ev.stopPropagation());
    dialog.addEventListener("close", () => {
      dialog.remove();
      syncSheets();
      if (button.isConnected) button.focus({ preventScroll: true });
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    reason.focus();
  }
  document.addEventListener("click", ev => {
    const button = ev.target.closest && ev.target.closest("[data-thumb], [data-feedback-send]");
    if (!button || button.disabled) return;
    const group = button.closest("[data-feedback-target]");
    if (!group) return;
    if (button.dataset.feedbackSend !== undefined) feedbackSheet(button, group, true);
    else if (button.getAttribute("aria-pressed") === "true") keepFeedback(group, button.dataset.thumb, "", true);
    else if (button.dataset.thumb === "down") feedbackSheet(button, group);
    else keepFeedback(group, "up", "", false);
  });

  // A message's actions as a menu (PLAN-pwa-market decision 2): long-press on touch, right-click or the context-menu key
  // on a computer, as in Messages or Slack. They are the message's own buttons (shown on hover and Tab, hidden on
  // touch): the menu presses them. On touch, "Select text" lets the text be selected, which a long press now is not.
  const selectIcon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 4h4M7 4v16M5 20h4M13 8h7M13 12h7M13 16h5"/></svg>';
  function msgMenuBox() {
    let m = document.getElementById("msgmenu");
    if (!m) {
      m = document.createElement("div");
      m.id = "msgmenu";
      m.setAttribute("role", "menu");
      m.setAttribute("aria-label", "Message actions");
      m.hidden = true;
      m.addEventListener("keydown", (ev) => {
        const items = [...m.querySelectorAll("button")];
        const at = items.indexOf(document.activeElement);
        if (ev.key === "Escape" || ev.key === "Tab") { ev.preventDefault(); closeMsgMenu(true); }
        else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          items[(at + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
        }
      });
      document.body.appendChild(m);
    }
    return m;
  }
  function closeMsgMenu(back) {
    const m = document.getElementById("msgmenu");
    if (!m || m.hidden) return;
    m.hidden = true;
    const turn = m._turn;
    m._turn = null;
    if (!turn) return;
    turn.classList.remove("menuopen");
    const first = back && turn.querySelector(".msgactions button");
    if (first && first.getClientRects().length) first.focus({ preventScroll: true });
  }
  // The whole message: an answer is one .md; the operator's own is a run of parts (a quote, text, a code block).
  function selectText(turn) {
    const md = turn.querySelector(".md");
    const parts = md ? [md] : [...turn.querySelectorAll(".wrap, pre.code")].filter(e => !e.parentElement.closest(".wrap"));
    if (!parts.length) return;
    turn.classList.add("selecting");
    const range = document.createRange();
    range.setStartBefore(parts[0]);
    range.setEndAfter(parts[parts.length - 1]);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  function openMsgMenu(turn, x, y, keyboard) {
    const acts = [...turn.querySelectorAll(".msgactions button, .who [data-reply-message], .who [data-thumb], .who [data-feedback-send]")];
    if (!acts.length) return false;
    const m = msgMenuBox();
    m.textContent = "";
    const items = acts.map(b => [b.dataset.feedbackSend !== undefined ? "Send feedback…" : b.dataset.thumb ? (b.getAttribute("aria-pressed") === "true" ? "Clear thumbs " : "Thumbs ") +
      (b.dataset.thumb === "up" ? "up" : "down") : b.dataset.copy !== undefined ? "Copy" : "Reply", b.dataset.feedbackSend !== undefined ? "" : b.innerHTML, () => b.click()]);
    if (touchOnly()) items.push(["Select text", selectIcon, () => selectText(turn)]);
    for (const [label, svg, run] of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.innerHTML = svg;
      b.append(document.createTextNode(label));
      b.addEventListener("click", () => { closeMsgMenu(false); run(); });
      m.appendChild(b);
    }
    closeMsgMenu(false);
    m._turn = turn;
    turn.classList.add("menuopen");
    m.hidden = false;
    if (keyboard) {
      const r = turn.getBoundingClientRect();
      x = r.left + 12;
      y = Math.min(r.bottom, window.innerHeight - 8);
    }
    const w = m.offsetWidth, hgt = m.offsetHeight;
    m.style.left = `${Math.max(8, Math.min(x, window.innerWidth - w - 8))}px`;
    m.style.top = `${y + 8 + hgt <= window.innerHeight - 8 ? y + 8 : Math.max(8, y - hgt - 8)}px`;
    if (keyboard) m.querySelector("button").focus();
    return true;
  }
  // The message pressed, or null: a link, a button or the activity log keep their own touch (a boolean here was taken
  // for the message, and the long press threw before its timer started — Codex review of the branch)
  const pressable = (t) => !t || !t.closest || t.closest("a, button, summary, input, textarea, select, details.activity, #msgmenu")
    ? null : t.closest(".feed .turn");
  let press = null;                     // pointer events, and the finger still down when the time is up
  document.addEventListener("pointerdown", (ev) => {
    const turn = ev.pointerType === "touch" && pressable(ev.target);
    if (!turn || !ev.isPrimary || turn.classList.contains("selecting")) return;
    const x = ev.clientX, y = ev.clientY, id = ev.pointerId;
    if (press) clearTimeout(press.timer);
    press = { x, y, id, timer: setTimeout(() => {
      if (!press || press.id !== id) return;
      press = null;
      openMsgMenu(turn, x, y);
    }, 500) };
  });
  document.addEventListener("pointermove", (ev) => {
    if (press && press.id === ev.pointerId && Math.hypot(ev.clientX - press.x, ev.clientY - press.y) > 10) {
      clearTimeout(press.timer);
      press = null;
    }
  }, true);
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, (ev) => {
      if (press && press.id === ev.pointerId) { clearTimeout(press.timer); press = null; }
    }, true);
  }
  document.addEventListener("contextmenu", (ev) => {
    const turn = ev.target && ev.target.closest && ev.target.closest(".feed .turn");
    if (!turn || ev.target.closest("a, input, textarea")) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && turn.contains(sel.anchorNode)) return;   // the browser's own menu copies a selection
    const keyboard = !ev.clientX && !ev.clientY;                           // the context-menu key, Shift-F10
    if (openMsgMenu(turn, ev.clientX, ev.clientY, keyboard)) ev.preventDefault();
  });
  document.addEventListener("pointerdown", (ev) => {
    const m = document.getElementById("msgmenu");
    if (m && m._turn && !m.contains(ev.target)) closeMsgMenu(false);      // _turn: it is open
  }, true);
  document.addEventListener("scroll", () => closeMsgMenu(false), true);
  document.addEventListener("selectionchange", () => {      // Select text ends when the selection is let go
    const sel = window.getSelection();
    document.querySelectorAll(".feed .turn.selecting").forEach(t => {
      if (!sel || sel.isCollapsed || !t.contains(sel.anchorNode)) t.classList.remove("selecting");
    });
  });

  let quoteTimer = null;
  document.addEventListener("selectionchange", () => {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(placeQuoteButton, 120);    // after the selection settles (a drag, a phone's handles)
  });

  // The rail (docs/plans/PLAN-webui-chat §1): an instance whose last answer this browser has not seen is marked unread. What was
  // seen is kept in this browser only (a convenience: losing it marks a chat unread, nothing more).
  function seenGet() {
    try { return JSON.parse(localStorage.getItem("shaft.seen") || "{}"); } catch (e) { return {}; }
  }
  function seenSet(v) {
    try { localStorage.setItem("shaft.seen", JSON.stringify(v)); } catch (e) { /* private window: no marks kept */ }
  }
  // Seen: a reply, or a message it sent you (data-sent-at), whose start is inside the feed's visible box — or that
  // fills the box, longer than the screen. Rendered was
  // not enough: a reply below the fold was marked seen, and a message it sent never was (W8, review 2026-10-24).
  function onScreen(n) {
    if (!n.getClientRects().length) return false;   // not rendered (a hidden panel)
    const r = n.getBoundingClientRect();
    const box = n.closest && n.closest("[data-scroll]");
    const b = box ? box.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    return (r.top >= b.top && r.top < b.bottom) || (r.top <= b.top && r.bottom >= b.bottom);   // its start, or it fills it
  }
  function seenFeed() {
    if (sheet || document.visibilityState !== "visible") return;  // a covering sheet hides the feed
    const seen = seenGet();
    let changed = false;
    document.querySelectorAll(".feed [data-seen-reply], .feed [data-sent-at]").forEach(item => {
      const inst = item.dataset.seenInst, last = item.dataset.seenReply || item.dataset.sentAt;
      if (inst && last && (seen[inst] || "") < last && onScreen(item)) {
        seen[inst] = last;
        changed = true;
      }
    });
    if (changed) seenSet(seen);
  }
  function markRail(rail) {
    const seen = seenGet();
    rail.querySelectorAll("a[data-inst]").forEach(a => {
      const last = a.dataset.last || "";
      a.classList.toggle("unread", !!last && (seen[a.dataset.inst] || "") < last);
    });
    const unread = rail.querySelectorAll("a.unread").length;
    document.querySelectorAll("[data-toggle-nav]").forEach(b => b.classList.toggle("news", unread > 0));
    if (navigator.setAppBadge) {                    // the Home Screen icon's count: an app shows no tab title
      (unread ? navigator.setAppBadge(unread) : navigator.clearAppBadge()).catch(() => {});
    }
    document.title = document.title.replace(/^\(\d+\) /, "");
    if (unread) document.title = `(${unread}) ${document.title}`;
  }
  // Opening a chat lands on its last message (the operator's call, 2026-10-26: it used to open at the first unread one).
  // Then it stays there while you follow it: whatever grows the feed or shrinks its box after the jump (the draft
  // growing the composer, the documents sheet, an image, the phone's keyboard) scrolls it back to the end — unless you
  // scrolled up (_atBottom, kept by the scroll listener).
  function openFeed(feed) {
    toBottom(feed);
    const box = feed.closest("[data-scroll]");
    if (!box || typeof ResizeObserver === "undefined" || feed._pinned) return;
    feed._pinned = new ResizeObserver(() => {
      if (feed._atBottom) { box.scrollTop = box.scrollHeight; feed._top = box.scrollTop; }
    });
    feed._pinned.observe(box);
    feed._pinned.observe(feed);
  }
  function unreadState() {
    seenFeed();                                    // only rendered current-chat replies carry these attributes
    const rail = document.querySelector("nav .rail");
    if (rail) markRail(rail);
  }
  // Notifications (docs/plans/PLAN-pwa P4): Web Push, the only way a reply reaches a phone whose app is closed or locked (an
  // iPhone stops a page's scripts in the background). "On" means SHAFT has this device's subscription, not only the browser.
  const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const ios = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  function keyBytes(b64) {
    const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64.length + 3) % 4));
    return Uint8Array.from(s, c => c.charCodeAt(0));
  }
  const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  // A renewal is tried again until it gets through: `refused` only stops one SHAFT turned down from being asked for
  // on every refresh of this page. One begun and not finished (the old subscription dropped for a new key, then
  // subscribe() failed) is remembered (shaft.push.renew): the next visit subscribes instead of reading "off".
  let renewing = false, refused = false;
  const store = (k, v) => { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* none */ } };
  const stored = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  async function renew(reg, sub, key, again) {
    if (renewing || refused) return "incomplete";
    renewing = true;
    try {
      if (again) {
        store("shaft.push.renew", "1");
        if (sub) await sub.unsubscribe();
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
      }
      const r = await fetch("/push/subscribe", { method: "POST", credentials: "same-origin",
        body: JSON.stringify(sub), headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/json" } });
      if (!r.ok) { refused = true; return "incomplete"; }
      store("shaft.push.renew", "");
      return "on";
    } finally {
      renewing = false;
    }
  }
  async function pushState() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return ios() && !standalone() ? "home" : "none";
    }
    if (Notification.permission === "denied") return "blocked";
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    const halfway = !!stored("shaft.push.renew") && !stored("shaft.push.off") && Notification.permission === "granted";
    if (!sub && !halfway) return "off";
    try {                                    // a later visit: SHAFT may have dropped this device since
      const r = await fetch("/push/status", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) return "incomplete";
      const held = await r.json();
      if (!sub) return held.key ? await renew(reg, null, held.key, true) : "incomplete";
      // W5 (review 2026-10-24): SHAFT dropped it (a push service refused it), holds another endpoint (the browser
      // rotated it), or it was made for another key: subscribed again, with the key pushes are signed with now.
      const theirs = sub.options && sub.options.applicationServerKey;
      const keyChanged = held.stale || (!!held.key && !!theirs && b64url(theirs) !== held.key);
      const off = !!stored("shaft.push.off");   // turned off here: a browser subscription left behind is not renewed
      if (Notification.permission === "granted" && !off && held.key
          && (!held.subscribed || held.endpoint !== sub.endpoint || keyChanged)) {
        return await renew(reg, sub, held.key, keyChanged);
      }
      return held.subscribed && held.endpoint === sub.endpoint && !keyChanged ? "on" : "incomplete";
    } catch (e) {
      return "incomplete";
    }
  }
  async function notifyButton() {
    const b = document.querySelector("[data-notify]");
    if (!b) return;
    const state = await pushState().catch(() => "none");
    b.dataset.state = state;
    b.hidden = state === "none";
    b.disabled = state === "blocked" || state === "home";
    b.textContent = { home: "For notifications, add SHAFT to your Home Screen (Share → Add to Home Screen)",
                      blocked: "Notifications are blocked for SHAFT in this browser's settings",
                      on: "Notifications on — turn off",
                      incomplete: "Notifications incomplete — retry",
                      off: "Notify me on this device" }[state] || "";
  }
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", credentials: "same-origin", body,
                                 headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/json" } });
    if (pairAgain(r)) return false;
    toast(textOf(await r.text()) || `HTTP ${r.status}`, !r.ok);
    return r.ok;
  }
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-notify]");
    if (!b || b.disabled) return;
    let sub = null;                          // set only while a browser subscription is not yet accepted by SHAFT
    try {
      const reg = await navigator.serviceWorker.ready;
      if (b.dataset.state === "on") {
        // the browser drops it only after SHAFT has: a failed unsubscribe must not read as off
        if (await post("/push/unsubscribe", "{}")) {
          store("shaft.push.off", "1");
          store("shaft.push.renew", "");
          sub = await reg.pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
          sub = null;
        }
      } else {
        if (await Notification.requestPermission() !== "granted") { notifyButton(); return; }
        store("shaft.push.off", "");
        sub = await reg.pushManager.getSubscription();   // incomplete: send the one the browser already has
        if (!sub) {
          const r = await fetch("/push/key", { credentials: "same-origin" });
          if (pairAgain(r)) return;
          const { key, error } = await r.json();
          if (!key) throw new Error(error || "no key");
          sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
        }
        if (!await post("/push/subscribe", JSON.stringify(sub))) {
          try { await sub.unsubscribe(); } catch (e) { /* refused: the button must not say on */ }
        }
        sub = null;
      }
    } catch (e) {
      if (sub) { try { await sub.unsubscribe(); } catch (e2) { /* the button must not say on */ } }
      toast(`Notifications: ${e.message}`, true);
    }
    notifyButton();
  });
  document.addEventListener("shaft:swapped", (ev) => {
    if (ev.target.classList && (ev.target.classList.contains("rail") || ev.target.classList.contains("feed"))) unreadState();
  });
  document.addEventListener("visibilitychange", unreadState);
  // Hidden (the phone locked, the app went to the background, another tab) or left (pagehide, visible or not): SHAFT
  // is told at once, so a reply that ends in the next seconds is pushed to this device and not taken as seen on screen
  // here (W9, review 2026-10-24). Stamped, like the feed's polls, so one sent before it cannot undo it. A GET with
  // keepalive, not sendBeacon: a beacon is a POST, and a POST needs the page's token in a header it cannot carry.
  function away(ev) {
    const feed = document.querySelector(".feed[data-follow][data-poll]");
    if (!feed || /[?&]id=/.test(feed.dataset.poll)) return;
    if (ev && ev.type === "visibilitychange" && document.visibilityState !== "hidden") return;
    const url = feed.dataset.poll + (feed.dataset.poll.includes("?") ? "&" : "?") + `away=1&at=${Date.now()}`;
    try { fetch(url, { credentials: "same-origin", cache: "no-store", keepalive: true }).catch(() => {}); }
    catch (e) { /* the page is going: nothing to do */ }
  }
  document.addEventListener("visibilitychange", away);
  window.addEventListener("pagehide", away);
  let seenTimer = null;                            // scrolled down to a reply: it is seen now, not at the next refresh
  document.addEventListener("scroll", (ev) => {
    if (!ev.target.matches || !ev.target.matches("[data-scroll]")) return;
    clearTimeout(seenTimer);
    seenTimer = setTimeout(unreadState, 250);
  }, true);
  window.addEventListener("storage", (ev) => {
    if (ev.key === "shaft.seen" || ev.key === null) unreadState();
  });

  // The documents panel (docs/plans/PLAN-webui-chat §3): a card in a reply or a name in its list opens that document in the
  // panel, which then refreshes it as the instance writes it; the address keeps ?doc= so a reload shows the same one.
  // What the panel polls for: one document (?doc=), or the list (?list=1: without it the server opens what this chat
  // last changed, so ← Documents would bounce straight back to it) of a folder (?dir=, none for notes/docs/ itself), or a
  // text file the instance sent (?att=).
  function docsPoll(panel, doc, thread = "", att = "", dir = "") {
    const poll = new URL(panel.dataset.poll, location.href);
    for (const k of ["doc", "thread", "list", "att", "dir"]) poll.searchParams.delete(k);
    if (att) poll.searchParams.set("att", att);
    else poll.searchParams.set(doc ? "doc" : "list", doc || "1");
    if (doc && thread) poll.searchParams.set("thread", thread);
    if (!doc && !att && dir) poll.searchParams.set("dir", dir);
    panel.dataset.poll = poll.pathname + poll.search;
  }
  function docDirty(view) {
    return view.querySelector("form.docedit textarea").value !== view.querySelector("textarea.raw").value;
  }
  function docDraftKey(form) {
    return `shaft.draft.doc.${form.dataset.post}.${form.querySelector("input[name=path]").value}`;
  }
  function clearDocDraft(form) {
    try { localStorage.removeItem(docDraftKey(form)); } catch (e) { /* no storage */ }
  }
  function keepDocDraft(form) {
    const view = form.closest(".docview");
    if (!docDirty(view)) { clearDocDraft(form); return; }
    try {
      localStorage.setItem(docDraftKey(form), JSON.stringify({
        text: form.querySelector("textarea").value,
        sha: form._draftSha || form.querySelector("input[name=sha]").value,
      }));
    } catch (e) { /* no storage: beforeunload still warns */ }
  }
  function restoreDocDraft(view) {
    const form = view.querySelector("form.docedit");
    if (!form || view.classList.contains("editing")) return;
    try {
      const draft = JSON.parse(localStorage.getItem(docDraftKey(form)));
      if (!draft || typeof draft.text !== "string" || typeof draft.sha !== "string") return;
      const sha = form.querySelector("input[name=sha]");
      const note = form.querySelector("[data-doc-draft]");
      note.textContent = draft.sha !== sha.value
        ? "Unsaved draft restored — started from an older version" : "Unsaved draft restored";
      form.querySelector("textarea").value = draft.text;
      sha.value = draft.sha;
      form._draftSha = draft.sha;
      editing(view, true);
    } catch (e) { /* no usable draft */ }
  }
  window.addEventListener("beforeunload", (ev) => {
    const view = document.querySelector("#docs .docview.editing");
    if (!view || !docDirty(view)) return;
    keepDocDraft(view.querySelector("form.docedit"));
    ev.preventDefault();
    ev.returnValue = "";
  });
  function leaveDoc(next) {
    const view = document.querySelector("#docs .docview.editing");
    if (!view) { next(); return; }
    const form = view.querySelector("form.docedit");
    if (form.dataset.busy) return;
    if (!docDirty(view)) { editing(view, false); next(); return; }
    const old = form.querySelector("[data-doc-choice]");
    if (old) old.remove();
    const prompt = document.createElement("div");
    prompt.dataset.docChoice = "";
    prompt.setAttribute("role", "group");
    prompt.setAttribute("aria-label", "Unsaved changes");
    prompt.textContent = "Save your changes? ";
    for (const label of ["Save", "Discard", "Stay"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", () => {
        if (form.dataset.busy) return;
        prompt.remove();
        if (label === "Save") { form._afterDocSave = next; form.requestSubmit(); }
        if (label === "Discard") { clearDocDraft(form); editing(view, false); next(); }
      });
      prompt.appendChild(button);
    }
    form.appendChild(prompt);
    prompt.lastElementChild.focus();
  }
  function docsList(dir = "") {
    const panel = document.getElementById("docs");
    if (!panel) return;
    const view = panel.querySelector(".docview.editing");
    if (view) editing(view, false);                 // polling resumes only after the editor lets go
    docsPoll(panel, "", "", "", dir);
    const u = new URL(location.href);
    u.searchParams.delete("doc");
    u.searchParams.delete("thread");
    u.searchParams.delete("att");
    if (dir) u.searchParams.set("dir", dir);
    else u.searchParams.delete("dir");
    history.replaceState(null, "", u);
    refreshDocs();
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-back-docs]");
    if (!b) return;
    const view = document.querySelector("#docs .docview.editing");
    if (view && view.querySelector("form.docedit textarea").value !== view.querySelector("textarea.raw").value
        && !window.confirm("Drop your changes?")) return;
    if (view) {
      if (view.querySelector("form.docedit").dataset.busy) return;
      clearDocDraft(view.querySelector("form.docedit"));
    }
    docsList(b.dataset.dir || "");                  // the open document's folder, or the folder above this one
  });
  // A column head above the list sorts it. The order is this browser's (a cookie the server reads, so a reload, another
  // chat or another tab lists them the same way): a ?sort= left by a link followed without the script would outrank it.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-sort-docs]");
    const panel = document.getElementById("docs");
    if (!a || !panel || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    document.cookie = `shaft_docsort=${a.dataset.sortDocs}; Path=/; Max-Age=${400 * 86400}; Secure; SameSite=Strict`;
    const poll = new URL(panel.dataset.poll, location.href);
    poll.searchParams.delete("sort");
    panel.dataset.poll = poll.pathname + poll.search;
    const u = new URL(location.href);
    if (u.searchParams.has("sort")) { u.searchParams.delete("sort"); history.replaceState(null, "", u); }
    refreshDocs();
  });
  // A folder in the list, or a step of its path above it, lists that folder.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-open-dir]");
    if (!a || !document.getElementById("docs") || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    showDocs(true, a);
    docsList(a.dataset.openDir);
  });
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-open-doc]");
    const panel = document.getElementById("docs");
    if (!a || !panel || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    const doc = a.dataset.openDoc;
    showDocs(true, a);                              // closed, or on a phone a sheet not yet open: open it
    const view = document.querySelector("#docs .docview.editing");
    if (view && view.dataset.doc === doc && docDirty(view) && !a.dataset.openThread) return;
    leaveDoc(() => {
      docsPoll(panel, doc, a.dataset.openThread);
      const u = new URL(location.href);
      u.searchParams.delete("att");
      u.searchParams.delete("dir");                 // its folder is in its path
      u.searchParams.set("doc", doc);
      if (a.dataset.openThread) u.searchParams.set("thread", a.dataset.openThread);
      else u.searchParams.delete("thread");
      history.replaceState(null, "", u);
      showDocs(true, a);
      refreshDocs();
    });
  });
  // A text file the instance sent opens read-only in the same panel instead of downloading; ⌘/Ctrl-click still
  // follows the link (a download), and so does a page without the panel.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-open-att]");
    const panel = document.getElementById("docs");
    if (!a || !panel || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    const att = a.dataset.openAtt;
    showDocs(true, a);
    leaveDoc(() => {                                // an unsaved edit asks first, as opening another document does
      docsPoll(panel, "", "", att);
      const u = new URL(location.href);
      u.searchParams.delete("doc");
      u.searchParams.delete("thread");
      u.searchParams.delete("dir");
      u.searchParams.set("att", att);
      history.replaceState(null, "", u);
      showDocs(true, a);
      refreshDocs();
    });
  });
  // Editing a document: the rendered view becomes its Markdown in a box; the panel stops refreshing until Save or
  // Cancel, and Save's compare-and-set refuses if the instance wrote it meanwhile (the box keeps your text).
  function editing(view, on) {
    view.classList.toggle("editing", on);
    view.querySelector(".docmd").hidden = on;
    view.querySelector("form.docedit").hidden = !on;
    if (on) {
      const form = view.querySelector("form.docedit");
      if (!form._draftSha) form._draftSha = form.querySelector("input[name=sha]").value;
      const box = view.querySelector("form.docedit textarea");
      box.focus();
      grow(box, 0.7);
    }
  }
  // Another document asked for: the panel says it is loading until it arrives — on a phone's network the old one stayed
  // up, looking current and usable, with nothing to say the tap was taken (review C12).
  function refreshDocs() {
    const panel = document.getElementById("docs");
    if (!panel) return;
    delete panel.dataset.etag;
    if (panel.dataset.shown !== undefined && panel.dataset.poll !== panel.dataset.shown) {
      panel.classList.add("loading");
      panel.setAttribute("aria-busy", "true");
    }
    if (panel._tick) panel._tick();
  }
  // The separate comment composer shares the page's request and feedback conventions.
  window.shaftWeb = { csrf, toast, textOf, refreshDocs, pairAgain };

  function docSaved(form, doc, typed, sha) {
    const panel = document.getElementById("docs");
    const view = form.closest(".docview");
    if (view) {
      form.querySelector("input[name=sha]").value = sha;
      form._draftSha = sha;
      view.querySelector("textarea.raw").value = typed;
      form.querySelector("[data-doc-draft]").textContent = "";
      clearDocDraft(form);
      if (form.querySelector("textarea").value !== typed) {
        keepDocDraft(form);
        return;
      }
      editing(view, false);
      if (form._afterDocSave) { form._afterDocSave(); return; }
      const edit = view.querySelector("[data-edit-doc]");
      if (edit) edit.focus();                       // focus where the editor was, not lost on the page
    }
    if (panel && doc) {
      docsPoll(panel, doc);
      if (!view) {                                  // a new document: the address opens it, not the folder it is in
        const field = document.activeElement;       // its name, still focused, held the panel (busyIn): it never
        if (field && form.contains(field)) field.blur();   // showed the new document until you clicked elsewhere
        const u = new URL(location.href);
        u.searchParams.delete("dir");
        u.searchParams.set("doc", doc);
        history.replaceState(null, "", u);
      }
    }
    if (panel && panel._tick) panel._tick();
  }
  // Open or closed is the operator's: beside the chat the panel shows once there are documents unless he closed it
  // (.hidedocs, remembered in this browser), and with none only when asked (.showdocs); on a phone it is a sheet.
  function docsOn() {
    const panel = document.getElementById("docs");
    return !!panel && panel.getClientRects().length > 0;
  }
  function docsAria() {                             // 📄 says whether the panel is on screen, however it got there
    document.querySelectorAll("[data-show-docs]").forEach(b => b.setAttribute("aria-expanded", String(docsOn())));
  }
  let sheet = null;
  const sheetOpeners = { docs: ["[data-show-docs]"], nav: [".chathead [data-toggle-nav]", ".pagebar [data-toggle-nav]"] };
  const openerOf = (kind) => sheetOpeners[kind].map(s => document.querySelector(s)).find(Boolean) || null;
  function sheetFocusables(root) {
    return [...root.querySelectorAll('button, a[href], input, textarea, select, summary, [tabindex], [contenteditable]')]
      .filter(el => !el.disabled && el.tabIndex >= 0 && !el.closest("[inert]") && el.getClientRects().length);
  }
  function sheetRoots() {
    // The document comment composer lives on body so a document refresh cannot destroy it.
    return [sheet.panel, ...(sheet.kind === "docs" ? [document.getElementById("comment-box")] : [])].filter(Boolean);
  }
  function focusSheet() {
    const close = sheet.panel.querySelector(sheet.kind === "nav" ? ".closenav" : "[data-close-docs]");
    (close || sheetFocusables(sheet.panel)[0] || sheet.panel).focus();
  }
  function syncSheets(opener) {
    if (document.querySelector("dialog.changelog[open], dialog.feedback-sheet[open]")) return;
    const grid = document.querySelector(".chatgrid");     // none on the other pages: only the rail's sheet there
    if (!grid && !document.querySelector(".pagebar")) return;
    const nav = document.querySelector("nav"), docs = grid ? document.getElementById("docs") : null;
    const fixed = el => el && el.getClientRects().length && getComputedStyle(el).position === "fixed";
    // Read the actual layout: Documents also becomes a sheet at the chat-area container breakpoint.
    const kind = document.body.classList.contains("navopen") && fixed(nav) ? "nav" : fixed(docs) ? "docs" : null;
    const panel = kind === "nav" ? nav : docs;
    if (sheet && (sheet.kind !== kind || sheet.panel !== panel)) {
      const old = sheet;
      old.inert.forEach(el => el.removeAttribute("inert"));
      old.attrs.forEach(([key, value]) => value === null ? old.panel.removeAttribute(key) : old.panel.setAttribute(key, value));
      sheet = null;
      const back = old.opener && old.opener.isConnected ? old.opener : openerOf(old.kind);
      if (back && back.getClientRects().length) back.focus();
      else if (old.panel.getClientRects().length) {
        const first = sheetFocusables(old.panel)[0];
        if (first) first.focus();                   // the phone opener is hidden beside the desktop rail
      }
    }
    if (kind && !sheet) {
      away();                                      // stop suppressing push as soon as the feed is covered
      sheet = { kind, panel, opener: opener || openerOf(kind), inert: [],
        attrs: ["role", "aria-modal", "aria-label", "tabindex", "aria-owns"].map(k => [k, panel.getAttribute(k)]) };
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-label", kind === "nav" ? "Chats and pages" : "Documents");
      panel.setAttribute("tabindex", "-1");
      if (kind === "docs") panel.setAttribute("aria-owns", "comment-box comment-sel quote-reply");
      // Isolate siblings at each level, never an ancestor of the dialog. Leave live regions on body audible. A
      // document's selection buttons are the sheet's own: ↩ Reply made inert did nothing at all (W2).
      for (let branch = panel; branch !== document.body && branch.parentElement; branch = branch.parentElement) {
        for (const sibling of branch.parentElement.children) {
          const live = ["toast", "netnote", "announce"].includes(sibling.id);
          const comment = kind === "docs" && ["comment-box", "comment-sel", "quote-reply"].includes(sibling.id);
          if (sibling !== branch && !live && !comment && !sibling.hasAttribute("inert")) {
            sibling.setAttribute("inert", "");
            sheet.inert.push(sibling);
          }
        }
      }
      focusSheet();
    } else if (sheet && !sheetRoots().some(root => root.contains(document.activeElement))) {
      focusSheet();                               // a polled document replaced the focused control
    }
    const expanded = !!grid && grid.classList.contains("docfullscreen");
    if (docs) docs.querySelectorAll("[data-fullscreen-doc]").forEach(button => {
      button.title = expanded ? "Exit full screen" : "Full screen";   // the label stays: aria-pressed says which
      button.setAttribute("aria-pressed", String(expanded));
    });
    docsAria();
    document.querySelectorAll("[data-toggle-nav]").forEach(b => b.setAttribute("aria-expanded", String(kind === "nav")));
  }
  function fullscreenDoc(on, opener) {
    const grid = document.querySelector(".chatgrid");
    if (!grid) return;
    grid.classList.toggle("docfullscreen", on);
    syncSheets(opener);
  }
  document.addEventListener("click", (ev) => {
    const button = ev.target.closest && ev.target.closest("[data-fullscreen-doc]");
    if (button) fullscreenDoc(!document.querySelector(".chatgrid").classList.contains("docfullscreen"), button);
  });
  function showNav(on, opener) {
    document.body.classList.toggle("navopen", on);
    syncSheets(opener);
  }
  function showDocs(on, opener) {
    const grid = document.querySelector(".chatgrid");
    if (!grid) return;
    if (!on) {
      grid.classList.remove("docfullscreen");
      // An editor with nothing changed closes with the panel; one with changes stays (its draft is kept), and the
      // panel keeps waiting for it (busyIn): left open unchanged, it froze the panel for good (W4)
      const view = document.querySelector("#docs .docview.editing");
      if (view && !editDirty(view)) editing(view, false);
    }
    grid.classList.toggle("showdocs", on);
    grid.classList.toggle("hidedocs", !on);
    try { if (on) localStorage.removeItem("shaft.docs.closed"); else localStorage.setItem("shaft.docs.closed", "1"); }
    catch (e) { /* no storage: it opens again on the next page */ }
    syncSheets(opener);
  }
  function chatHeaderState(el) {
    if (!el.querySelector(".chathead")) return;
    notifyButton();
    syncSheets();
    const rail = document.querySelector("nav .rail");
    if (rail) markRail(rail);
  }
  document.addEventListener("shaft:swapped", (ev) => {   // an instance's first document opens the panel beside the chat
    if (ev.target.id === "docs") {
      syncSheets();
      ev.target.querySelectorAll(".docview").forEach(restoreDocDraft);
    }
    chatHeaderState(ev.target);
  });
  document.addEventListener("click", (ev) => {           // 📄 Documents: open or close the panel
    const button = ev.target.closest && ev.target.closest("[data-show-docs]");
    if (!button) return;
    if (document.getElementById("docs")) showDocs(!docsOn(), button);
  });
  document.addEventListener("click", (ev) => {
    const edit = ev.target.closest && ev.target.closest("[data-edit-doc]");
    const cancel = ev.target.closest && ev.target.closest("[data-cancel-edit]");
    if (edit) { restoreDocDraft(edit.closest(".docview")); editing(edit.closest(".docview"), true); }
    if (cancel) {
      const view = cancel.closest(".docview");
      const form = view.querySelector("form.docedit");
      if (form.dataset.busy) return;
      const box = view.querySelector("form.docedit textarea");
      if (box.value !== view.querySelector("textarea.raw").value && !window.confirm("Drop your changes?")) return;
      clearDocDraft(form);
      const choice = form.querySelector("[data-doc-choice]");
      if (choice) choice.remove();
      form.querySelector("[data-doc-draft]").textContent = "";
      box.value = view.querySelector("textarea.raw").value;
      editing(view, false);
    }
  });
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-copy-doc]");
    if (!b) return;
    const raw = b.closest(".docview").querySelector("textarea.raw");
    copyText(raw ? raw.value : "", b);
  });

  // On a phone (docs/plans/PLAN-pwa P2) the chat is the whole screen: the rail is a sheet behind ☰, the documents a sheet over
  // the chat, and the page is as tall as what the keyboard leaves visible, so the composer sits right above it.
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest && ev.target.closest("[data-toggle-nav], [data-close-docs], nav a");
    if (!t) return;
    if (t.matches("[data-close-docs]")) {
      showDocs(false);
      return;
    }
    const open = t.matches("[data-toggle-nav]") ? !document.body.classList.contains("navopen") : false;
    showNav(open, open ? t : null);
  });
  document.addEventListener("keydown", (ev) => {
    if (!sheet) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      if (sheet.kind === "nav") showNav(false);
      else if (document.querySelector(".chatgrid").classList.contains("docfullscreen")) fullscreenDoc(false);
      else showDocs(false);
    } else if (ev.key === "Tab") {
      const items = sheetRoots().flatMap(sheetFocusables);
      const at = items.indexOf(document.activeElement);
      ev.preventDefault();
      if (!items.length) sheet.panel.focus();
      else items[at < 0 ? (ev.shiftKey ? items.length - 1 : 0) :
        (at + (ev.shiftKey ? -1 : 1) + items.length) % items.length].focus();
    }
  });
  function fitViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    // Pinched in to read: the visible part is smaller because it is magnified, not because of a keyboard. Resizing the
    // app to it shrank the layout under the reader's fingers and fought their panning (review C7).
    if (vv.scale > 1.01) return;
    const root = document.documentElement.style;
    root.setProperty("--vvh", `${Math.round(vv.height)}px`);
    // What the keyboard leaves visible, for the sheets fixed over the page (the documents, a comment): their bottom —
    // Save, Cancel — was under the keyboard (W3, review 2026-10-24). iOS may also pan the visible part (offsetTop).
    // Within a few px of the whole window and not panned: no keyboard — iOS can leave visualViewport a little off after
    // it closes, and the sheets kept a sliver of inset (Grok review of 8735c62). Never below 0.
    const whole = Math.abs(window.innerHeight - vv.height) <= 4 && Math.abs(vv.offsetTop) < 1;
    const top = whole ? 0 : Math.max(0, Math.round(vv.offsetTop));
    const bottom = whole ? 0 : Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    root.setProperty("--vvtop", `${top}px`);
    root.setProperty("--vvbottom", `${bottom}px`);
    const box = document.querySelector("form.composer textarea");
    if (box) grow(box);
    document.body.classList.toggle("kbd", keyboardUp());
    if (keyboardUp()) { document.body.classList.toggle("hwkbd", false); hardwareKeys = false; }   // the on-screen keyboard now: its shortcuts are not true
    // iOS scrolls the whole page to show a focused field; the page is already the visible height: put it back
    if (document.body.classList.contains("chatapp") && window.scrollY) window.scrollTo(0, 0);
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitViewport);
    window.visualViewport.addEventListener("scroll", fitViewport);
  }
  // iOS scrolls the page to show a focused field after the last visualViewport event: the whole chat went off screen,
  // the header and the feed above it and only the buttons left (iPhone review I1). The chat page never scrolls itself.
  window.addEventListener("scroll", () => {
    const vv = window.visualViewport;
    if (document.body.classList.contains("chatapp") && window.scrollY && !(vv && vv.scale > 1.01)) window.scrollTo(0, 0);
  }, { passive: true });
  document.addEventListener("focusin", (ev) => {
    if (ev.target.matches && ev.target.matches("form.composer textarea")) setTimeout(fitViewport, 350);
  });

  function start() {
    region("toast", "status");
    region("announce", "status");
    try {
      const message = sessionStorage.getItem(reloadFailure);
      if (message) {
        toast(message, true);
        sessionStorage.removeItem(reloadFailure);
      }
    } catch (e) { /* storage refused: the page still works */ }
    const grid = document.querySelector(".chatgrid");
    if (grid) {
      document.body.classList.add("chatapp");
      const docsPanel = document.getElementById("docs");
      if (docsPanel) docsPanel.dataset.shown = docsPanel.dataset.poll;   // drawn with the page: not loading
      const asked = new URL(location.href).searchParams;
      if (asked.get("doc") || asked.get("att") || asked.get("dir")) showDocs(true);
      else {
        try { if (localStorage.getItem("shaft.docs.closed")) grid.classList.add("hidedocs"); } catch (e) { /* none */ }
        syncSheets();
      }
      window.matchMedia("(max-width: 720px), (pointer: coarse) and (max-height: 520px)")
        .addEventListener("change", () => syncSheets());
      const observer = new ResizeObserver(() => syncSheets());
      observer.observe(grid.parentElement);        // main is the chat-area query container
    } else if (document.querySelector(".pagebar")) {   // another page: its rail is a sheet on a phone
      window.matchMedia("(max-width: 720px), (pointer: coarse) and (max-height: 520px)")
        .addEventListener("change", () => syncSheets());
      const on = document.querySelector(".tabs a.on");    // the strip of an instance's pages shows the one open
      if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "center" });
    }
    fitViewport();
    if ("serviceWorker" in navigator) {             // the app's shell: offline page and cached static files
      navigator.serviceWorker.register("/sw.js").catch(() => { /* not over https, or refused: the page works */ });
    }
    document.querySelectorAll("[data-poll]").forEach(poll);
    document.querySelectorAll("[data-follow]").forEach(el => { decorate(el); openFeed(el); syncQueue(el); });
    unreadState();                                  // after openFeed: what it scrolled past is now seen
    document.querySelectorAll(".docview").forEach(decorate);
    document.querySelectorAll(".docview").forEach(restoreDocDraft);
    restoreDraft();
    document.querySelectorAll("form[data-keep]").forEach(restoreKept);
    notifyButton();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
