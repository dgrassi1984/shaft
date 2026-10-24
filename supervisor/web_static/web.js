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
      for (const key of keys.filter(k => k.startsWith("shaft-"))) {
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
    if (box) box.scrollTop = box.scrollHeight;
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
          const scrolls = new Map([...cur.querySelectorAll("[data-scroll-key]")].map(d => [d.dataset.scrollKey, d.scrollTop]));
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
          if (node._scrolls.has(d.dataset.scrollKey)) d.scrollTop = node._scrolls.get(d.dataset.scrollKey);
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
        const headers = el.dataset.etag && el.dataset.etagUrl === url ? { "If-None-Match": el.dataset.etag } : {};
        const r = await fetch(url, { credentials: "same-origin", cache: "no-store", signal: ctl.signal, headers });
        if (pairAgain(r)) return;
        checkVersion(r);
        if (r.status >= 502 && r.status <= 504) {      // the proxy answered, SHAFT did not (Tailscale's 503): unreachable
          throw new Error(`its web service is not answering, HTTP ${r.status}`);
        }
        if (r.status === 304) {
          if (!again && el.dataset.poll === url) { fails = 0; recovered(); }
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
            if (follower) follower._atBottom = atBottom;
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
            if (atBottom && follower) toBottom(follower);
            const newBelow = !!box && [...el.querySelectorAll(".turn[data-k]")].some(n =>
              !oldKeys.has(n.dataset.k) && n.getBoundingClientRect().top >= box.getBoundingClientRect().bottom);
            el.dispatchEvent(new CustomEvent("shaft:swapped", { bubbles: true, detail: { newBelow } }));
          }
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
    function recovered() {
      fresh(el);
      net.down.delete(el);
      for (const panel of net.down) { if (!panel.isConnected) net.down.delete(panel); }
      if (!net.down.size) netNote("");
    }
    el._tick = tick;
    later(el.dataset.now ? 0 : every);                 // a first render without live data (the rail): fill it now
    const onVisible = () => { if (document.visibilityState === "visible") tick(); };
    const onShow = () => tick();                       // back from the cache or the app switcher
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
    buttons.forEach(b => { b.disabled = true; });
    form.dataset.busy = "1";
    const box = form.querySelector("textarea[name=text]");
    const typed = box ? box.value : null;            // what this send carries
    const mid = chatSend(form) ? form.querySelector("input[name=mid]") : null;
    if (mid && !mid.value) mid.value = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const sentMid = mid ? mid.value : "";
    const payload = new FormData(form);
    if (queue) queue.value = "";              // the next send chooses its own mode
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
      if (pairAgain(r)) { if (shown) shown.remove(); return; }
      const body = await r.text();
      toast(textOf(body) || (r.ok ? "done" : `HTTP ${r.status}`), !r.ok);
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
      if (r.ok && form.dataset.keep) dropKept(form);   // saved: the copy kept in this browser goes
      if (r.ok && form.dataset.reset) {
        const later = box && box.value !== typed ? box.value : null;   // typed while it was sending: kept
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
      if (!r.ok && shown) shown.remove();
      if (r.ok && form.dataset.diff) hideDiff(form);   // saved: back to the text, which is kept
      if (r.ok && form.dataset.after === "reload") setTimeout(() => location.reload(), 800);   // Unpair this browser
      if (r.ok && form.dataset.after === "home") setTimeout(() => { location.href = "/"; }, 1500);   // it is gone
      document.dispatchEvent(new Event("shaft:refresh"));      // show what the action changed, now
    } catch (e) {                                  // the answer was lost. A chat id is safe to repeat; anything else may have happened
      if (shown) shown.remove();
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
    // On a touch device Return sends only while no on-screen keyboard is up (a hardware keyboard is typing, as on an
    // iPad's), and then when its line break arrives (beforeinput below): iOS applies the Return's autocorrection after
    // this keydown, so sending here would send the uncorrected word. With the on-screen keyboard it stays a new line.
    if (inBox) form._returnSends = ev.key === "Enter" && !ev.isComposing && touchOnly() && !ev.metaKey && !ev.ctrlKey
      && !ev.altKey && !ev.shiftKey && keyboardUp() === false;
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
      document.querySelectorAll("details.menu[open]").forEach(d => { d.open = false; delete d._hover; });
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
  // A copy button on each code block of an answer or a document: beside the block, in a wrapper, not inside it — the
  // block scrolls sideways, and a button inside scrolled away with its first lines (W18, review 2026-10-24).
  function decorate(root) {
    root.querySelectorAll(".turn.it pre, .docview pre").forEach(pre => {
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
  document.addEventListener("scroll", (ev) => {
    const box = ev.target;
    if (box.matches && box.matches("[data-scroll]")) {
      const feed = box.querySelector("[data-follow]");
      if (feed) feed._atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
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
  function quoteText(text, box, doc = "") {
    const from = doc ? `> (from notes/docs/${doc})\n` : "";     // a quote from a document names it
    const quoted = from + text.trim().split(/\r?\n/).map(l => `> ${l}`.trimEnd()).join("\n") + "\n\n";
    const before = box.value.slice(0, box.selectionStart);
    const after = box.value.slice(box.selectionEnd);
    const lead = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    box.value = before + lead + quoted + after;
    const at = (before + lead + quoted).length;
    window.getSelection().removeAllRanges();
    box.focus();
    box.setSelectionRange(at, at);
    box.dispatchEvent(new Event("input", { bubbles: true }));   // a quoted reply is a draft too
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-reply-message]");
    const box = document.querySelector("form.composer textarea");
    if (b && box) quoteText(b.closest("[data-raw]").dataset.raw, box);
  });
  // Retry fills the composer with the message that failed. It does not send: sending would run the request again.
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-retry]");
    if (!b) return;
    const box = document.querySelector("form.composer textarea");
    if (!box) return;
    const text = b.dataset.retry;
    if (!text) return;
    if (box.value && box.value !== text && !confirm("Replace your draft with the failed message?")) return;
    if (box.value !== text) {                  // a second click must not append a copy
      box.value = text;
      box.dispatchEvent(new Event("input", { bubbles: true }));   // a draft, and a new message id
    }
    box.focus();
    box.setSelectionRange(text.length, text.length);
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
  // Seen: a reply, or a message it sent you (data-sent-at), whose start is inside the feed's visible box. Rendered was
  // not enough: a reply below the fold was marked seen, and a message it sent never was (W8, review 2026-10-24).
  function onScreen(n) {
    if (!n.getClientRects().length) return false;   // not rendered (a hidden panel)
    const r = n.getBoundingClientRect();
    const box = n.closest && n.closest("[data-scroll]");
    const b = box ? box.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    return r.top < b.bottom && r.bottom > b.top;
  }
  function seenFeed() {
    if (document.visibilityState !== "visible") return;
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
  // Opening a chat: the first reply or message it sent you that this browser has not seen yet goes to the top of the
  // feed, so you read on from there (↓ New below takes you to the end). Nothing unread, or nothing seen here before
  // (a new browser, cleared storage): it opens on the latest, as it always did.
  function firstUnread(feed) {
    const seen = seenGet();
    return [...feed.querySelectorAll("[data-seen-inst]")].find(n => {
      const mark = seen[n.dataset.seenInst], at = n.dataset.seenReply || n.dataset.sentAt || "";
      return !!mark && at > mark;
    }) || null;
  }
  function openFeed(feed) {
    const box = feed.closest("[data-scroll]"), first = box && firstUnread(feed);
    if (!first) { toBottom(feed); return; }
    box.scrollTop += first.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
    feed._atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    const nb = document.querySelector("[data-newbelow]");
    if (nb) nb.hidden = feed._atBottom;
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
  let renewed = false;                       // once a page: a renewal SHAFT refuses is not asked for on every refresh
  async function pushState() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return ios() && !standalone() ? "home" : "none";
    }
    if (Notification.permission === "denied") return "blocked";
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) return "off";
    try {                                    // a later visit: SHAFT may have dropped this device since
      const r = await fetch("/push/status", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) return "incomplete";
      const held = await r.json();
      // W5 (review 2026-10-24): SHAFT dropped it (a push service refused it), holds another endpoint (the browser
      // rotated it), or it was made for another key: subscribed again, with the key pushes are signed with now.
      const theirs = sub.options && sub.options.applicationServerKey;
      const keyChanged = held.stale || (!!held.key && !!theirs && b64url(theirs) !== held.key);
      let off = false;                       // turned off here: a browser subscription left behind is not renewed
      try { off = !!localStorage.getItem("shaft.push.off"); } catch (e) { /* no storage */ }
      if (Notification.permission === "granted" && !renewed && !off && held.key
          && (!held.subscribed || held.endpoint !== sub.endpoint || keyChanged)) {
        renewed = true;
        if (keyChanged) {
          await sub.unsubscribe();
          sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(held.key) });
        }
        const again = await fetch("/push/subscribe", { method: "POST", credentials: "same-origin",
          body: JSON.stringify(sub), headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/json" } });
        return again.ok ? "on" : "incomplete";
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
          try { localStorage.setItem("shaft.push.off", "1"); } catch (e) { /* no storage */ }
          sub = await reg.pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
          sub = null;
        }
      } else {
        if (await Notification.requestPermission() !== "granted") { notifyButton(); return; }
        try { localStorage.removeItem("shaft.push.off"); } catch (e) { /* no storage */ }
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
  // Hidden (the phone locked, the app went to the background, another tab): SHAFT is told at once, so a reply that ends
  // in the next seconds is pushed to this device and not taken as seen on screen here (W9, review 2026-10-24).
  function away() {
    const feed = document.querySelector(".feed[data-follow][data-poll]");
    if (!feed || document.visibilityState !== "hidden" || /[?&]id=/.test(feed.dataset.poll)) return;
    const url = feed.dataset.poll + (feed.dataset.poll.includes("?") ? "&" : "?") + "away=1";
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
  function refreshDocs() {
    const panel = document.getElementById("docs");
    if (!panel) return;
    delete panel.dataset.etag;
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
  const sheetOpeners = { docs: "[data-show-docs]", nav: ".chathead [data-toggle-nav]" };
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
    const grid = document.querySelector(".chatgrid");
    if (!grid) return;
    const nav = document.querySelector("nav"), docs = document.getElementById("docs");
    const fixed = el => el && el.getClientRects().length && getComputedStyle(el).position === "fixed";
    // Read the actual layout: Documents also becomes a sheet at the chat-area container breakpoint.
    const kind = document.body.classList.contains("navopen") && fixed(nav) ? "nav" : fixed(docs) ? "docs" : null;
    const panel = kind === "nav" ? nav : docs;
    if (sheet && (sheet.kind !== kind || sheet.panel !== panel)) {
      const old = sheet;
      old.inert.forEach(el => el.removeAttribute("inert"));
      old.attrs.forEach(([key, value]) => value === null ? old.panel.removeAttribute(key) : old.panel.setAttribute(key, value));
      sheet = null;
      const back = old.opener && old.opener.isConnected ? old.opener : document.querySelector(sheetOpeners[old.kind]);
      if (back && back.getClientRects().length) back.focus();
      else if (old.panel.getClientRects().length) {
        const first = sheetFocusables(old.panel)[0];
        if (first) first.focus();                   // the phone opener is hidden beside the desktop rail
      }
    }
    if (kind && !sheet) {
      sheet = { kind, panel, opener: opener || document.querySelector(sheetOpeners[kind]), inert: [],
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
    const expanded = grid.classList.contains("docfullscreen");
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
    const root = document.documentElement.style;
    root.setProperty("--vvh", `${Math.round(vv.height)}px`);
    // What the keyboard leaves visible, for the sheets fixed over the page (the documents, a comment): their bottom —
    // Save, Cancel — was under the keyboard (W3, review 2026-10-24). iOS may also pan the visible part (offsetTop).
    root.setProperty("--vvtop", `${Math.max(0, Math.round(vv.offsetTop))}px`);
    root.setProperty("--vvbottom", `${Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop))}px`);
    const box = document.querySelector("form.composer textarea");
    if (box) grow(box);
    document.body.classList.toggle("kbd", keyboardUp());
    // iOS scrolls the whole page to show a focused field; the page is already the visible height: put it back
    if (document.body.classList.contains("chatapp") && window.scrollY) window.scrollTo(0, 0);
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitViewport);
    window.visualViewport.addEventListener("scroll", fitViewport);
  }

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
