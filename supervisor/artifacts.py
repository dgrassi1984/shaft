"""An instance's artifacts: interactive HTML pages it writes under notes/artifacts/<slug>/ and keeps up to date, shown
to the operator in the web UI and the apps (docs/web-and-api.md, "Artifacts"). Stdlib only.

An artifact is a folder: index.html (the page: layout, script, style), an optional data.json (what it shows), and
optional .js and .css files beside them. No supervisor tool writes them: the instance writes them with write_file, and
a scheduled plugin job can keep data.json current without a cycle (`"publishes": {"notes/artifacts/<slug>/data.json":
"data.json"}`, supervisor/jobs.py).

The page is untrusted code: an instance wrote it, perhaps from what it read on the web. So it never runs with
anything of the operator's. `assemble` makes one self-contained document of it — each `<script src="x.js">` and
`<link rel="stylesheet" href="x.css">` naming a file beside it inlined, data.json inlined as `window.SHAFT_DATA` — and
puts a Content-Security-Policy first in its <head> that lets it load nothing: no network, no forms, no frames, data:
and blob: images only. The web UI serves it with the same policy as a header plus `sandbox allow-scripts`, in a
sandboxed frame: its origin is opaque, so it reaches no cookie, no storage and no page of the web UI. The apps show it
in a web view with no persistent storage, nothing it may load, no handler to call and a null origin (the HTML is
loaded as a string, never from the server's address with the app's bearer). A page that names a file it does not have,
or anything on the network, is told so in `problems`: a dashboard that silently loses its chart library is a bug.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat as stat_mod
from pathlib import Path

from .core import TooBig, UnsafePath, instance_root, open_beneath, read_beneath

DIR = "notes/artifacts"
PAGE = "index.html"
DATA = "data.json"
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")                     # one folder name: what the URL carries
ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.(js|css)$")    # a file beside index.html it may name
MAX_PAGE = 1024 * 1024                    # index.html
MAX_DATA = 1024 * 1024                    # data.json (a job publishes at most jobs.MAX_FILE_BYTES of it)
MAX_ASSET = 2 * 1024 * 1024               # each .js or .css (a chart library minified is ~200 KB)
MAX_TOTAL = 4 * 1024 * 1024               # the assembled document
MAX_ASSETS = 16
MAX_ARTIFACTS = 100                       # listed, the last changed first
TITLE_MAX = 120

# The policy the document carries (in a <meta>, the apps' only way to give it one) and the web UI's header adds to.
POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
          "font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
          "object-src 'none'; frame-src 'none'; worker-src 'none'; manifest-src 'none'")
# The header: `sandbox` (an opaque origin even when opened on its own) and who may frame it, which a <meta> cannot say.
HEADER_POLICY = f"sandbox allow-scripts; frame-ancestors 'self'; {POLICY}"

_SCRIPT_SRC = (r"<script\b(?P<attrs>[^>]*?)\bsrc\s*=\s*(?P<q>[\"']?)(?P<src>[^\"'\s>]+)(?P=q)(?P<rest>[^>]*)>"
               r"\s*</script\s*>")
_STYLE_LINK = r"<link\b(?P<lattrs>[^>]*)>"
# One pass over the page as written: an inlined file's text is never read again as HTML
_NAMED = re.compile(f"{_SCRIPT_SRC}|{_STYLE_LINK}", re.I)
_MEDIA = re.compile(r"\bmedia\s*=\s*(?P<q>[\"'])(?P<media>[^\"']*)(?P=q)", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ELEMENT_SRC = re.compile(r"<(?:img|audio|video|source|track|iframe|embed|input|object)\b[^>]*?\b(?:src|data)\s*=\s*"
                          r"[\"']?(?P<src>[^\"'\s>]+)", re.I)
_CSS_URL = re.compile(r"url\(\s*[\"']?(?P<src>[^\"')\s]+)", re.I)
_HREF = re.compile(r"\bhref\s*=\s*(?P<q>[\"']?)(?P<href>[^\"'\s>]+)(?P=q)", re.I)
_STYLESHEET = re.compile(r"\brel\s*=\s*[\"']?stylesheet\b", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_DOCTYPE = re.compile(r"(?:\ufeff|\s)*<!doctype[^>]*>", re.I)


class Refused(Exception):
    pass


def slug(value: str) -> str:
    value = str(value or "").strip()
    if not SLUG.match(value):
        raise Refused(f"{value!r}: an artifact is a folder under {DIR}/ named with lowercase letters, digits, - and _ "
                      "(at most 64)")
    return value


def _dir_fd(name: str, s: str) -> int:
    """A descriptor for notes/artifacts/<s>/, no link followed on the way (core.open_beneath)."""
    return open_beneath(instance_root(name), Path(DIR, s).parts, os.O_RDONLY | os.O_DIRECTORY)


def _files(fd: int) -> dict[str, os.stat_result]:
    """The plain files in an artifact's folder (no link, no folder, no hidden file): name -> its stat."""
    out = {}
    with os.scandir(fd) as scan:
        for e in scan:
            if e.name.startswith(".") or e.is_symlink():
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat_mod.S_ISREG(st.st_mode):
                out[e.name] = st
    return out


