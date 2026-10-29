"""Markdown for the web UI (docs/plans/PLAN-webui.md §1, widened in docs/plans/PLAN-webui-chat.md): what instances write — answers, documents,
journals — rendered so that it reads as they meant it, and never runs as markup. Stdlib only.

Safety, the one rule: nothing the text holds reaches the page unescaped. The pieces that must not be escaped twice
(code spans, backslash escapes, links) are cut out first and kept as finished, escaped HTML behind placeholders;
everything else is escaped whole before any emphasis is applied; links go only to http(s) and mailto; images become
links (the page's CSP loads no image from elsewhere); alignment is a class (the CSP allows no inline style).

Blocks: ATX headings (# is the page's h2: the page has its own h1), paragraphs with hard breaks (two trailing spaces
or a backslash), bullet (-, *, + or •), numbered and task lists — nested, tight or loose —, block quotes (nested),
fenced code (``` or ~~~, with its language), tables (with alignment), horizontal rules. Inline: `code`, **bold** /
__bold__, *em* / _em_, ~~strike~~, [links](https://…), <https://…> and bare https:// addresses, \\escapes.
"""
from __future__ import annotations

import html
import re

from .web_pages import Html, clean

OPEN, CLOSE = "\ue000", "\ue001"          # placeholder brackets: private-use characters, removed from the input first
SCHEMES = ("http://", "https://", "mailto:")

_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_HEAD = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_QUOTE = re.compile(r"^ {0,3}> ?")
_ITEM = re.compile(r"^( *)([-*+•]|\d{1,9}[.)])([ \t]+|$)(.*)$")
_TASK = re.compile(r"^\[([ xX])\][ \t]+")
_TABLE_SEP = re.compile(r"^ *\|? *:?-+:? *(\| *:?-+:? *)*\|? *$")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def render(text: str) -> Html:
    """The whole text, as the page shows it."""
    src = clean(str(text)).replace(OPEN, "").replace(CLOSE, "").replace("\r\n", "\n").replace("\r", "\n")
    return Html(f'<div class="md">{_blocks(src.split(chr(10)))}</div>')


# -- inline ----------------------------------------------------------------------------------------------------------
_CODE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^\s<>]+)>")
# The destination never holds a placeholder (a code span or an autolink cut out before): it would expand inside
# href="…" and break out of the attribute (W16, review 2026-10-24). _BARE excludes them the same way.
_URL = r"[^\s()<>" + OPEN + CLOSE + r"]"
_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*<?(" + _URL + r"*(?:\(" + _URL + r"*\)" + _URL + r"*)*)>?"
                   r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_BARE = re.compile(r"(?<![\w/@=\"'])(https?://[^\s<>" + OPEN + CLOSE + r"]+)")
_STRONG = (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])"))
_EM = (re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])"),
       re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"))
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_PLACE = re.compile(OPEN + r"(\d+)" + CLOSE)


def _anchor(url: str, inner: str) -> str:
    return f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{inner}</a>'


def inline(text: str, links: bool = True, kept: list[str] | None = None) -> str:
    """`kept`: the pieces already cut out, when this is a link's label inside inline(): a code span in the label is
    a placeholder of the outer call's (a list of its own raised IndexError: the whole page failed)."""
    kept = [] if kept is None else kept

    def keep(fragment: str) -> str:
        kept.append(fragment)
        return f"{OPEN}{len(kept) - 1}{CLOSE}"

    s = _CODE.sub(lambda m: keep(f"<code>{esc(_code_text(m[2]))}</code>"), text)
    s = _ESCAPE.sub(lambda m: keep(esc(m[1])), s)
    if links:
        s = _AUTOLINK.sub(lambda m: keep(_anchor(m[1], esc(m[1]))), s)

        def link(m: re.Match) -> str:
            image, label, url = m[1], m[2], m[3]
            shown = inline(label, links=False, kept=kept) if label.strip() else esc(url)
            if image:
                shown = "🖼 " + (inline(label, links=False, kept=kept) if label.strip() else "image")
            if not url.lower().startswith(SCHEMES):        # javascript:, data:, a path: the text only, never a link
                return keep(shown)
            return keep(_anchor(url, shown))
        s = _LINK.sub(link, s)

        def bare(m: re.Match) -> str:
            url = m[1]
            tail = ""
            while url and (url[-1] in ".,;:!?'\"" or (url[-1] == ")" and url.count("(") < url.count(")"))):
                url, tail = url[:-1], url[-1] + tail
            return keep(_anchor(url, esc(url))) + tail
        s = _BARE.sub(bare, s)
    s = esc(s)
    for rx in _STRONG:
        s = rx.sub(r"<b>\1</b>", s)
    s = _STRIKE.sub(r"<s>\1</s>", s)
    for rx in _EM:
        s = rx.sub(r"<i>\1</i>", s)
    while _PLACE.search(s):                               # a kept piece may hold another (a link's label)
        s = _PLACE.sub(lambda m: kept[int(m[1])], s)
    return s


