"""Read-only operator Drive metadata. Every result is checked against the current reach."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import google

API = 'https://www.googleapis.com/drive/v3'
FILE_RE = re.compile(r'^[A-Za-z0-9_-]{10,}$')
FILE_FIELDS = ('id,name,mimeType,size,createdTime,modifiedTime,trashed,ownedByMe,'
               'owners(emailAddress,displayName),driveId,capabilities(canDownload),shortcutDetails(targetId),'
               'lastModifyingUser(displayName,emailAddress)')
SHORTCUT = 'application/vnd.google-apps.shortcut'


class NotVisible(PermissionError):
    """A hidden, missing or malformed file receives the same answer."""


def member_drives(acct: google.Account) -> set[str]:
    """Every Shared Drive the account belongs to, across every page."""
    drives, token = set(), ''
    while True:
        found = acct.get(f'{API}/drives', fields='drives(id),nextPageToken', pageSize=100, pageToken=token)
        drives.update(d['id'] for d in found.get('drives') or [])
        token = found.get('nextPageToken') or ''
        if not token:
            return drives


def visible(f: dict, reach: str, address: str, drives: set[str], before: datetime | None = None) -> bool:
    if f.get('trashed'):
        return False
    if before and datetime.fromisoformat(f['createdTime'].replace('Z', '+00:00')).timestamp() >= before.timestamp():
        return False
    if reach == 'all':
        return True
    if reach != 'own':
        return False
    if f.get('ownedByMe'):
        return True
    if f.get('driveId'):
        return f['driveId'] in drives
    domain = address.rpartition('@')[2].casefold()
    return bool(domain and domain not in {'gmail.com', 'googlemail.com'} and any(
        o.get('emailAddress', '').rpartition('@')[2].casefold() == domain for o in f.get('owners') or []))


def get_file(acct: google.Account, reach: str, drives: set[str], file_id: str,
             before: datetime | None = None) -> dict:
    """Fetch metadata and resolve at most one shortcut, checking both it and its target."""
    def fetch(fid):
        if not isinstance(fid, str) or not FILE_RE.fullmatch(fid):
            raise NotVisible('file is not visible to you')
        try:
            f = acct.get(f'{API}/files/{fid}', fields=FILE_FIELDS, supportsAllDrives='true')
        except google.ApiError as e:
            if e.status in (400, 404):
                raise NotVisible('file is not visible to you') from None
            raise
        if not visible(f, reach, acct.address, drives, before):
            raise NotVisible('file is not visible to you')
        return f

    f = fetch(file_id)
    if f.get('mimeType') == SHORTCUT:
        f = fetch((f.get('shortcutDetails') or {}).get('targetId'))
        if f.get('mimeType') == SHORTCUT:
            raise NotVisible('file is not visible to you')
    return f


def _literal(value: str) -> str:
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _row(acct: google.Account, f: dict) -> dict:
    owners = f.get('owners') or []
    owner = ', '.join(o.get('displayName') or o.get('emailAddress') or '' for o in owners)
    return {'id': f['id'], 'handle': f"{acct.alias}/{f['id']}", 'name': f.get('name', ''),
            'mimeType': f.get('mimeType', ''), 'owner': 'Shared Drive' if f.get('driveId') else owner,
            'modified': f.get('modifiedTime', '')}


def search(acct: google.Account, reach: str, drives: set[str], text: str, folder: str, modified_after: str, limit: int,
           before: datetime | None = None, page: str = '') -> tuple[list[dict], int, str, bool]:
    """Visible metadata rows, dropped count, ID-only cursor and Google's incomplete-search flag."""
    q = ['trashed = false']
    if text:
        q.append(f'fullText contains {_literal(text)}')
    if folder:
        parent = get_file(acct, reach, drives, folder, before)
        q.append(f"{_literal(parent['id'])} in parents")
    if modified_after:
        q.append(f'modifiedTime > {_literal(modified_after)}')
    try:
        state = json.loads(page) if page else {'pending': [], 'token': ''}
        pending, token = state['pending'], state['token']
        if not isinstance(pending, list) or not all(isinstance(fid, str) for fid in pending) or not isinstance(token, str):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError('invalid drive_search cursor') from None
    rows, dropped, first, incomplete = [], 0, not page, False
    while len(rows) < limit:
        if not pending:
            if not first and not token:
                break
            found = acct.get(f'{API}/files', q=' and '.join(q), fields=f'files({FILE_FIELDS}),nextPageToken,incompleteSearch',
                             supportsAllDrives='true', includeItemsFromAllDrives='true', corpora='allDrives',
                             pageSize=max(1, min(limit - len(rows), 100)), pageToken=token)
            pending = [f.get('id') for f in found.get('files') or []]
            token, first = found.get('nextPageToken') or '', False
            incomplete = incomplete or bool(found.get('incompleteSearch'))
            if not pending:
                continue
        fid, pending = pending[0], pending[1:]
        try:
            f = get_file(acct, reach, drives, fid, before)
        except NotVisible:
            dropped += 1
            continue
        rows.append(_row(acct, f))
    rows.sort(key=lambda r: r['modified'], reverse=True)
    cursor = json.dumps({'pending': pending, 'token': token}, separators=(',', ':')) if pending or token else ''
    return rows, dropped, cursor, incomplete
