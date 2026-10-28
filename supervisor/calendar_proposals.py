"""Operator approvals, PLAN-calendar-write M2/D2–D5. One shared append-only journal at
.supervisor/calendar/proposals.jsonl stores snapshots; the last snapshot per id is authoritative. flock covers
state checks and submission across web/scheduler/broker processes. Approval sends the frozen request, never a
freshly read event. A durable failed claim before submission prevents retries after an ambiguous process death.
Audit/inbox delivery runs after unlocking: broker creation already holds the instance's audit lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import core, google
from .core import knob, whole

PROPOSAL_HOURS, _ = knob(
    "SHAFT_CALENDAR_PROPOSAL_HOURS", "24", whole, "calendar proposal lifetime in hours",
    group="Runs and conversations", services=("runs", "api", "web", "scheduler"))


@contextmanager
def _journal():
    path = core.SUP_DIR / "calendar/proposals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        rows = {}
        for n, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:                      # one torn line (a crash mid-write) must not hide every other proposal
                row = json.loads(line)
                rows[row['id']] = row
            except (ValueError, TypeError, KeyError):
                core.log(f"calendar proposals: unreadable line {n} in {path} skipped")
        yield stream, rows


def _save(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _outcome(row):
    from . import messages
    path = core.SUP_DIR / 'calendar' / f"{row['instance']}.jsonl"
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps({"ts": row['decided_at'], "tool": row['tool'], "proposal": row['id'],
                                 "calendar": row['target'], "before": row['before'], "after": row['request']['body'],
                                 "approved_by": row.get('approved_by'), "outcome": row['state'],
                                 "answer": row['outcome'], "submitted": False}) + '\n')
    try:   # decided and audited already: an instance that is gone (deleted) must not undo that
        messages.deliver_background(row['instance'], f"Calendar proposal {row['id']} {row['state']}: "
                                    f"{row['summary']}\n{row['outcome']}", shown='calendar approval')
    except Exception as e:
        core.log(f"calendar proposal {row['id']}: outcome not delivered to {row['instance']}: {e}")


def _notify(row):
    from . import telegram, web, webpush
    # Notification failures cannot undo the durable proposal, nor change its tool answer (D3).
    try:
        base = web.url()
        link = base.rstrip('/') + '/calendar/proposals' if base.startswith('https://') else 'No web URL is configured.'
        token, owner = telegram.tokens().get(row['instance']), telegram.paired_id()
        if token and owner is not None:
            telegram.send(token, owner, f"Calendar proposal {row['id']}\n{row['summary']}\n"
                          f"Who would be told: {', '.join(row['attendees']) or 'unknown attendees'}\n{link}")
    except Exception as e:
        core.log(f"calendar proposal {row['id']}: Telegram notification failed: {e}")
    try:
        webpush.notify({'title': f"{row['instance']}: calendar approval", 'body': row['summary'],
                        'url': '/calendar/proposals', 'tag': 'calendar-' + row['id']})
    except Exception as e:
        core.log(f"calendar proposal {row['id']}: push notification failed: {e}")


def create(instance, tool, grant, target, request, summary, attendees, before):
    now = datetime.now().astimezone()
    row = dict(id=uuid.uuid4().hex, instance=instance, tool=tool, account=grant['account'],
               calendar=grant['calendar'], target=target, request=request, summary=summary, attendees=attendees,
               before=before, created_at=now.isoformat(), expires_at=(now + timedelta(hours=PROPOSAL_HOURS)).isoformat(),
               state='pending', outcome=None)
    with _journal() as (stream, rows):
        _save(stream, row)
    _notify(row)
    return row


def listing():
    """Lazy expiry, also called by the existing scheduler even while instances are paused (M2)."""
    if not (core.SUP_DIR / "calendar/proposals.jsonl").exists():
        return []
    expired = []
    now = datetime.now().astimezone()
    with _journal() as (stream, rows):
        for row in rows.values():
            if row['state'] == 'pending' and datetime.fromisoformat(row['expires_at']) <= now:
                row.update(state='expired', outcome='The operator did not answer before expiry.', decided_at=now.isoformat())
                _save(stream, row)
                expired.append(row)
    for row in expired:
        _outcome(row)
    return sorted(rows.values(), key=lambda r: r['created_at'], reverse=True)


_said = ""


def expire() -> None:
    """The scheduler's tick (autonomy.serve): lazy expiry that never raises into its loop, which also starts every
    wake, sleep and job; a failure is logged once per kind, not every 30 s."""
    global _said
    try:
        listing()
        _said = ""
    except Exception as e:
        if (said := f"{type(e).__name__}: {e}") != _said:
            core.log(f"calendar proposals: expiry failed: {said}")
            _said = said


def decide(pid, approve, *, secrets=None):
    listing()
    with _journal() as (stream, rows):
        row = rows.get(pid)
        if not row:
            raise ValueError('no such calendar proposal')
        if row['state'] != 'pending':
            raise ValueError(f"proposal is already {row['state']}")
        row['decided_at'] = datetime.now().astimezone().isoformat()
        if not approve:
            row.update(state='declined', outcome='Declined by the operator.')
        else:
            row.update(state='failed', approved_by='operator', outcome='Submission interrupted; do not retry automatically.')
            _save(stream, row)  # claim before any external write; double approval can never send twice
            try:
                if datetime.fromisoformat(row['expires_at']) <= datetime.now().astimezone():
                    row.update(state='expired', outcome='The proposal expired before approval.')
                else:
                    if not any(g.get('write') and g['account'] == row['account'] and g['calendar'] == row['calendar']
                               for g in core.calendar_grants(row['instance'])):
                        raise PermissionError('calendar is no longer granted for writing')
                    acct = google.Account(row['account'], core.secrets_env() if secrets is None else secrets)
                    acct.require(google.CALENDAR_EVENTS)
                    req = row['request']
                    from .calendar import API
                    send = getattr(acct, req['method'].lower())
                    params = {'sendUpdates': req['sendUpdates']}
                    if req['method'] == 'PATCH':
                        params['etag'] = req['etag']
                    answer = send(f"{API}/{req['path']}", req['body'], **params)
                    row.update(state='approved', outcome=answer)
            except Exception as e:
                row.update(state='failed', outcome=f'{type(e).__name__}: {e}')
        _save(stream, row)
    _outcome(row)
    return row
