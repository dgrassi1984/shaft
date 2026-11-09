# PLAN-calendar-write — an instance books what it finds, and asks first when someone else would be told

**Status (2026-10-27): built (M0–M3), not yet merged.**

## Problem (today)

An instance can read the operator's calendars (docs/plans/PLAN-calendar.md: one login per account, `gmail.readonly` +
`calendar.readonly`; broker tools `calendar_events`, `calendar_event`; per-calendar grants checked on every call).
It cannot change them, so every scheduling task ends with the operator doing the last step.
Synthetic example: "I found a slot for the sample one-to-one, but you must move it yourself."

Writing is not like reading: moving a meeting that has attendees, or answering an invitation, **tells other people**,
in the operator's name. That part must not happen without the operator's say-so.

## What changes

Three broker tools, on the calendars the operator grants for writing:

| Tool | Does | Tells someone else? |
|---|---|---|
| `calendar_create` | a new event: title, start–end, calendar, optional location/description/attendees | only if it invites attendees |
| `calendar_move` | a new start–end (and optionally title, location, description) for one event | when the event has attendees other than the operator |
| `calendar_respond` | the operator's answer to an invitation: yes, no, maybe | always (the organiser is told) |

No delete and no cancel in this plan; no change to a whole recurring series (one occurrence only).

**What runs at once and what waits.** A change that tells nobody else — an event on a granted calendar with no
attendees other than the operator, created or moved — is made at once. A change that would tell someone (attendees
other than the operator, an invitation, an RSVP) is **proposed**: the tool records it and answers "waiting for the
operator", the operator gets it in the web UI and on Telegram, and approves or declines it. An approved change is
made then, with Google's `sendUpdates=all`; a change made at once uses `sendUpdates=none`. A proposal not answered
in `SHAFT_CALENDAR_PROPOSAL_HOURS` (24) expires, and its instance is told.

**Checks on every call, by the supervisor, before Google sees anything** (as the read tools do):
- the calendar is granted for writing to this instance (a read grant is not enough);
- the event belongs to that calendar, and for `move` the operator may edit it (organiser, or the calendar's owner
  with edit rights) — Google refuses otherwise, and the refusal comes back verbatim;
- start before end, both within 92 days, time zone stated or the calendar's own;
- writes per instance per day under `SHAFT_CALENDAR_WRITES_PER_DAY` (20), proposals included;
- event text from others stays marked as theirs (as in `calendar_event`).

Every write and every proposal is logged (`.supervisor/calendar/<instance>.jsonl`: who, what, before, after,
approved by whom, Google's answer) and readable by the instance through `record(kind="calendar")`.

## Options considered

**A (recommended) — the operator's account, a write scope, supervisor-side tools.** Add Google's
`calendar.events` scope to the operator's login (`shaft google login` again for each authorized account). The supervisor holds
the token, as today; every write goes through the three tools above, so the "ask first" rule is enforced, not
promised. Cost: a fresh login per account; the approval queue is new code (none exists today: no approve action in
the web UI, no Telegram buttons).

**B — the instance's own Google account.** The operator shares chosen calendars with example-b's own account (Google
Calendar's "make changes to events"), and the instance's own `google` key gets a calendar bundle in
`google.KEY_SCOPES`, used through `http_request`, as its Gmail and Drive are (PLAN-own-mail, PLAN-own-drive).
Cheaper (no approval code, no change to the operator's login; Google shows "edited by example-b"), but **nothing
enforces "ask first"**: the instance calls Google directly. And Workspace accounts often forbid sharing edit rights
outside the domain, so it may not work for the work calendar at all.

A is recommended because the requirement is a guarantee about telling other people, and only a supervisor-side tool
can give it.

## Decisions (the operator, 2026-10-27)

- **D1: accounts.** Authorize each account separately. If a Workspace administrator blocks the write
  scope, report the refusal and continue only with accounts that have authorized it.

- **D2 — what waits: only changes that tell someone else.** No other attendees: made at once (`sendUpdates=none`).
  Attendees, invitations, RSVPs: proposed, made only when approved (`sendUpdates=all`).
- **D3 — where approvals happen: the web UI**, with a Telegram message per proposal linking to it. Approving by
  replying on Telegram is a later milestone.
- **D4 — RSVPs: included** (`calendar_respond`, always a proposal).
- **D5 — limits as proposed:** 20 writes per instance per day, proposals included; proposals expire after 24 h; no
  delete or cancel; one occurrence of a recurring event only.
- **D6: per-calendar authorization** (`shaft calendar <name> grant-write <alias> <calendar>`).

**What the operator does (Claude cannot):** in Google Cloud, for each OAuth project, add
`https://www.googleapis.com/auth/calendar.events` under Google Auth Platform → Data Access (a *sensitive* scope, like
`calendar.readonly`: the personal app stays unverified); then `shaft google login` again for each account and tick it.

## Milestones

**M0 — scope and grants.** `google.CALENDAR_EVENTS` added to the login's scopes (kept with `calendar.readonly`);
`Account.require` names the login command when it is missing; write grants as a level on the existing calendar grants
(`shaft calendar <name> grant-write|revoke-write <alias> <calendar>`); `shaft calendar` shows them.
- [ ] A write tool on a calendar with only a read grant is refused before any Google call (test).
- [ ] A token without `calendar.events` fails with the exact login command, never a silent read-only.

**M1 — direct writes.** `calendar_create` and `calendar_move` for events that tell nobody; `sendUpdates=none`; the
log; `record(kind="calendar")`; the daily cap.
- [ ] Against the fake Google (tests): created and moved as asked; an event with attendees is never written directly;
  the cap; a recurring event moves one occurrence; Google's refusal returned verbatim.
- [ ] Real system: example-b creates and moves a test event on a synthetic test calendar; the operator sees it in Google Calendar.

**M2 — proposals and approval.** Proposals for changes that tell someone (and every RSVP); the web UI list with
approve/decline (tailnet only, CSRF-checked like the other actions); a Telegram message per proposal; expiry; the
instance told of each outcome in its inbox.
- [ ] A proposal makes no Google call until approved; approve makes exactly one, with `sendUpdates=all`; decline and
  expiry make none (tests).
- [ ] Real system: example-b proposes moving a test event that has a second test attendee; it waits; approved from the
  phone, the attendee gets Google's update.

**M3 — docs.** docs/safety.md (what an instance may write, what waits), docs/cli.md, docs/integrations.md, the
template prompt line for `SHAFT_CALENDAR` (which calendars are writable and the "ask first" rule).

Branch `claude/calendar-write` (and a template twin for the prompt line). Merged only when the operator says so.

## Not in this plan

Deleting or cancelling events, whole recurring series, other people's calendars beyond the operator's edit rights,
and option B.
