// SHAFT web UI behaviour (PLAN-webui.md). No inline script anywhere (the CSP forbids it): pages declare what they
// need with data- attributes, and this file binds it once.
//   data-poll="/fragment/…"  the element's content is refreshed from that URL every 2 s while the tab is visible
//   <form data-post="/…">    sent with fetch as a POST carrying the CSRF token; data-confirm asks first, data-reset
//                            clears it once sent, data-diff="/preview/…" shows what would change before asking;
//                            the reply is shown as a toast and the polled parts refresh
// Listeners are delegated from the document, so content swapped in by a poll needs no re-binding.
(() => {
  "use strict";
  if (window.__shaftWeb) return;          // loaded once per page, whatever happens
  window.__shaftWeb = true;

  const csrf = () => (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function fullText(html) {
    return new DOMParser().parseFromString(html, "text/html").body.textContent.trim();
  }

  function textOf(html) {                    // for a toast or a note: the first 300 characters
    return fullText(html).slice(0, 300);
  }

  function toast(text, bad) {
    let box = document.getElementById("toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "toast";
      box.setAttribute("role", "status");
      document.body.appendChild(box);
    }
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

  function poll(el) {
    const every = Number(el.dataset.every || 2000);
    let running = false;
    async function tick() {
      const url = el.dataset.poll;                  // read each time: the documents panel changes what it shows
      if (running || !el.isConnected) return;
      if (document.visibilityState !== "visible" && !el.dataset.hidden) return;   // data-hidden: keeps going (the rail)
      if (busyIn(el)) return;
      running = true;
      try {
        const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        const body = await r.text();
        checkVersion(r);
        if (r.ok) {
          // Asked again now: an edit or a selection begun while this refresh was on its way must survive it (Codex)
          if (busyIn(el) || el.dataset.poll !== url) return;
          if (body !== el.dataset.last) {               // unchanged: nothing replaced, nothing collapsed
            const open = new Set([...el.querySelectorAll("details[data-key][open]")].map(d => d.dataset.key));
            const shut = new Set([...el.querySelectorAll("details[data-key]:not([open])")].map(d => d.dataset.key));
            const box = el.closest("[data-scroll]");      // a feed that scrolls in its own box (the chat), else the page
            const atBottom = box && box.scrollHeight > box.clientHeight
              ? box.scrollTop + box.clientHeight >= box.scrollHeight - 40
              : window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
            el.innerHTML = body;
            el.dataset.last = body;
            el.querySelectorAll("details[data-key]").forEach(d => {
              if (open.has(d.dataset.key)) d.open = true;
              if (shut.has(d.dataset.key)) d.open = false;
            });
            if (atBottom && el.dataset.follow) toBottom(el);
            el.dispatchEvent(new CustomEvent("shaft:swapped", { bubbles: true }));
          }
          fresh(el);
        } else {
          const why = textOf(body) ||
            (r.status >= 502 ? "the web service is not answering (shaft web status)" : "no reason given");
          stale(el, `Not refreshed: HTTP ${r.status} — ${why}`);
        }
      } catch (e) {
        stale(el, `Not refreshed: ${e.message} (is the web service running?)`);
      } finally {
        running = false;
      }
    }
    el._tick = tick;
    setInterval(tick, every);
    if (el.dataset.now) tick();                        // a first render without live data (the rail): fill it now
    document.addEventListener("visibilitychange", tick);
    document.addEventListener("shaft:refresh", () => setTimeout(tick, 300));
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
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    form.dataset.busy = "1";
    const box = form.querySelector("textarea[name=text]");
    const typed = box ? box.value : null;            // what this send carries
    const shown = form.dataset.draft ? pending(form) : null;
    try {
      const multipart = !!form.dataset.multipart;          // files go with it: the browser sets the boundary
      const r = await fetch(form.dataset.post, {
        method: "POST", credentials: "same-origin",
        headers: multipart ? { "X-CSRF-Token": csrf() }
                           : { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
        body: multipart ? new FormData(form) : new URLSearchParams(new FormData(form)),
      });
      const body = await r.text();
      toast(textOf(body) || (r.ok ? "done" : `HTTP ${r.status}`), !r.ok);
      const refusal = form.querySelector("[data-refusal]");
      if (refusal) {                                  // all of it, as this server wrote it (escaped): what changed
        refusal.replaceChildren(...(r.ok ? [] : new DOMParser().parseFromString(body, "text/html").body.childNodes));
      }
      const now = r.headers.get("X-Doc-Sha");         // refused as stale: Save again replaces it, knowingly
      if (!r.ok && now && form.querySelector("input[name=sha]")) form.querySelector("input[name=sha]").value = now;
      if (r.ok && form.dataset.after === "doc-saved") docSaved(form, r.headers.get("X-Doc"));
      if (r.ok && form.dataset.reset) {
        const later = box && box.value !== typed ? box.value : null;   // typed while it was sending: kept
        form.reset();
        if (later !== null) box.value = later;
        picked(form);
        if (form.dataset.draft && later === null) sent(form);
        else if (box) grow(box);
      }
      if (!r.ok && shown) shown.remove();
      if (r.ok && form.dataset.after === "reload") setTimeout(() => location.reload(), 800);
      document.dispatchEvent(new Event("shaft:refresh"));      // show what the action changed, now
    } catch (e) {
      if (shown) shown.remove();
      toast(`Not sent: ${e.message}`, true);
    } finally {
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
      x.textContent = "×";
      x.dataset.unpick = String(k);
      chip.appendChild(x);
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
    // line and the Send button sends (PLAN-pwa P0: on the iPhone a message could not have two lines).
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
    const was = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = was; }, 1200);
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest("[data-copy]");
    if (!b) return;
    const pre = b.closest("pre");
    copyText(pre ? pre.querySelector("code, span.code")?.textContent ?? pre.textContent.replace(/Copy(ed)?$/, "")
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
      b.className = "copy small";
      b.dataset.copy = "";
      b.textContent = "Copy";
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
    const from = q.doc ? `> (from notes/docs/${q.doc})\n` : "";     // a quote from a document names it
    const quoted = from + q.text.trim().split(/\r?\n/).map(l => `> ${l}`.trimEnd()).join("\n") + "\n\n";
    const box = q.composer;
    const before = box.value.slice(0, box.selectionStart);
    const after = box.value.slice(box.selectionEnd);
    const lead = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    box.value = before + lead + quoted + after;
    const at = (before + lead + quoted).length;
    window.getSelection().removeAllRanges();
    box.focus();
    box.setSelectionRange(at, at);
  }

  let quoteTimer = null;
  document.addEventListener("selectionchange", () => {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(placeQuoteButton, 120);    // after the selection settles (a drag, a phone's handles)
  });

  // The rail (PLAN-webui-chat §1): an instance whose last answer this browser has not seen is marked unread. What was
  // seen is kept in this browser only (a convenience: losing it marks a chat unread, nothing more).
  function seenGet() {
    try { return JSON.parse(localStorage.getItem("shaft.seen") || "{}"); } catch (e) { return {}; }
  }
  function seenSet(v) {
    try { localStorage.setItem("shaft.seen", JSON.stringify(v)); } catch (e) { /* private window: no marks kept */ }
  }
  function markRail(rail) {
    const seen = seenGet();
    let changed = false;
    rail.querySelectorAll("a[data-inst]").forEach(a => {
      const last = a.dataset.last || "";
      if (a.classList.contains("on") && document.visibilityState === "visible") {
        if (last && seen[a.dataset.inst] !== last) { seen[a.dataset.inst] = last; changed = true; }
      }
      // the chat on screen is unread too while this tab is hidden: the title says a reply landed
      const away = !a.classList.contains("on") || document.visibilityState !== "visible";
      a.classList.toggle("unread", !!last && away && (seen[a.dataset.inst] || "") < last);
    });
    if (changed) seenSet(seen);
    const unread = rail.querySelectorAll("a.unread").length;
    if (navigator.setAppBadge) {                    // the Home Screen icon's count: an app shows no tab title
      (unread ? navigator.setAppBadge(unread) : navigator.clearAppBadge()).catch(() => {});
    }
    document.title = document.title.replace(/^\(\d+\) /, "");
    if (unread) document.title = `(${unread}) ${document.title}`;
  }
  // Desktop notifications, only after you ask for them (the ⋯ menu): a reply that lands while this tab is hidden.
  const notified = {};
  function notifyButton() {
    const b = document.querySelector("[data-notify]");
    if (!b) return;
    if (!("Notification" in window)) { b.hidden = true; return; }
    b.textContent = Notification.permission === "granted" ? "Notifications on (browser settings turn them off)"
      : Notification.permission === "denied" ? "Notifications blocked in this browser" : "Notify me of replies";
    b.disabled = Notification.permission !== "default";
  }
  document.addEventListener("click", async (ev) => {
    if (!(ev.target.closest && ev.target.closest("[data-notify]"))) return;
    try { await Notification.requestPermission(); } catch (e) { /* refused */ }
    notifyButton();
  });
  function notifyUnread(rail) {
    if (!("Notification" in window) || Notification.permission !== "granted" || document.visibilityState === "visible") {
      return;
    }
    rail.querySelectorAll("a.unread[data-inst]").forEach(a => {
      const key = `${a.dataset.inst}@${a.dataset.last}`;
      if (notified[key]) return;
      notified[key] = true;
      const last = a.querySelector(".last");
      const n = new Notification(`${a.dataset.inst} replied`, { body: last ? last.textContent : "", tag: a.dataset.inst });
      n.onclick = () => { window.focus(); location.href = a.href; };
    });
  }
  let railSeeded = false;                         // what was already unread when the page opened is not news
  document.addEventListener("shaft:swapped", (ev) => {
    if (!(ev.target.classList && ev.target.classList.contains("rail"))) return;
    markRail(ev.target);
    if (!railSeeded) {
      ev.target.querySelectorAll("a.unread[data-inst]").forEach(a => { notified[`${a.dataset.inst}@${a.dataset.last}`] = true; });
      railSeeded = true;
    } else {
      notifyUnread(ev.target);
    }
  });
  document.addEventListener("visibilitychange", () => {
    const rail = document.querySelector("nav .rail");
    if (rail) markRail(rail);                          // back on the tab: the chat on screen is seen
  });

  // The documents panel (PLAN-webui-chat §3): a card in a reply or a name in its list opens that document in the
  // panel, which then refreshes it as the instance writes it; the address keeps ?doc= so a reload shows the same one.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest("[data-open-doc]");
    const panel = document.getElementById("docs");
    if (!a || !panel || ev.metaKey || ev.ctrlKey) return;
    ev.preventDefault();
    const doc = a.dataset.openDoc;
    panel.dataset.poll = panel.dataset.poll.replace(/\?doc=.*$/, "") + `?doc=${encodeURIComponent(doc)}`;
    const u = new URL(location.href);
    u.searchParams.set("doc", doc);
    history.replaceState(null, "", u);
    if (panel._tick) panel._tick();
    if (window.innerWidth <= 720) panel.scrollIntoView({ behavior: "smooth" });
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
  function docSaved(form, doc) {
    const panel = document.getElementById("docs");
    const view = form.closest(".docview");
    if (view) editing(view, false);
    if (panel && doc) {
      panel.dataset.poll = panel.dataset.poll.replace(/\?doc=.*$/, "") + `?doc=${encodeURIComponent(doc)}`;
    }
    if (panel && panel._tick) panel._tick();
  }
  document.addEventListener("click", (ev) => {           // 📄 Documents: show the panel (it hides when there are none)
    if (!(ev.target.closest && ev.target.closest("[data-show-docs]"))) return;
    const grid = document.querySelector(".chatgrid");
    const panel = document.getElementById("docs");
    if (!grid || !panel) return;
    grid.classList.toggle("showdocs");
    if (grid.classList.contains("showdocs")) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

  function start() {
    if ("serviceWorker" in navigator) {             // the app's shell: offline page and cached static files
      navigator.serviceWorker.register("/sw.js").catch(() => { /* not over https, or refused: the page works */ });
    }
    document.querySelectorAll("[data-poll]").forEach(poll);
    document.querySelectorAll("[data-follow]").forEach(el => { decorate(el); toBottom(el); });  // opens on the latest
    document.querySelectorAll(".docview").forEach(decorate);
    restoreDraft();
    notifyButton();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