def _read(name: str, s: str, file: str, limit: int) -> bytes:
    return read_beneath(instance_root(name), Path(DIR, s, file).parts, limit)


def _kb(n: int) -> str:
    return f"{-(-n // 1024)} KB"


_cache: dict[tuple[str, str], tuple[tuple, dict]] = {}     # (instance, slug) -> (its files' stats, its summary)


def _stamp(files: dict[str, os.stat_result]) -> tuple:
    # ctime too: any write moves it, and no one can set it back (a same-size edit with its mtime restored)
    return tuple(sorted((n, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size) for n, st in files.items()))


def summary(name: str, s: str) -> dict | None:
    """One artifact: slug, title, mtime (its newest file), bytes, version (changes whenever anything it shows does),
    problems; None when there is no such artifact (no index.html). Assembled again only when a file changed."""
    s = slug(s)
    try:
        fd = _dir_fd(name, s)
    except (UnsafePath, OSError):
        return None
    try:
        files = _files(fd)
    finally:
        os.close(fd)
    if PAGE not in files:
        return None
    stamp = _stamp(files)
    hit = _cache.get((name, s))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        doc, title, version, problems = _assemble(name, s, files)
        size = len(doc)
    except Refused as e:
        title, version, problems, size = s, "refused-" + hashlib.sha256(str(stamp).encode()).hexdigest()[:16], [str(e)], 0
    except Exception as e:                      # noqa: BLE001 — one artifact's fault never empties the whole list
        title, version, problems, size = (s, "failed-" + hashlib.sha256(str(stamp).encode()).hexdigest()[:16],
                                          [f"cannot be shown: {type(e).__name__}: {e}"], 0)
    out = {"slug": s, "title": title, "mtime": max(st.st_mtime for st in files.values()), "bytes": size,
           "version": version, "problems": problems}
    _cache[(name, s)] = (stamp, out)
    return out


