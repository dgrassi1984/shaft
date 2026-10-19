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

  function toBottom(el) {
    const box = el.closest("[data-scroll]");
    if (box) box.scrollTop = box.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  }

  function poll(el) {
    const url = el.dataset.poll;
    const every = Number(el.dataset.every || 2000);
    let running = false;
    async function tick() {
      if (running || !el.isConnected) return;
      if (document.visibilityState !== "visible" && !el.dataset.hidden) return;   // data-hidden: keeps going (the rail)
      if (selectingIn(el)) return;                  // replacing it would drop the text being selected (to quote it)
      running = true;
      try {
        const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        const body = await r.text();
        if (r.ok) {
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
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
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
      if (r.ok && form.dataset.reset) { form.reset(); picked(form); }
      if (r.ok && form.dataset.after === "reload") setTimeout(() => location.reload(), 800);
      document.dispatchEvent(new Event("shaft:refresh"));      // show what the action changed, now
    } catch (e) {
      toast(`Not sent: ${e.message}`, true);
    } finally {
      if (button) button.disabled = false;
    }
  });

  // A composer (form[data-files]): files picked, pasted or dropped go with the message; Enter sends.
  function picked(form) {
    const input = form.querySelector("input[type=file]");
    const label = form.querySelector("[data-picked]");
    if (input && label) label.textContent = [...input.files].map(f => f.name).join(", ");
  }

  function addFiles(form, files) {
    const input = form.querySelector("input[type=file]");
    if (!input || !files.length) return;
    const all = new DataTransfer();
    for (const f of input.files) all.items.add(f);
    for (const f of files) all.items.add(f);
    input.files = all.files;
    picked(form);
  }

  document.addEventListener("change", (ev) => {
    const form = ev.target.closest("form[data-files]");
    if (form && ev.target.type === "file") picked(form);
  });
  document.addEventListener("paste", (ev) => {
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    const files = [...(ev.clipboardData && ev.clipboardData.files || [])];
    if (form && files.length) { ev.preventDefault(); addFiles(form, files); }
  });
  document.addEventListener("dragover", (ev) => {
    if (ev.target.closest && ev.target.closest("form[data-files]")) ev.preventDefault();
  });
  document.addEventListener("drop", (ev) => {
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    if (form && ev.dataTransfer && ev.dataTransfer.files.length) { ev.preventDefault(); addFiles(form, [...ev.dataTransfer.files]); }
  });
  document.addEventListener("keydown", (ev) => {
    const form = ev.target.closest && ev.target.closest("form[data-files]");
    if (form && ev.target.tagName === "TEXTAREA" && ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      form.requestSubmit();
    }
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
    const feed = host && host.closest(".feed");
    const composer = document.querySelector("form.composer textarea");
    if (!feed || !composer || !s.toString().trim()) return null;
    return { text: s.toString(), range, composer };
  }

  function placeQuoteButton() {
    const b = quoteButton();
    const q = quotable();
    if (!q) { b.hidden = true; return; }
    const r = q.range.getBoundingClientRect();
    b.hidden = false;
    // below the selection: a phone shows its own copy/paste menu above it
    b.style.top = `${window.scrollY + r.bottom + 6}px`;
    b.style.left = `${Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - b.offsetWidth - 8))}px`;
  }

  function quoteSelection() {
    const q = quotable();
    quoteButton().hidden = true;
    if (!q) return;
    const quoted = q.text.trim().split(/\r?\n/).map(l => `> ${l}`.trimEnd()).join("\n") + "\n\n";
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
    document.title = document.title.replace(/^\(\d+\) /, "");
    if (unread) document.title = `(${unread}) ${document.title}`;
  }
  document.addEventListener("shaft:swapped", (ev) => {
    if (ev.target.classList && ev.target.classList.contains("rail")) markRail(ev.target);
  });
  document.addEventListener("visibilitychange", () => {
    const rail = document.querySelector("nav .rail");
    if (rail) markRail(rail);                          // back on the tab: the chat on screen is seen
  });

  function start() {
    document.querySelectorAll("[data-poll]").forEach(poll);
    document.querySelectorAll("[data-follow]").forEach(toBottom);   // a chat opens on its latest message
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
