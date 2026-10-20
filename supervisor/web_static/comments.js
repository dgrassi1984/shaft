(() => {
  "use strict";

  // Offsets are UTF-16, like DOM Range offsets. Synthetic separators have DOM boundary
  // points too; highlights split over the non-synthetic spans without changing HTML.
  function walk(root) {
    let text = "";
    const spans = [];
    const offsets = new WeakMap();
    function emit(value, node, from, to, synthetic = false) {
      const start = text.length;
      text += value;
      spans.push({ start, end: text.length, node, from, to, synthetic });
    }
    function visit(node) {
      if (node.nodeType === 3) {
        offsets.set(node, text.length);
        emit(node.data, node, 0, node.length);
        return;
      }
      // A control is the page's, not the document's: web.js puts a "Copy" button in each code block (Grok review)
      if (node.nodeType !== 1 || node.tagName === "BUTTON") return;
      const parent = node.parentNode;
      const index = parent ? Array.prototype.indexOf.call(parent.childNodes, node) : 0;
      if (/^(P|H[1-6]|LI|PRE|TR|BLOCKQUOTE|BR)$/.test(node.tagName)) {
        emit("\n", parent || node, index, index, true);
      } else if (/^(TD|TH)$/.test(node.tagName) && node.previousElementSibling &&
                 /^(TD|TH)$/.test(node.previousElementSibling.tagName)) {
        emit("\t", parent, index, index, true);
      }
      const bounds = [text.length];
      for (const child of node.childNodes) {
        visit(child);
        bounds.push(text.length);
      }
      offsets.set(node, bounds);
    }
    visit(root);
    const map = {
      spans,
      offsetAt(node, offset) {
        const bounds = offsets.get(node);
        return typeof bounds === "number" ? bounds + offset : bounds && bounds[offset];
      },
      // At a shared boundary, start prefers the next text node; end prefers the previous.
      pointAt(offset, edge = "start") {
        const real = spans.filter(s => !s.synthetic && s.end > s.start);
        const span = edge === "end"
          ? real.findLast(s => s.start < offset && offset <= s.end) || real.findLast(s => s.end <= offset) || real[0]
          : real.find(s => s.start <= offset && offset < s.end) || real.find(s => s.start >= offset) || real[real.length - 1];
        return span ? { node: span.node, offset: Math.max(0, Math.min(span.to, offset - span.start)) } : null;
      },
    };
    return { text, map };
  }
  function locate(text, anchor) {
    const exact = anchor.exact;
    if (!exact) return { detached: true };
    const hits = [];
    for (let at = text.indexOf(exact); at !== -1; at = text.indexOf(exact, at + 1)) hits.push(at);
    if (!hits.length) return { detached: true };
    if (hits.length === 1) return { start: hits[0], end: hits[0] + exact.length };
    const prefix = anchor.prefix || "", suffix = anchor.suffix || "";
    const neighbours = hits.filter(at =>
      text.slice(Math.max(0, at - prefix.length), at) === prefix &&
      text.slice(at + exact.length, at + exact.length + suffix.length) === suffix);
    if (neighbours.length === 1) {
      return { start: neighbours[0], end: neighbours[0] + exact.length };
    }
    const candidates = neighbours.length ? neighbours : hits;
    if (!Number.isFinite(anchor.start)) return { ambiguous: true };
    let best = null, distance = Infinity, tied = false;
    for (const at of candidates) {
      const delta = Math.abs(at - anchor.start);
      if (delta < distance) { best = at; distance = delta; tied = false; }
      else if (delta === distance) tied = true;
    }
    return tied ? { ambiguous: true } : { start: best, end: best + exact.length };
  }
  window.shaftComments = { walk, locate };
  // The text helpers also load in Node without a browser DOM.
  if (typeof document === "undefined") return;

  let highlighted = [];
  let lastActive = null;
  const hasHighlights = () => window.CSS && window.CSS.highlights && typeof window.Highlight === "function";
  function paintActive() {
    if (!hasHighlights()) return;
    const active = new window.Highlight();
    for (const entry of highlighted) {
      for (const match of entry.matches) {
        if (match.article && match.article.classList.contains("active")) {
          for (const range of match.ranges) active.add(range);
        }
      }
    }
    // Reinsert last so the active thread paints over the other open threads.
    window.CSS.highlights.delete("comment-active");
    window.CSS.highlights.set("comment-active", active);
  }
  function activate(article, view) {
    view.querySelectorAll("article[data-thread]").forEach(a => a.classList.toggle("active", a === article));
    const panel = view.closest("#docs");
    lastActive = { id: article.dataset.thread, doc: view.dataset.doc, poll: panel && panel.dataset.poll };
    for (let p = article.parentElement; p && p !== panel; p = p.parentElement) {
      if (p.tagName === "DETAILS") p.open = true;
    }
    article.scrollIntoView({ block: "nearest" });
    paintActive();
  }
  function rebuildHighlights() {
    if (!hasHighlights()) return;
    window.CSS.highlights.delete("comment-open");
    window.CSS.highlights.delete("comment-active");
    highlighted = [];
    const all = new window.Highlight();
    const panel = document.getElementById("docs");
    for (const view of panel ? panel.querySelectorAll(".docview[data-threads]") : []) {
      const md = view.querySelector(".docmd");
      if (!md) continue;
      const { text, map } = walk(md);
      const articles = new Map([...view.querySelectorAll("article[data-thread]")].map(a => [a.dataset.thread, a]));
      // The page opened on ?thread=c7 marks it active once: remember it, or the next refresh would drop it
      const marked = !lastActive && view.querySelector("article[data-thread].active");
      if (marked) lastActive = { id: marked.dataset.thread, doc: view.dataset.doc, poll: panel.dataset.poll };
      if (lastActive && lastActive.doc === view.dataset.doc) {
        const article = articles.get(lastActive.id);
        if (article) articles.forEach(a => a.classList.toggle("active", a === article));
      }
      const matches = [];
      highlighted.push({ md, view, map, matches });
      for (const thread of JSON.parse(view.dataset.threads)) {
        if (thread.state !== "open" || !thread.anchor) continue;
        const match = locate(text, thread.anchor);
        const article = articles.get(thread.id);
        if (article) {
          article.classList.toggle("detached", !!match.detached);
          article.classList.toggle("ambiguous", !!match.ambiguous);
          const note = article.querySelector("p[data-detached-note]");
          if (note) {
            note.hidden = !(match.detached || match.ambiguous);
            note.textContent = match.ambiguous ? "text found more than once" : "text no longer in the document";
          }
        }
        if (match.detached || match.ambiguous) continue;
        const start = map.pointAt(match.start, "start"), end = map.pointAt(match.end, "end");
        const ranges = [];
        if (start && end) {
          for (const span of map.spans) {
            if (span.synthetic || span.end <= match.start || span.start >= match.end || span.start === span.end) continue;
            const range = document.createRange();
            range.setStart(span.node, span.node === start.node ? start.offset : span.from);
            range.setEnd(span.node, span.node === end.node ? end.offset : span.to);
            ranges.push(range);
            all.add(range);
          }
        }
        matches.push({ ...match, article, ranges });
      }
    }
    window.CSS.highlights.set("comment-open", all);
    paintActive();
  }
  document.addEventListener("click", ev => {
    if (!hasHighlights() || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    const entry = highlighted.find(item => item.md.contains(ev.target));
    if (!entry) return;
    let node, offset;
    if (document.caretPositionFromPoint) {
      const point = document.caretPositionFromPoint(ev.clientX, ev.clientY);
      if (!point) return;
      node = point.offsetNode; offset = point.offset;
    } else if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(ev.clientX, ev.clientY);
      if (!range) return;
      node = range.startContainer; offset = range.startOffset;
    } else return;
    const at = entry.map.offsetAt(node, offset);
    let shortest = null;
    for (const match of entry.matches) {
      if (match.article && match.start <= at && at < match.end &&
          (!shortest || match.end - match.start < shortest.end - shortest.start)) shortest = match;
    }
    if (shortest) activate(shortest.article, entry.view);
  });

  function selection() {
    const s = window.getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return null;
    const range = s.getRangeAt(0);
    const host = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    const md = host.closest(".docmd");
    if (!md || !md.contains(range.endContainer)) return null;
    const view = md.closest(".docview");
    if (!view || view.dataset.readOnly) return null;
    return { range, md, view };
  }

  const button = document.createElement("button");
  button.id = "comment-sel";
  button.type = "button";
  button.textContent = "💬 Comment";
  button.hidden = true;
  document.body.appendChild(button);

  function place() {
    const q = selection();
    button.hidden = !q;
    if (!q) return;
    const r = q.range.getBoundingClientRect();
    const reply = document.getElementById("quote-reply");
    const other = reply && !reply.hidden ? reply : null;
    const width = button.offsetWidth + (other ? other.offsetWidth + 6 : 0);
    const height = Math.max(button.offsetHeight, other ? other.offsetHeight : 0);
    const below = r.bottom + 6 + height <= window.innerHeight - 8;
    const top = below ? r.bottom + 6 : Math.max(8, r.top - height - 6);
    const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
    button.style.top = `${top}px`;
    button.style.left = `${left}px`;
    if (other) {
      const replyTop = `${top}px`, replyLeft = `${left + button.offsetWidth + 6}px`;
      if (other.style.top !== replyTop) other.style.top = replyTop;
      if (other.style.left !== replyLeft) other.style.left = replyLeft;
    }
  }
  // Reply places itself on its own schedule. Observe that button so neither handler
  // can leave the pair overlapping, regardless of which selection callback ran last.
  new MutationObserver(records => {
    if (records.some(r => r.target.id === "quote-reply" ||
        [...r.addedNodes].some(n => n.id === "quote-reply"))) requestAnimationFrame(place);
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["style", "hidden"] });
  document.addEventListener("selectionchange", () => requestAnimationFrame(place));
  document.addEventListener("scroll", () => requestAnimationFrame(place), true);
  window.addEventListener("resize", () => requestAnimationFrame(place));

  let box = null;
  function close(keep = true) {
    if (!box) return;
    if (keep) box._saveDraft();
    const focus = box._returnFocus;
    box.remove();
    box = null;
    if (focus && focus.isConnected) focus.focus();
  }
  function instance() {
    const panel = document.getElementById("docs");
    const url = new URL(panel.dataset.poll, location.href);
    return decodeURIComponent(url.pathname.slice("/fragment/docs/".length));
  }
  function open(view, mode, value = {}) {
    close();
    const name = instance();
    const pid = "p-" + Array.from(crypto.getRandomValues(new Uint8Array(8)), n => n.toString(16).padStart(2, "0")).join("");
    const fields = mode === "edit" ? { pid: value.pid } : mode === "reply" ? { pid, thread: value.thread }
      : { pid, doc: view.dataset.doc, sha: view.dataset.sha, anchor: JSON.stringify(value.anchor || null) };
    const draftKey = `shaft.draft.comment.${JSON.stringify([name, view.dataset.doc, mode,
      mode === "edit" ? value.pid : mode === "reply" ? value.thread : value.anchor || null])}`;
    const form = document.createElement("form");
    form.id = "comment-box";
    form.setAttribute("role", "dialog");
    form.setAttribute("aria-label", "Document comment");
    form._returnFocus = document.activeElement;
    form.innerHTML = '<p class="comment-quote"></p><label>Comment<textarea name="text" rows="4" required></textarea></label>' +
      '<label class="comment-send"><input type="checkbox" name="send" checked> <span></span></label>' +
      '<p class="comment-result" role="status"></p><div class="comment-actions"><button type="submit">Save</button>' +
      '<button type="button" data-comment-cancel>Cancel</button>' +
      '<button type="button" data-comment-discard>Discard</button></div>';
    form.querySelector(".comment-quote").textContent = mode === "edit" ? "editing your note"
      : mode === "reply" ? `reply in ${value.thread}` : value.anchor ? value.anchor.exact : "the whole document";
    const textarea = form.querySelector("textarea");
    textarea.value = value.text || "";
    const send = form.querySelector(".comment-send");
    const checkbox = send.querySelector("input");
    const save = form.querySelector("button[type=submit]");
    try {
      const draft = JSON.parse(localStorage.getItem(draftKey));
      if (draft && typeof draft.text === "string" && typeof draft.send === "boolean" &&
          /^p-[0-9a-f]{16}$/.test(draft.pid)) {
        textarea.value = draft.text;
        checkbox.checked = draft.send;
        fields.pid = draft.pid;
        if (mode === "new" && typeof draft.sha === "string") fields.sha = draft.sha;
      }
    } catch (e) { /* no readable draft */ }
    function label() {
      save.textContent = mode === "edit" ? "Save note" : checkbox.checked ? "Send comment" : "Save without sending";
    }
    function draftValue() {
      return JSON.stringify({ text: textarea.value, send: checkbox.checked, pid: fields.pid, sha: fields.sha });
    }
    form._saveDraft = () => {
      try {
        if (textarea.value) localStorage.setItem(draftKey, draftValue());
        else localStorage.removeItem(draftKey);
      } catch (e) { /* no storage: no draft kept */ }
    };
    textarea.addEventListener("input", form._saveDraft);
    checkbox.addEventListener("change", () => { label(); form._saveDraft(); });
    label();
    send.querySelector("span").textContent = `Send comment to ${name}`;
    if (mode === "edit") send.remove();
    form.querySelector("[data-comment-cancel]").addEventListener("click", () => close());
    form.querySelector("[data-comment-discard]").addEventListener("click", () => {
      try { localStorage.removeItem(draftKey); } catch (e) { /* none */ }
      close(false);
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (form.dataset.busy) return;
      const save = form.querySelector("button[type=submit]");
      const result = form.querySelector(".comment-result");
      const body = new URLSearchParams({ ...fields, text: textarea.value });
      if (mode !== "edit" && send.querySelector("input").checked) body.set("send", "1");
      form._saveDraft();
      const submittedDraft = draftValue();
      form.dataset.busy = "1";
      save.disabled = true;
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 30000);
      const api = window.shaftWeb;
      try {
        const r = await fetch(`/docs/${encodeURIComponent(name)}/comment${mode === "edit" ? "/edit" : ""}`, {
          method: "POST", credentials: "same-origin", signal: ctl.signal,
          headers: { "X-CSRF-Token": api.csrf(), "Content-Type": "application/x-www-form-urlencoded" }, body,
        });
        if (api.pairAgain(r)) return;
        const message = api.textOf(await r.text()) || (r.ok ? "done" : `HTTP ${r.status}`);
        if (!r.ok) { result.textContent = message; return; }
        try {
          if (localStorage.getItem(draftKey) === submittedDraft) localStorage.removeItem(draftKey);
        } catch (e) { /* none */ }
        api.toast(message);
        if (box === form) close(false);
        api.refreshDocs();
      } catch (e) {
        result.textContent = `No answer: ${e.message}. Submit again to retry safely.`;
      } finally {
        clearTimeout(timer);
        delete form.dataset.busy;
        save.disabled = false;
      }
    });
    box = form;
    document.body.appendChild(form);
    window.getSelection().removeAllRanges();
    button.hidden = true;
    textarea.focus();
  }
  function selected() {
    const q = selection();
    if (!q || !q.view) return;
    const { text, map } = walk(q.md);
    let start = map.offsetAt(q.range.startContainer, q.range.startOffset);
    const end = map.offsetAt(q.range.endContainer, q.range.endOffset);
    if (start === undefined || end === undefined) return;
    const raw = text.slice(start, end);
    start += raw.length - raw.trimStart().length;
    const exact = raw.trim().slice(0, 2000).trimEnd();
    if (!exact) return;
    open(q.view, "new", { anchor: { exact, prefix: text.slice(Math.max(0, start - 32), start),
      suffix: text.slice(start + exact.length, start + exact.length + 32), start } });
  }
  button.addEventListener("mousedown", ev => { ev.preventDefault(); selected(); });
  button.addEventListener("touchend", ev => { ev.preventDefault(); selected(); });
  button.addEventListener("click", selected);
  document.addEventListener("keydown", ev => {
    if (ev.key === "Escape" && box) { ev.preventDefault(); ev.stopImmediatePropagation(); close(); }
  }, true);

  let pendingThread = null;
  function revealThread() {
    if (!pendingThread) return;
    const panel = document.getElementById("docs");
    const view = panel && panel.querySelector(".docview");
    if (!view || view.dataset.doc !== pendingThread.doc) return;
    const article = document.getElementById(`thread-${pendingThread.id}`);
    if (!article || !view.contains(article)) return;
    activate(article, view);
    pendingThread = null;
  }
  document.addEventListener("shaft:swapped", ev => {
    if (ev.target.id !== "docs") return;
    rebuildHighlights();
    revealThread();
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", rebuildHighlights, { once: true });
  else rebuildHighlights();
  document.addEventListener("click", ev => {
    const target = ev.target.closest && ev.target.closest("[data-comment-doc], [data-reply-thread], [data-edit-post], [data-chat-turn], [data-open-doc]");
    if (!target || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    if (target.matches("[data-open-doc]")) {
      pendingThread = target.dataset.openThread ? { doc: target.dataset.openDoc, id: target.dataset.openThread } : null;
      requestAnimationFrame(revealThread);
      return; // web.js owns the document URL and the panel's visibility.
    }
    if (target.matches("[data-chat-turn]")) {
      const match = /#(\d+)$/.exec(target.dataset.chatTurn);
      const turn = match && document.querySelector(`.feed [data-k="a${Number(match[1])}"], .feed [data-k="u${Number(match[1])}"]`);
      if (turn) {
        ev.preventDefault();
        const closeDocs = document.querySelector("[data-close-docs]");
        if (closeDocs) closeDocs.click();
        turn.scrollIntoView({ block: "center" });
      }
      return;
    }
    const view = target.closest(".docview");
    if (!view) return;
    ev.preventDefault();
    if (target.matches("[data-reply-thread]")) open(view, "reply", { thread: target.dataset.replyThread });
    else if (target.matches("[data-edit-post]")) {
      const post = [...view.querySelectorAll(".post[data-pid]")].find(p => p.dataset.pid === target.dataset.editPost);
      if (post) open(view, "edit", { pid: target.dataset.editPost, text: post.dataset.text ?? post.textContent.trim() });
    } else open(view, "new");
  });
})();