def listing(name: str) -> list[dict]:
    """Every artifact, the last changed first (at most MAX_ARTIFACTS folders, the last changed, are read). No folder yet
    is no artifact; one that cannot be read raises (an empty list would read as "none yet")."""
    try:
        fd = open_beneath(instance_root(name), Path(DIR).parts, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return []
    except UnsafePath as e:
        if "No such file" in str(e):
            return []
        raise
    try:
        with os.scandir(fd) as scan:
            dirs = [(e.stat(follow_symlinks=False).st_mtime, e.name) for e in scan
                    if SLUG.match(e.name) and not e.is_symlink() and e.is_dir(follow_symlinks=False)]
    finally:
        os.close(fd)
    slugs = [x for _, x in sorted(dirs, reverse=True)[:MAX_ARTIFACTS]]
    out = [a for x in slugs if (a := summary(name, x))]
    found = {(name, a["slug"]) for a in out}
    for key in [k for k in _cache if k[0] == name and k not in found]:
        _cache.pop(key, None)
    return sorted(out, key=lambda a: a["mtime"], reverse=True)[:MAX_ARTIFACTS]


def page(name: str, s: str) -> dict:
    """The artifact as one document: {slug, title, version, problems, html}. Refused when there is none or it cannot
    be shown (too big)."""
    s = slug(s)
    try:
        fd = _dir_fd(name, s)
    except (UnsafePath, OSError):
        raise Refused(f"{DIR}/{s}/ is not there (any more)") from None
    try:
        files = _files(fd)
    finally:
        os.close(fd)
    if PAGE not in files:
        raise Refused(f"{DIR}/{s}/ has no {PAGE}")
    doc, title, version, problems = _assemble(name, s, files)
    return {"slug": s, "title": title, "version": version, "problems": problems, "html": doc.decode()}


def _inline_text(text: str, closing: str) -> str:
    """A file's text safe inside <script> or <style>: it cannot close the element early."""
    return re.sub(rf"</({closing})", r"<\\/\1", text, flags=re.I)


def _assemble(name: str, s: str, files: dict[str, os.stat_result]) -> tuple[bytes, str, str, list[str]]:
    """(the document, its title, its version, problems). Refused when index.html or the result is too big."""
    problems: list[str] = []
    try:
        raw = _read(name, s, PAGE, MAX_PAGE)
    except TooBig:
        raise Refused(f"{DIR}/{s}/{PAGE} is {_kb(files[PAGE].st_size)}: at most {_kb(MAX_PAGE)}") from None
    except (UnsafePath, OSError) as e:
        raise Refused(f"{DIR}/{s}/{PAGE} cannot be read ({e})") from None
    text = raw.decode("utf-8", errors="replace")
    digest = hashlib.sha256(raw)
    inlined: set[str] = set()
    size = [len(raw)]                             # what the page will be, kept under MAX_TOTAL as it is built

    def asset(file: str, kind: str) -> str | None:
        if not ASSET.match(file) or not file.endswith("." + kind):
            problems.append(f"{file}: a page loads nothing from the network or from other folders; put the {kind} "
                            f"file beside {PAGE} (a plain name ending .{kind}) and name it as it is called there")
            return None
        if file not in files:
            problems.append(f"{file} is named in {PAGE} but is not in {DIR}/{s}/")
            return None
        if file in inlined:
            problems.append(f"{file} is named more than once in {PAGE}: it is put in the page once, where first named")
            return ""
        if len(inlined) >= MAX_ASSETS:
            problems.append(f"{file}: at most {MAX_ASSETS} .js and .css files are put in a page")
            return None
        try:
            data = _read(name, s, file, MAX_ASSET)
        except TooBig:
            problems.append(f"{file} is {_kb(files[file].st_size)}: at most {_kb(MAX_ASSET)}")
            return None
        except (UnsafePath, OSError) as e:
            problems.append(f"{file} cannot be read ({e})")
            return None
        size[0] += len(data)
        if size[0] > MAX_TOTAL:
            raise Refused(f"{DIR}/{s}/ makes a page of more than {_kb(MAX_TOTAL)} with its files")
        inlined.add(file)
        digest.update(b"\0" + file.encode() + b"\0" + data)
        return data.decode("utf-8", errors="replace")

    def named(m: re.Match) -> str:
        if m["src"] is not None:                  # <script src="x.js"></script>
            attrs = (m["attrs"] + " " + m["rest"]).strip()
            if re.search(r"\b(?:async|defer)\b", attrs, re.I):
                problems.append(f"{m['src']} is marked async or defer: put in the page, it runs where its tag stands "
                                "(put the tag at the end of <body> to run it after the page)")
                attrs = re.sub(r"\b(?:async|defer)\b", "", attrs, flags=re.I).strip()
            body = asset(m["src"], "js")
            if body is None:
                return m[0]
            return f"<script{' ' + attrs if attrs else ''}>{_inline_text(body, 'script')}</script>" if body else ""
        if not _STYLESHEET.search(m["lattrs"]) or not (href := _HREF.search(m["lattrs"])):
            return m[0]
        body = asset(href["href"], "css")
        if body is None:
            return m[0]
        media = _MEDIA.search(m["lattrs"])
        return (f"<style{f' media={json.dumps(media[2])}' if media else ''}>{_inline_text(body, 'style')}</style>"
                if body else "")

    own = _COMMENT.sub("", _NAMED.sub("", text))      # the page's own words: what it names is said below
    found = _TITLE.search(own)
    title = html.unescape(re.sub(r"\s+", " ", found[1])).strip()[:TITLE_MAX] if found else ""
    elsewhere = sorted({m["src"] for rx in (_ELEMENT_SRC, _CSS_URL) for m in rx.finditer(own)
                        if not m["src"].lower().startswith(("data:", "blob:", "#"))})
    text = _NAMED.sub(named, text)
    if elsewhere:
        problems.append(f"{PAGE} loads {', '.join(elsewhere[:5])}{' …' if len(elsewhere) > 5 else ''}: a page loads "
                        "nothing from the network or from files (draw images as SVG or data: URLs; put scripts and "
                        "styles beside it)")

    data_js = "null"
    if DATA in files:
        try:
            data = _read(name, s, DATA, MAX_DATA)
            digest.update(b"\0" + DATA.encode() + b"\0" + data)
            # ASCII: any string JSON allows (a lone surrogate too) is written as an escape, never as bytes
            data_js = json.dumps(json.loads(data), ensure_ascii=True, separators=(",", ":"))
        except TooBig:
            problems.append(f"{DATA} is {_kb(files[DATA].st_size)}: at most {_kb(MAX_DATA)}")
        except ValueError as e:
            problems.append(f"{DATA} is not valid JSON: {e}")
        except (UnsafePath, OSError) as e:
            problems.append(f"{DATA} cannot be read ({e})")
    version = digest.hexdigest()[:16]
    meta = {"slug": s, "version": version, "problems": problems}
    head = (f'<meta http-equiv="Content-Security-Policy" content="{POLICY}">'
            f'<script>window.SHAFT_DATA={_safe_js(data_js)};'
            f'window.SHAFT_ARTIFACT={_safe_js(json.dumps(meta, ensure_ascii=True))};</script>')
    doc = _insert_head(text, head).encode("utf-8", errors="replace")
    if len(doc) > MAX_TOTAL:
        raise Refused(f"{DIR}/{s}/ makes a page of {_kb(len(doc))} with its files: at most {_kb(MAX_TOTAL)}")
    return doc, title or s, version, problems


def _safe_js(js: str) -> str:
    """JSON inside a <script>: no "<" (so no "</script>" or "<!--") and no line separators a parser could stop at."""
    return js.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _insert_head(text: str, head: str) -> str:
    """`head` first in the document, after a doctype of the supervisor's own: a CSP <meta> governs only what follows
    it, and the parser puts a <meta> met before <html> in the <head> it opens. The page's own doctype goes, and with it
    anything before it (a no-break space there would start the body before the policy)."""
    if m := _DOCTYPE.match(text):
        text = text[m.end():]
    return "<!doctype html>" + head + text