def plain(text: str) -> str:
    """One line of Markdown as the page shows its text: what an anchor quoting the rendered document must match. The
    line's block syntax goes as the page drops it (a quote's >, a heading's #, a list marker; a task box is its ☐ or
    ☑; a table row's cells are tab-separated, as comments.js walks a table), then its inline Markdown."""
    line = clean(str(text)).replace(OPEN, "").replace(CLOSE, "").strip()
    while _QUOTE.match(line):
        line = _QUOTE.sub("", line, count=1).strip()
    if (m := _HEAD.match(line)) and m[2] is not None:
        line = m[2]
    elif (m := _ITEM.match(line)) and m[4].strip():
        line = m[4]
        if t := _TASK.match(line):
            line = ("☑ " if t[1] != " " else "☐ ") + line[t.end():]
    elif line.startswith("|") or line.endswith("|"):
        return "\t".join(plain(cell) for cell in _cells(line))
    return html.unescape(re.sub(r"<[^>]*>", "", inline(line)))


def first_line(text: str, quotes: bool = True) -> str:
    """The first line of a text as the page shows it (plain): a line of a list, a rail entry, a push. A fence's
    marker, a rule and a table's separator row are not text; `quotes=False` skips quoted lines too (the operator's
    quote of what they answer is not their message)."""
    for line in str(text).splitlines():
        if not line.strip() or _FENCE.match(line) or _HR.match(line) or _TABLE_SEP.match(line) and "-" in line:
            continue
        if not quotes and _QUOTE.match(line):
            continue
        if shown := plain(line).strip():
            return shown
    return ""


def _code_text(s: str) -> str:
    s = s.replace("\n", " ")
    return s[1:-1] if len(s) > 2 and s[0] == " " and s[-1] == " " and s.strip() else s


def _para(lines: list[str]) -> str:
    """Lines of one paragraph: joined by a space, or by a line break where a line ends in two spaces or a backslash."""
    out = []
    for k, line in enumerate(lines):
        last = k == len(lines) - 1
        hard = not last and (line.endswith("  ") or (line.endswith("\\") and not line.endswith("\\\\")))
        text = line.strip()
        if hard and text.endswith("\\"):
            text = text[:-1].rstrip()
        out.append(inline(text) + ("<br>" if hard else "" if last else " "))
    return "".join(out)


# -- blocks ----------------------------------------------------------------------------------------------------------
def _starts_block(line: str) -> bool:
    """A line that ends a paragraph above it without a blank line."""
    if _FENCE.match(line) or _HR.match(line) or _QUOTE.match(line):
        return True
    if (m := _HEAD.match(line)) and m[2] is not None:
        return True
    m = _ITEM.match(line)
    return bool(m and m[4].strip() and (not m[2][0].isdigit() or m[2][:-1] == "1"))


MAX_DEPTH = 16                     # quotes and lists nested deeper show as text: they never break the page


