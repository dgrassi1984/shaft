# PLAN — where an instance's messages reach the operator (web UI or Telegram)

Status: built on branch `claude/operator-channel` (2026-10-20), M1–M3 done; M4 (live check) waits for the merge.
Reviewed by Codex: four findings, all fixed (timestamp cursors could skip a message logged late — Telegram's too; a
failed wake hid the next notification; `/push/latest` could be taken cross-site; `api_talks.json` was not saved whole).

## Why

Every `send_message(to="operator")` lands in the operator inbox (the web UI shows it) and raises a macOS banner;
Telegram then copies it from the instance's bot whenever that bot exists. The web UI pushes to the phone only when a
*chat reply* ends — a message an instance sends you never reaches the phone through the web UI. So the operator
cannot choose the web UI as where an instance reaches them, and the instance is told "on Telegram" regardless.

The operator, 2026-10-20: configure per instance which channel is the default, and the notification goes there; the
operator picks the conversation up from there. Decisions taken with him:

- Always write to the inbox (unchanged): the web UI's message log stays the one record.
- Each conversation carries an explicit channel attribute (web, telegram), not one inferred from headers.
- During an open conversation, the alert goes through that conversation's channel.
- Otherwise, the instance's setting: `auto` (default) — the channel the operator last wrote to it on — or a fixed
  `web` or `telegram`.
- Web push for every message to the operator routed to the web, not only for chat replies.
- The instance's `send_message` description says where its message goes.

## Design

**One routing decision, made when the message is delivered, and recorded with it.**
`supervisor/channel.py: route(name) -> (channel, why)`, called by the broker in `_deliver` for "operator":

1. An open conversation (API talk not yet written up, API service running) on web or Telegram: the most recent
   one's channel.
2. The operator's setting for the instance, if fixed (`web` / `telegram`).
3. `auto`: the channel of the operator's most recent web or Telegram conversation with the instance.
4. No conversation ever: Telegram if its bot can reach the operator, else the web.

A channel that cannot alert (Telegram: no bot, not paired, service off; web: no device has notifications on) gives
way to the other if that one can, and the reason says so. The row in `.supervisor/messages.jsonl` gets
`alert` (web | telegram) and `alert_why`. The macOS banner is unchanged (it is the Mac, not a channel).

**The channel attribute.** Clients name their channel with `X-Shaft-Channel: web | telegram` (the web UI and the
Telegram service send it); the API stores it on the talk (`.supervisor/api-talks.json`, `channel`), `api` when absent.
"Last used" and "open" are read from those talks: no second record.

**Delivery, per channel, from the log** (each service a reader of the one log, with its own cursor — a byte offset
in append order, `messages.appended`, never a timestamp: a message is stamped before it is logged, so a timestamp
cursor could skip one logged late):
- Telegram: `telegram.forward_new` forwards only rows with `alert == "telegram"`.
- Web: the web service forwards rows with `alert == "web"` as a push (a thread like Telegram's), unless the
  instance's chat page is being looked at.

**Push events (fixes issue #23 part 2).** A push carries no content (PLAN-pwa P4); the service worker fetched "the
newest reply of any instance", so two quick pushes showed the same text. Now each push queues its event
(title, body, url) for each subscribed device (`.supervisor/web/push-queue.json`); `/push/latest` (same-origin
requests only: it takes them) hands the device everything waiting, each shown as its own notification, so a wake that
failed (retried twice) shows with the next one. Chat replies and instance messages both go through it.

**Picking up on the web.** The push opens the instance's chat. The chat shows what the instance sent you since that
chat began — now without the chat's end as a bound for the chat on screen, so a message after it ended still shows
under it — and each such card has the Reply button: quoting a question answers it (archived from the inbox), as a
Telegram reply does.

**The setting.** `.supervisor/tools/<name>.json` (`channel`), operator-only like the other access settings (an
instance cannot change it). `shaft channel <name> [auto|web|telegram]`; the web UI's Access page shows the setting,
where a message would go now and why, and sets it.

## Milestones

- M1 `channel.py` (setting, route), API channel attribute, clients send it, broker records `alert`, Telegram
  filters, tool description. Tests: route cases, API stores channel, forward_new filter, description.
- M2 push events queue, web forwarder thread, `/push/latest` per device, chat reply through it. Tests: two pushes →
  two different events; a web-routed message → one push; watched → none.
- M3 chat feed window + Reply on sent cards answering a question; Access page + CLI; message page shows where it
  was sent. Docs: integrations.md, web-and-api.md, cli.md.
- M4 verify in the running system (after merge, with the operator's go-ahead to restart api, web, telegram).
