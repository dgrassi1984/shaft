// SHAFT web UI behaviour (PLAN-webui.md). No inline script anywhere (the CSP forbids it): pages declare what they
// need with data- attributes, and this file binds it once.
//   data-poll="/fragment/…"  the element's content is refreshed from that URL every 2 s while the tab is visible
//   <form data-post="/…">    sent with fetch as a POST carrying the CSRF token; data-confirm asks first
// Listeners are delegated from the document, so content swapped in by a poll needs no re-binding.
(() => {
  "use strict";
  if (window.__shaftWeb) return;          // loaded once per page, whatever happens
  window.__shaftWeb = true;

  const csrf = () => (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function textOf(html) {
    return new DOMParser().parseFromString(html, "text/html").body.textContent.trim().slice(0, 300);
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

  function poll(el) {
    const url = el.dataset.poll;
    const every = Number(el.dataset.every || 2000);
    let running = false;
    async function tick() {
      if (running || document.visibilityState !== "visible" || !el.isConnected) return;
      running = true;
      try {
        const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        const body = await r.text();
        if (r.ok) {
          el.innerHTML = body;
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
    document.addEventListener("visibilitychange", tick);
  }

  document.addEventListener("submit", async (ev) => {
    const form = ev.target.closest("form[data-post]");
    if (!form) return;
    ev.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      const r = await fetch(form.dataset.post, {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(new FormData(form)),
      });
      const body = await r.text();
      toast(textOf(body) || (r.ok ? "done" : `HTTP ${r.status}`), !r.ok);
      if (r.ok && form.dataset.after === "reload") setTimeout(() => location.reload(), 800);
    } catch (e) {
      toast(`Not sent: ${e.message}`, true);
    } finally {
      if (button) button.disabled = false;
    }
  });

  function start() {
    document.querySelectorAll("[data-poll]").forEach(poll);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