def _blocks(lines: list[str], tight: bool = False, depth: int = 0) -> str:
    if depth >= MAX_DEPTH:
        return f"<p>{esc(' '.join(l.strip() for l in lines if l.strip()))}</p>"
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if m := _FENCE.match(line):
            indent, fence, lang = len(m[1]), m[2], m[3]
            body = []
            i += 1
            while i < len(lines) and not re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$",
                                                  lines[i]):
                body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
                i += 1
            i += 1                                          # the closing fence (or the end of the text)
            label = f' data-lang="{esc(lang)}"' if lang else ""
            out.append(f'<pre class="code"{label}><code>{esc(chr(10).join(body))}</code></pre>')
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if (m := _HEAD.match(line)) and m[2] is not None:
            n = min(6, len(m[1]) + 1)                       # the page has its own h1
            out.append(f"<h{n}>{inline(m[2])}</h{n}>")
            i += 1
            continue
        if _QUOTE.match(line):
            body = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                body.append(_QUOTE.sub("", lines[i], count=1))
                i += 1
            out.append(f"<blockquote>{_blocks(body, depth=depth + 1)}</blockquote>")
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            rows = [line]
            seps = lines[i + 1]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_table(rows, seps))
            continue
        if (m := _ITEM.match(line)) and m[4].strip():
            i, rendered = _list(lines, i, depth)
            out.append(rendered)
            continue
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]) and not (
                "|" in lines[i] and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])):
            para.append(lines[i])
            i += 1
        out.append(_para(para) if tight else f"<p>{_para(para)}</p>")
    return "".join(out)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _table(rows: list[str], seps: str) -> str:
    align = []
    for c in _cells(seps):
        align.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else
                     "l" if c.startswith(":") else "")
    head = _cells(rows[0])
    cls = lambda k: f' class="al-{align[k]}"' if k < len(align) and align[k] else ""
    ths = "".join(f"<th{cls(k)}>{inline(x)}</th>" for k, x in enumerate(head))
    labels = [html.unescape(re.sub(r"<[^>]*>", "", inline(x))) for x in head]
    trs = []
    for r in rows[1:]:
        cells = _cells(r)
        cells += [""] * (len(head) - len(cells))
        trs.append("<tr>" + "".join(
            f'<td data-label="{esc(labels[k]) if k < len(head) else ""}"{cls(k)}>{inline(v)}</td>'
            for k, v in enumerate(cells[:max(len(head), 1)])) + "</tr>")
    return (f'<div class="tablewrap"><table class="md"><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody>'
            "</table></div>")


def _list(lines: list[str], i: int, depth: int = 0) -> tuple[int, str]:
    """One list from lines[i]: its items (each rendered as blocks of its own lines), until a line that is neither an
    item of the same kind at this indentation nor indented under one. (next line, html)"""
    first = _ITEM.match(lines[i])
    base = len(first[1])
    ordered = first[2][0].isdigit()
    start = int(first[2][:-1]) if ordered else 1
    items: list[list[str]] = []
    loose = False
    blank_before = False
    while i < len(lines):
        line = lines[i]
        m = _ITEM.match(line)
        if m and len(m[1]) <= base + 1 and m[2][0].isdigit() == ordered and len(m[1]) >= base - 1:
            if blank_before and items:
                loose = True
            pad = len(m[1]) + len(m[2]) + (len(m[3]) if m[4] else 1)
            items.append([" " * pad + m[4]] if m[4] else [])
            items[-1].insert(0, f"\0{pad}")                 # where this item's content starts
            blank_before = False
            i += 1
            continue
        if not line.strip():
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            nxt = lines[j] if j < len(lines) else ""
            n = _ITEM.match(nxt)
            pad = int(items[-1][0][1:]) if items else base + 2
            inside = nxt.strip() and len(nxt) - len(nxt.lstrip()) >= min(pad, base + 2)
            same = n and len(n[1]) <= base + 1 and n[2][0].isdigit() == ordered and len(n[1]) >= base - 1
            if not (inside or same):
                break
            if inside and items:
                items[-1].append("")
            blank_before = True
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if items and (indent > base or (not blank_before and not _starts_block(line) and not m)):
            items[-1].append(line)                           # indented under the item, or a lazy continuation
            if blank_before and indent > base:
                loose = True
            blank_before = False
            i += 1
            continue
        break
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    lis = []
    for item in items:
        pad = int(item[0][1:])
        body = [l[pad:] if l[:pad].strip() == "" else l.lstrip() for l in item[1:]]
        box = ""
        if body and (t := _TASK.match(body[0])):
            done = t[1] != " "
            box = f'<span class="task{" done" if done else ""}" aria-label="{"done" if done else "to do"}">' \
                  f'{"☑" if done else "☐"}</span> '
            body[0] = body[0][t.end():]
        lis.append(f'<li{" class=" + chr(34) + "task-item" + chr(34) if box else ""}>{box}{_blocks(body, tight=not loose, depth=depth + 1)}</li>')
    return i, f"<{tag}{attr}>{''.join(lis)}</{tag}>"
