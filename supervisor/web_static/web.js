// SHAFT web UI behaviour (docs/plans/PLAN-webui.md). No inline script anywhere (the CSP forbids it): pages declare what they
// need with data- attributes, and this file binds it once.
//   data-poll="/fragment/…"  the element's content is refreshed from that URL every 2 s while the tab is visible
//   <form data-post="/…">    sent with fetch as a POST carrying the CSRF token; data-confirm asks first, data-reset
//                            clears it once sent, data-diff="/preview/…" shows what would change before asking;
//                            the reply is shown as a toast and the polled parts refresh
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
    setTimeout(() => { box.textContent = text; }, 50);
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
    b.addEventListener("click", () => location.reload());
    bar.appendChild(b);
    document.body.appendChild(bar);
  }

  function toBottom(el) {
    const box = el.closest("[data-scroll]");
    if (box) box.scrollTop = box.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  }

  // What a refresh must not replace: text being selected (to quote it), a document being edited (Save's check says
  // if it changed), a field being typed in (a new document's name).
  function busyIn(el) {
    if (selectingIn(el) || el.querySelector(".editing")) return true;
    const typing = document.activeElement;
    return !!(typing && el.contains(typing) && typing.matches("input, textarea"));
  }

  // The network (docs/plans/PLAN-pwa P3). A refresh sends the version it has (If-None-Match) and an unchanged one costs a 304;
  // it gives up after 15 s; a failure backs off (up to 30 s) and is said once, at the top, not under every panel;
  // the connection coming back or the app coming to the front refreshes at once.
  const POLL_LIMIT_MS = 15000;
  const net = { down: 0 };
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
  // strips and anything the page added to it (copy buttons); a turn being sent stays until its own turn arrives.
  function morph(el, html) {
    const next = new DOMParser().parseFromString(`<!doctype html><body>${html}`, "text/html").body;
    const sending = [...el.children].filter(c => c.classList.contains("sending") && !c.dataset.done);
    const want = [...next.children];
    want.forEach((n, i) => {
      const cur = [...el.children].filter(c => !c.classList.contains("sending"))[i];
      const src = n.outerHTML;
      if (cur && cur.dataset.k && cur.dataset.k === n.dataset.k && cur._src === src) return;
      const node = document.importNode(n, true);
      node._src = src;
      if (!cur || cur.dataset.k !== n.dataset.k) {     // a new item: an answer is read out
        if (node.classList.contains("turn") && node.classList.contains("it") && !node.classList.contains("live")) {
          const who = (node.querySelector(".who") || {}).firstChild;
          const first = (node.querySelector(".md p, .md li, .md h2, .md h3, p.warn") || {}).textContent || "";
          announce(`${who ? who.textContent.trim() : "It"} replied: ${first.slice(0, 200)}`);
        }
      }
      if (cur && cur.dataset.k === n.dataset.k) cur.replaceWith(node);
      else if (cur) el.insertBefore(node, cur);
      else el.appendChild(node);
    });
    [...el.children].filter(c => !c.classList.contains("sending")).slice(want.length).forEach(c => c.remove());
    el.querySelectorAll(".sending").forEach(c => { if (!sending.includes(c)) c.remove(); });
    sending.forEach(c => el.appendChild(c));          // kept last, until the server's turn replaces it
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
      if (running || !el.isConnected) return;
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
        if (net.down) { net.down = 0; netNote(""); document.querySelectorAll(".stale").forEach(x => x.classList.remove("stale")); }
        if (r.status === 304) { fails = 0; fresh(el); return; }
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
            const shut = new Set([...el.querySelectorAll("details[data-key]:not([open])")].map(d => d.dataset.key));
            const box = el.closest("[data-scroll]");      // a feed that scrolls in its own box (the chat), else the page
            const atBottom = box && box.scrollHeight > box.clientHeight
              ? box.scrollTop + box.clientHeight >= box.scrollHeight - 40
              : window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
            if (el.dataset.keyed) morph(el, body);
            else el.innerHTML = body;
            el.dataset.last = body;
            el.querySelectorAll("details[data-key]").forEach(d => {
              if (open.has(d.dataset.key)) d.open = true;
              if (shut.has(d.dataset.key)) d.open = false;
            });
            if (restoreFocus) {
              const target = [...el.querySelectorAll(controls)].find(n => key && focusKey(n) === key)
                || el.querySelector('details[data-key] summary');
              if (target) target.focus({ preventScroll: true });
            }
            if (atBottom && el.dataset.follow) toBottom(el);
            el.dispatchEvent(new CustomEvent("shaft:swapped", { bubbles: true }));
          }
          fresh(el);
        } else {
          fails += 1;
          stale(el, `Not refreshed: HTTP ${r.status} — ${textOf(body) || "no reason given"}`);
        }
      } catch (e) {                                    // no answer at all: the network, not this panel
        fails += 1;
        net.down += 1;
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
    el._tick = tick;
    later(el.dataset.now ? 0 : every);                 // a first render without live data (the rail): fill it now
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tick(); });
    window.addEventListener("pageshow", () => tick());   // back from the cache or the app switcher
    window.addEventListener("online", () => { fails = 0; tick(); });
    document.addEventListener("shaft:refresh", () => { if (running) again = true; else later(300); });
  }


  document.addEventListener("submit", async (ev) => {
    const form = ev.target.closest("form[data-post]");
    if (!form) return;
    ev.preventDefault();
    if (form.dataset.diff) {                    // show what would change first, then ask
      const out = form.querySelector("[data-diff-out]");
      try {
        const d = await fetch(form.dataset.diff, {
          method: "POST", credentials: "same-origin",
          headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(new FormData(form)),
        });
        if (pairAgain(d)) return;
        const text = fullText(await d.text());        // the whole diff, never cut like a toast
        if (out) out.textContent = text;
        if (!d.ok) { toast(`No diff: HTTP ${d.status} — ${text}`, true); return; }
        if (text === "(no change)") { toast("Nothing to save: no change"); return; }
      } catch (e) {
        toast(`No diff: ${e.message}`, true);
        return;
      }
    }
    if (form.dataset.busy) return;                  // one at a time: a second Enter sent it twice (Codex review)
    const tooBig = filesRefused(form);
    if (tooBig) { toast(tooBig, true); return; }      // before uploading it all to hear so (a phone's video)
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    form.dataset.busy = "1";
    const box = form.querySelector("textarea[name=text]");
    const typed = box ? box.value : null;            // what this send carries
    const mid = form.querySelector("input[name=mid]");
    if (mid && !mid.value) mid.value = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const shown = form.dataset.draft ? pending(form) : null;
    const multipart = !!form.dataset.multipart;          // files go with it: the browser sets the boundary
    const row = form.closest("[data-row]");
    const ctl = new AbortController();
    const limit = setTimeout(() => ctl.abort(), multipart ? 180000 : 30000);
    try {
      const r = await fetch(form.dataset.post, {
        method: "POST", credentials: "same-origin", signal: ctl.signal,
        headers: { "X-CSRF-Token": csrf(),
                   ...(!multipart ? { "Content-Type": "application/x-www-form-urlencoded" } : {}),
                   ...(row || form.dataset.addRow ? { "X-Row": "1" } : {}) },
        body: multipart ? new FormData(form) : new URLSearchParams(new FormData(form)),
      });
      if (pairAgain(r)) { if (shown) shown.remove(); return; }
      const body = await r.text();
      toast(textOf(body) || (r.ok ? "done" : `HTTP ${r.status}`), !r.ok);
      const refusal = form.querySelector("[data-refusal]") || (form.dataset.after === "doc-deleted"
                      ? form.closest(".docview").querySelector("[data-delete-refusal]") : null);
      if (refusal) {                                  // all of it, as this server wrote it (escaped): what changed
        refusal.replaceChildren(...(r.ok ? [] : new DOMParser().parseFromString(body, "text/html").body.childNodes));
      }
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
      if (r.ok && form.dataset.after === "doc-saved") docSaved(form, r.headers.get("X-Doc"));
      if (r.ok && form.dataset.after === "doc-deleted") docsList();
      if (r.ok && form.dataset.after === "comment-saved") refreshDocs();
      if (r.ok && mid) mid.value = "";               // sent: the next message gets an id of its own
      if (r.ok && shown) shown.dataset.done = "1";   // goes when the feed brings the turn as the server saved it
      if (r.ok && form.dataset.reset) {
        const later = box && box.value !== typed ? box.value : null;   // typed while it was sending: kept
        form.reset();
        if (later !== null) box.value = later;
        picked(form);
        if (form.dataset.draft && later === null) sent(form);
        else if (box) grow(box);
        if (box && form.dataset.draft && !touchOnly()) box.focus();   // write the next one (a phone keeps its keyboard down)
      }
      if (!r.ok && shown) shown.remove();
      document.dispatchEvent(new Event("shaft:refresh"));      // show what the action changed, now
    } catch (e) {                                  // the answer was lost: it may have arrived — the id says so
      if (shown) shown.remove();
      toast(e.name === "AbortError" ? "No answer: sending it again is safe (it is not sent twice)"
        : `Not sent: ${e.message} — sending it again is safe`, true);
    } finally {
      clearTimeout(limit);
      if (button) button.disabled = false;
      delete form.dataset.busy;
    }
  });

  // A composer (form[data-files]): files picked, pasted or dropped — anywhere on its page — go with the message, each
  // shown as a chip (a thumbnail for an image) that × takes out again; Enter sends. Its draft is kept per instance in
  // this browser (data-draft), so a reload or a closed tab does not lose it; the server's chat is the record.
  const composer = () => document.querySelector("form[data-files]");

  function touchOnly() {                         // a phone or tablet with no keyboard and no mouse
    return window.matchMedia("(hover: none) and (pointer: coarse)").matches;
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
    input.files = all.files;
    picked(form);
  }

  function addFiles(form, files) {
    const input = form.querySelector("input[type=file]");
    if (!input || !files.length) return;
    setFiles(form, [...input.files, ...files]);
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
    if (form && ev.target.type === "file") picked(form);
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
    box.style.height = `${Math.min(box.scrollHeight + 2, Math.round(window.innerHeight * part))}px`;
  }
  const draftKey = (form) => `shaft.draft.${form.dataset.draft}`;
  document.addEventListener("input", (ev) => {
    if (ev.target.closest && ev.target.closest("form.docedit")) { grow(ev.target, 0.7); return; }
    const midf = ev.target.closest && ev.target.closest("form[data-files]");
    if (midf && midf.querySelector("input[name=mid]")) midf.querySelector("input[name=mid]").value = "";  // a new text
    const form = ev.target.closest && ev.target.closest("form[data-draft]");
    if (!form || ev.target.tagName !== "TEXTAREA") return;
    grow(ev.target);
    try { localStorage.setItem(draftKey(form), ev.target.value); } catch (e) { /* no storage: no draft kept */ }
  });
  function restoreDraft() {
    const form = document.querySelector("form[data-draft]");
    if (!form) return;
    const box = form.querySelector("textarea");
    try { if (!box.value) box.value = localStorage.getItem(draftKey(form)) || ""; } catch (e) { /* none */ }
    grow(box);
  }
  function sent(form) {
    try { localStorage.removeItem(draftKey(form)); } catch (e) { /* none */ }
    grow(form.querySelector("textarea"));
  }

  // What you send shows in the feed at once, marked "sending", until the feed's next refresh replaces it with the
  // chat as the server saved it; if it fails, it goes and your text and files stay in the composer.
  function pending(form) {
    const feed = document.querySelector(".feed[data-follow]");
    const text = (form.querySelector("textarea") || {}).value || "";
    const files = [...(form.querySelector("input[type=file]") || { files: [] }).files].map(f => f.name);
    if (!feed || (!text.trim() && !files.length)) return null;
    const turn = document.createElement("div");
    turn.className = "turn me sending";
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

  document.addEventListener("keydown", (ev) => {
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    // Enter sends on a computer (Shift-Enter: a new line). A touch-only device has no Shift: there Return is a new
    // line and the Send button sends (docs/plans/PLAN-pwa P0: on the iPhone a message could not have two lines).
    if (form && ev.target.tagName === "TEXTAREA" && ev.key === "Enter" && !ev.shiftKey && !ev.isComposing
        && !touchOnly()) {
      ev.preventDefault();
      form.requestSubmit();
      return;
    }
    // Shortcuts on a chat: / writes, Esc closes a menu, Alt-↑ / Alt-↓ go to the chat above or below in the rail.
    const typing = ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable]");
    if (ev.key === "Escape") {
      document.querySelectorAll("details.menu[open]").forEach(d => { d.open = false; });
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
    const pre = b.closest("pre");
    copyText(pre ? pre.querySelector("code, span.code")?.textContent ?? pre.textContent
                 : (b.closest("[data-raw]") || {}).dataset?.raw || "", b);
  });
  function decorate(root) {                    // a copy button on each code block of an answer
    root.querySelectorAll(".turn.it pre, .docview pre").forEach(pre => {
      if (pre.querySelector("[data-copy]")) return;
      const code = document.createElement("span");
      code.className = "code";
      while (pre.firstChild) code.appendChild(pre.firstChild);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "copy small icon";
      b.dataset.copy = "";
      b.title = "Copy the code";
      b.setAttribute("aria-label", "Copy the code");
      b.innerHTML = icons.copy;
      pre.append(code, b);
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
    const box = feed.closest("[data-scroll]");
    const nb = document.querySelector("[data-newbelow]");
    if (box && nb && box.scrollTop + box.clientHeight < box.scrollHeight - 40) nb.hidden = false;
  });
  document.addEventListener("scroll", (ev) => {
    const box = ev.target;
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
  function seenFeed() {
    if (document.visibilityState !== "visible") return;
    const seen = seenGet();
    let changed = false;
    document.querySelectorAll(".feed [data-seen-reply]").forEach(reply => {
      const { seenInst: inst, seenReply: last } = reply.dataset;
      if (reply.getClientRects().length && last && (seen[inst] || "") < last) {
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
  async function pushState() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return ios() && !standalone() ? "home" : "none";
    }
    if (Notification.permission === "denied") return "blocked";
    const reg = await navigator.serviceWorker.ready;
    if (!(await reg.pushManager.getSubscription())) return "off";
    try {                                    // a later visit: SHAFT may have dropped this device since
      const r = await fetch("/push/status", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) return "incomplete";
      return (await r.json()).subscribed ? "on" : "incomplete";
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
          sub = await reg.pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
          sub = null;
        }
      } else {
        if (await Notification.requestPermission() !== "granted") { notifyButton(); return; }
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
  window.addEventListener("storage", (ev) => {
    if (ev.key === "shaft.seen" || ev.key === null) unreadState();
  });

  // The documents panel (docs/plans/PLAN-webui-chat §3): a card in a reply or a name in its list opens that document in the
  // panel, which then refreshes it as the instance writes it; the address keeps ?doc= so a reload shows the same one.
  // What the panel polls for: one document (?doc=), or the list (?list=1: without it the server opens what this chat
  // last changed, so ← Documents would bounce straight back to it).
  function docsPoll(panel, doc) {
    const poll = new URL(panel.dataset.poll, location.href);
    for (const k of ["doc", "thread", "list"]) poll.searchParams.delete(k);
    poll.searchParams.set(doc ? "doc" : "list", doc || "1");
    panel.dataset.poll = poll.pathname + poll.search;
  }
  function docsList() {
    const panel = document.getElementById("docs");
    if (!panel) return;
    const view = panel.querySelector(".docview.editing");
    if (view) editing(view, false);                 // polling resumes only after the editor lets go
    docsPoll(panel, "");
    const u = new URL(location.href);
    u.searchParams.delete("doc");
    u.searchParams.delete("thread");
    history.replaceState(null, "", u);
    refreshDocs();
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-back-docs]");
    if (!b) return;
    const view = document.querySelector("#docs .docview.editing");
    if (view && view.querySelector("form.docedit textarea").value !== view.querySelector("textarea.raw").value
        && !window.confirm("Drop your changes?")) return;
    docsList();
  });
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-open-doc]");
    const panel = document.getElementById("docs");
    if (!a || !panel || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    const doc = a.dataset.openDoc;
    docsPoll(panel, doc);
    const u = new URL(location.href);
    u.searchParams.set("doc", doc);
    history.replaceState(null, "", u);
    if (panel._tick) panel._tick();
    showDocs(true);                                 // closed, or on a phone a sheet not yet open: open it
  });
  // Editing a document: the rendered view becomes its Markdown in a box; the panel stops refreshing until Save or
  // Cancel, and Save's compare-and-set refuses if the instance wrote it meanwhile (the box keeps your text).
  function editing(view, on) {
    view.classList.toggle("editing", on);
    view.querySelector(".docmd").hidden = on;
    view.querySelector("form.docedit").hidden = !on;
    if (on) {
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

  function docSaved(form, doc) {
    const panel = document.getElementById("docs");
    const view = form.closest(".docview");
    if (view) {
      editing(view, false);
      const edit = view.querySelector("[data-edit-doc]");
      if (edit) edit.focus();                       // focus where the editor was, not lost on the page
    }
    if (panel && doc) {
      docsPoll(panel, doc);
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
  function showDocs(on) {
    const grid = document.querySelector(".chatgrid");
    if (!grid) return;
    grid.classList.toggle("showdocs", on);
    grid.classList.toggle("hidedocs", !on);
    try { if (on) localStorage.removeItem("shaft.docs.closed"); else localStorage.setItem("shaft.docs.closed", "1"); }
    catch (e) { /* no storage: it opens again on the next page */ }
    docsAria();
  }
  function chatHeaderState(el) {
    if (!el.querySelector(".chathead")) return;
    notifyButton();
    docsAria();
    const nav = el.querySelector("[data-toggle-nav]");
    if (nav) nav.setAttribute("aria-expanded", String(document.body.classList.contains("navopen")));
    const rail = document.querySelector("nav .rail");
    if (rail) markRail(rail);
  }
  document.addEventListener("shaft:swapped", (ev) => {   // an instance's first document opens the panel beside the chat
    if (ev.target.id === "docs") docsAria();
    chatHeaderState(ev.target);
  });
  document.addEventListener("click", (ev) => {           // 📄 Documents: open or close the panel
    if (!(ev.target.closest && ev.target.closest("[data-show-docs]"))) return;
    if (document.getElementById("docs")) showDocs(!docsOn());
  });
  document.addEventListener("click", (ev) => {
    const edit = ev.target.closest && ev.target.closest("[data-edit-doc]");
    const cancel = ev.target.closest && ev.target.closest("[data-cancel-edit]");
    if (edit) editing(edit.closest(".docview"), true);
    if (cancel) {
      const view = cancel.closest(".docview");
      const box = view.querySelector("form.docedit textarea");
      if (box.value !== view.querySelector("textarea.raw").value && !window.confirm("Drop your changes?")) return;
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
    document.body.classList.toggle("navopen", open);
    document.querySelectorAll("[data-toggle-nav]").forEach(b => b.setAttribute("aria-expanded", String(open)));
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    document.body.classList.remove("navopen");
    const grid = document.querySelector(".chatgrid");
    if (grid) { grid.classList.remove("showdocs"); docsAria(); }    // a phone's sheet; beside the chat, ✕ or 📄
  });
  function fitViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    document.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
    document.body.classList.toggle("kbd", vv.height < window.innerHeight - 120);   // the keyboard is up
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
    const grid = document.querySelector(".chatgrid");
    if (grid) {
      document.body.classList.add("chatapp");
      try { if (localStorage.getItem("shaft.docs.closed")) grid.classList.add("hidedocs"); } catch (e) { /* none */ }
      docsAria();
    }
    fitViewport();
    if ("serviceWorker" in navigator) {             // the app's shell: offline page and cached static files
      navigator.serviceWorker.register("/sw.js").catch(() => { /* not over https, or refused: the page works */ });
    }
    document.querySelectorAll("[data-poll]").forEach(poll);
    document.querySelectorAll("[data-follow]").forEach(el => { decorate(el); toBottom(el); });  // opens on the latest
    unreadState();
    document.querySelectorAll(".docview").forEach(decorate);
    restoreDraft();
    notifyButton();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
