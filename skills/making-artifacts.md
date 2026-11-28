# Making artifacts: interactive pages the operator opens beside our chat

An artifact is a page I make for the operator: a dashboard, a tracker, a chart to filter. The operator opens it in the
web UI (the instance's Artifacts tab) and in the apps (the side panel's Artifacts), and it refreshes there by itself
whenever I change it. A document (`notes/docs/*.md`) is for reading; an artifact is for looking at and using.

## Where it lives

One folder per artifact: `notes/artifacts/<name>/` (lowercase letters, digits, `-`, `_`).

- `index.html` (required): the page — layout, style, script. Its `<title>` is the name the operator sees.
- `data.json` (optional): what it shows. The page reads it as `window.SHAFT_DATA` (null when there is none).
- `*.js`, `*.css` beside them (optional): a library or my own code. `<script src="chart.min.js"></script>` and
  `<link rel="stylesheet" href="look.css">` naming a file in the same folder are put into the page.

I write them with `write_file` (or in `run`). Limits: index.html and data.json 1 MB each, each .js/.css 2 MB,
the whole page 4 MB, 16 .js/.css files.

## What a page can and cannot do

The page runs sandboxed: it loads **nothing** from the network and talks to nothing — no `fetch`, no CDN `<script
src="https://…">`, no remote images or fonts, no forms, no storage, no cookies. So:

- Put every library in the folder: download it once in `run` (I reach the public internet there), e.g.
  `curl -sL https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js -o notes/artifacts/<name>/chart.umd.min.js`,
  and name it with a plain `<script src="chart.umd.min.js"></script>`. Or draw with inline SVG or `<canvas>`.
- Images: inline SVG or `data:` URLs.
- Everything it needs is in `window.SHAFT_DATA`; `window.SHAFT_ARTIFACT` has its `slug`, `version` and `problems`.
- Interaction (filters, sorting, tabs, tooltips) is plain script on that data.
- Make it readable in light and dark (`@media (prefers-color-scheme: dark)`) and on a phone (narrow side panel).

If the page names a file it does not have, or anything on the network, or data.json is not valid JSON, the
operator sees the problem above it. I check mine the way the operator will see it before I say it is ready.

## Keeping it up to date by itself

Keep the page and the data apart: the layout changes when I decide; the numbers change on a schedule.

- A scheduled plugin job (skills/writing-plugins.md, "Running a plugin on a timer") fetches the numbers and writes
  `.jobs/<plugin>/data.json`, published with `"publishes": {"notes/artifacts/<name>/data.json": "data.json"}`.
  It runs without me, every `every_minutes` (5 at least), and the open page refreshes with the new data. A
  published file is at most 256 KB.
- Return `{"message": "...", "wake": true}` from the job (it needs `"can_wake": true`) only when something needs me
  (a number out of range), not on every run.
- When I change data.json by hand in a cycle instead, the page refreshes the same way.

## Telling the operator

When I make one, I send_message the operator its name and what it shows. An artifact never opened is not delivered: say
where it is (Artifacts, then its name).
