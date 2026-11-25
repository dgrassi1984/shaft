"""Supervisor-owned meeting metadata and resumable audio uploads."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from . import core

CHUNK_SIZE = 8_388_608
SIDECAR_LIMIT = 65_536
COPY_SIZE = 256 * 1024
ID = r"m-[0-9]{8}-[0-9]{4}-[0-9a-f]{4}"
CHUNK_ROUTE = rf"/meetings/(?P<meeting_id>{ID})/chunk/(?P<index>[0-9]+)"
EXTENSIONS = ("m4a", "caf", "wav")
MODES = ("online", "inperson")
WORKER_TRANSITIONS = {"queued": ("processing",), "processing": ("queued", "ready", "failed"),
                      "ready": ("queued",), "failed": ("queued",)}
TRANSCRIPT_PAGE_SIZE = 200


class Error(Exception):
    pass


class Invalid(Error):
    pass


class Missing(Error):
    pass


class Conflict(Error):
    pass


class TooLarge(Error):
    pass


def _root() -> Path:
    return core.SUP_DIR / "meetings"


@contextmanager
def _atomic_output(path: Path):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as out:
            yield out
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _save(folder: Path, metadata: dict) -> None:
    with _atomic_output(folder / "meeting.json") as out:
        out.write(_json_bytes(metadata))


def _timestamp(value, key: str) -> str:
    try:
        if "T" in value:
            return datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    raise Invalid(f"{key} must be an ISO 8601 date and time.")


def _metadata(*, mode, started, device, ext, title="", ended=None, sidecar=None, account=None) -> dict:
    if mode not in MODES:
        raise Invalid("mode must be online or inperson.")
    if ext not in EXTENSIONS:
        raise Invalid("ext must be m4a, caf, or wav.")
    if not isinstance(device, str) or not device.strip():
        raise Invalid("device must be a non-empty string.")
    if not isinstance(title, str):
        raise Invalid("title must be a string.")
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise Invalid("account must be a non-empty alias or null.")
    return {
        "title": title, "created": datetime.now().astimezone().isoformat(timespec="microseconds"),
        "started": _timestamp(started, "started"), "ended": None if ended is None else _timestamp(ended, "ended"),
        "mode": mode, "device": device, "channels": sidecar.get("channels"), "account": account,
        "state": "uploading", "error": None, "size": None, "sha256": None, "ext": ext,
        "chunk_size": CHUNK_SIZE, "chunks_received": [], "processed_with": None,
    }


def _publish(metadata: dict, sidecar: bytes, source: Path | None = None) -> dict:
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix = datetime.fromisoformat(metadata["created"]).strftime("m-%Y%m%d-%H%M-")
    with tempfile.TemporaryDirectory(dir=root, prefix=".create-") as staging:
        folder = Path(staging)
        (folder / ".lock").touch(mode=0o600)
        with _atomic_output(folder / "sidecar.json") as out:
            out.write(sidecar)
        if source is None:
            (folder / "upload").mkdir()
        else:
            size, digest = _assemble(folder / f"audio.{metadata['ext']}", (source,))
            metadata.update(state="queued", size=size, sha256=digest)
        while True:
            metadata["id"] = prefix + secrets.token_hex(2)
            _save(folder, metadata)
            try:
                os.replace(folder, root / metadata["id"])
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
            else:
                return metadata


def create(data: dict) -> dict:
    sidecar = data.get("sidecar", {})
    if not isinstance(sidecar, dict):
        raise Invalid("sidecar must be a JSON object.")
    try:
        encoded = _json_bytes(sidecar)
    except (ValueError, RecursionError):
        raise Invalid("sidecar must be a JSON object with valid JSON values.") from None
    if len(encoded) > SIDECAR_LIMIT:
        raise TooLarge(f"sidecar must be at most {SIDECAR_LIMIT} bytes.")
    fields = ("mode", "started", "device", "ext", "title", "ended", "account")
    values = {key: data[key] for key in fields if key in data}
    for key in fields[:4]:
        if key not in values:
            raise Invalid(f"{key} is required.")
    metadata = _metadata(**values, sidecar=sidecar)
    try:
        _json_bytes(metadata)
    except UnicodeError:
        raise Invalid("Meeting fields must contain valid Unicode.") from None
    client_id = data.get("client_id")
    if client_id is None:
        return _publish(metadata, encoded)
    try:
        metadata["client_id"] = str(UUID(client_id))
    except (AttributeError, ValueError, TypeError):
        raise Invalid("client_id must be a UUID.") from None
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".create.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for folder in root.glob("m-*"):
            try:
                with _locked(folder.name) as (_, existing):
                    if existing.get("client_id") == metadata["client_id"]:
                        return existing
            except Missing:
                continue
        return _publish(metadata, encoded)


def _received(folder: Path) -> list[int]:
    return sorted(int(path.name) for path in (folder / "upload").iterdir() if not path.name.startswith("."))


@contextmanager
def _locked(meeting_id: str):
    if re.fullmatch(ID, meeting_id) is None:
        raise Missing("No such meeting.")
    folder = _root() / meeting_id
    lock_path = folder / ".lock"
    try:
        lock = lock_path.open("rb")
    except FileNotFoundError:
        raise Missing("No such meeting.") from None
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not os.path.samestat(os.fstat(lock.fileno()), lock_path.stat()):
                raise Missing("No such meeting.")
            metadata = json.loads((folder / "meeting.json").read_bytes())
        except FileNotFoundError:
            raise Missing("No such meeting.") from None
        if metadata["state"] == "uploading":
            # The rename of a chunk may have succeeded before the metadata write.
            metadata["chunks_received"] = _received(folder)
        yield folder, metadata


def get(meeting_id: str) -> dict:
    with _locked(meeting_id) as (_, metadata):
        return metadata


def list_meetings() -> list[dict]:
    root = _root()
    rows = []
    if root.exists():
        for folder in root.iterdir():
            if re.fullmatch(ID, folder.name):
                try:
                    rows.append(get(folder.name))
                except Missing:
                    continue  # Deleted after it was listed.
    return sorted(rows, key=lambda row: (datetime.fromisoformat(row["created"]), row["id"]), reverse=True)


def _account_alias(alias, *, configured=True) -> str:
    from . import google

    if not isinstance(alias, str) or not google.ALIAS_RE.fullmatch(alias):
        raise Invalid("account must be a Google account alias.")
    if configured and not google.account(alias):
        raise Invalid("Choose a configured Google account alias.")
    return alias


def update(meeting_id: str, changes: dict) -> dict:
    if not changes or changes.keys() - {"account", "title"}:
        raise Invalid("Supply account, title, or both.")
    if "account" in changes and changes["account"] is not None:
        _account_alias(changes["account"])
    if "title" in changes and not isinstance(changes["title"], str):
        raise Invalid("title must be a string.")
    try:
        _json_bytes(changes)
    except UnicodeError:
        raise Invalid("Meeting fields must contain valid Unicode.") from None
    with _locked(meeting_id) as (folder, metadata):
        account_changed = "account" in changes and changes["account"] != metadata["account"]
        metadata.update(changes)
        if account_changed and (metadata["state"] == "ready" or "notified" in metadata):
            if metadata["account"]:
                metadata["notification_owed"] = True
            else:
                metadata.pop("notification_owed", None)
        _save(folder, metadata)
        if metadata["state"] == "ready" and _notification_owed(metadata):
            _deliver(folder, metadata, json.loads((folder / "transcript.json").read_bytes()))
        return metadata


def change_grant(name: str, action: str, alias: str) -> list[dict]:
    core.instance_root(name)
    _account_alias(alias, configured=action == "grant")
    with core._grant_lock(name, "meetings"):
        grants = [g for g in core.meetings_grants(name) if g["account"] != alias]
        if action == "grant":
            grants.append({"account": alias})
        return core.set_meetings_grants(name, grants)


def delete(meeting_id: str) -> None:
    with _locked(meeting_id) as (folder, _):
        _delete_folder(folder)


def _delete_folder(folder: Path) -> None:
    with tempfile.TemporaryDirectory(dir=folder.parent, prefix=".delete-") as trash:
        os.replace(folder, Path(trash) / folder.name)


def upload_status(metadata: dict) -> dict:
    return {"received": metadata["chunks_received"], "chunk_size": metadata["chunk_size"]}


def put_chunk(meeting_id: str, index: int, source, length: int) -> list[int]:
    with _locked(meeting_id) as (folder, metadata):
        if metadata["state"] != "uploading":
            raise Conflict("This meeting is no longer uploading.")
        if length > metadata["chunk_size"]:
            raise TooLarge(f"A chunk can be at most {metadata['chunk_size']} bytes.")
        if index < 0 or length < 0:
            raise Invalid("Chunk index and length must be non-negative.")
        with _atomic_output(folder / "upload" / str(index)) as out:
            remaining = length
            while remaining:
                block = source.read(min(remaining, COPY_SIZE))
                if not block:
                    raise Invalid("The chunk ended before its declared length.")
                out.write(block)
                remaining -= len(block)
        metadata["chunks_received"] = _received(folder)
        _save(folder, metadata)
        return metadata["chunks_received"]


def _assemble(path: Path, parts, *, expected: tuple[int, str] | None = None) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with _atomic_output(path) as out:
        for part in parts:
            with part.open("rb") as source:
                while block := source.read(COPY_SIZE):
                    size += len(block)
                    out.write(block)
                    digest.update(block)
        sha256 = digest.hexdigest()
        if expected is not None:
            if size != expected[0]:
                raise Conflict(f"Audio size is {size} bytes. Expected {expected[0]}.")
            if sha256 != expected[1]:
                raise Conflict("The audio SHA-256 does not match.")
    return size, sha256


def finish(meeting_id: str, *, size=None, sha256=None, chunks=None) -> dict:
    if type(size) is not int or size < 0:
        raise Invalid("size must be a non-negative integer.")
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        raise Invalid("sha256 must be a 64-digit hexadecimal string.")
    if type(chunks) is not int or chunks < 0:
        raise Invalid("chunks must be a non-negative integer.")
    sha256 = sha256.lower()
    with _locked(meeting_id) as (folder, metadata):
        upload = folder / "upload"
        if metadata["state"] == "uploading":
            received = metadata["chunks_received"]
            for index, stored in enumerate(received[:chunks]):
                if index != stored:
                    raise Conflict(f"Chunk {index} is missing.")
            if len(received) < chunks:
                raise Conflict(f"Chunk {len(received)} is missing.")
            if len(received) > chunks:
                # A stale finish that describes fewer chunks would assemble a prefix and delete the rest (review
                # 2026-11-25): refuse it and keep every chunk.
                raise Conflict(f"Chunk {chunks} and later were sent, but finish names {chunks} chunk(s).")
            parts = (upload / str(index) for index in range(chunks))
            _assemble(folder / f"audio.{metadata['ext']}", parts, expected=(size, sha256))
            metadata.update(state="queued", size=size, sha256=sha256, chunks_received=received[:chunks])
            _save(folder, metadata)
        elif (metadata["size"], metadata["sha256"], len(metadata["chunks_received"])) != (size, sha256, chunks):
            raise Conflict("This meeting is no longer uploading.")
        if upload.exists():
            shutil.rmtree(upload)
        return metadata


def set_state(meeting_id: str, state: str, error=None, processed_with=None) -> None:
    with _locked(meeting_id) as (folder, metadata):
        current = metadata["state"]
        if current not in WORKER_TRANSITIONS or state not in (*WORKER_TRANSITIONS[current], current):
            raise Conflict(f"Cannot move a meeting from {current} to {state}.")
        metadata.update(state=state, error=error, processed_with=processed_with)
        _save(folder, metadata)


def reprocess(meeting_id: str) -> dict:
    with _locked(meeting_id) as (folder, metadata):
        if metadata["state"] not in ("ready", "failed"):
            raise Conflict("Only ready or failed meetings can be reprocessed.")
        if metadata["state"] == "ready":
            metadata.setdefault("notified", [])  # Already ready before watches were introduced.
        metadata.pop("worker_crashes", None)
        metadata.update(state="queued", error=None, processed_with=None)
        _save(folder, metadata)
        return metadata


def recover_processing(meeting_id: str) -> None:
    with _locked(meeting_id) as (folder, metadata):
        if metadata["state"] == "processing":
            crashes = metadata.get("worker_crashes", 0) + 1
            retry = crashes < 2
            metadata.update(worker_crashes=crashes, state="queued" if retry else "failed",
                            error=None if retry else "worker stopped while processing")
            _save(folder, metadata)


def complete_processing(meeting_id: str, transcript: dict, markdown: str) -> None:
    with _locked(meeting_id) as (folder, metadata):
        _complete_processing(folder, metadata, transcript, markdown)


def _write_transcript(folder: Path, transcript: dict, markdown: str) -> None:
    with _atomic_output(folder / "transcript.json") as out, _atomic_output(folder / "transcript.md") as md:
        out.write(_json_bytes(transcript))
        md.write(markdown.encode())


def _complete_processing(folder: Path, metadata: dict, transcript: dict, markdown: str) -> None:
    if metadata["state"] != "processing":
        raise Conflict("This meeting is no longer processing.")
    _write_transcript(folder, transcript, markdown)
    first_ready = "notified" not in metadata
    if first_ready:
        metadata["notified"] = None                 # owed: delivered below, or at the worker's next start
    metadata.update(state="ready", error=None, processed_with=transcript["processed_with"],
                    ready_revision=secrets.token_hex(16))
    _save(folder, metadata)
    if _notification_owed(metadata):
        _deliver(folder, metadata, transcript)


def _notification_owed(metadata: dict) -> bool:
    return metadata.get("notified", []) is None or metadata.get("notification_owed", False)


def _deliver(folder: Path, metadata: dict, transcript: dict) -> None:
    """Keep prior recipients when sending a ready note after filing under a new account."""
    from . import watch

    notified = metadata.get("notified") or []
    if metadata["account"]:
        notified.extend(watch.meeting_ready(metadata, transcript))
    metadata["notified"] = notified
    metadata.pop("notification_owed", None)
    _save(folder, metadata)


def deliver_owed() -> None:
    """Send ready notes left pending by an interrupted processing or filing operation."""
    for row in list_meetings():
        if row["state"] == "ready" and _notification_owed(row):
            try:
                with _locked(row["id"]) as (folder, metadata):
                    if metadata["state"] == "ready" and _notification_owed(metadata):
                        _deliver(folder, metadata, json.loads((folder / "transcript.json").read_bytes()))
            except Missing:
                continue


def detail(meeting_id: str, page: int) -> dict:
    with _locked(meeting_id) as (folder, metadata):
        if metadata["state"] == "ready":
            transcript = json.loads((folder / "transcript.json").read_bytes())
            segments = transcript["segments"]
            first = (page - 1) * TRANSCRIPT_PAGE_SIZE
            metadata["transcript"] = {**transcript, "segments": segments[first:first + TRANSCRIPT_PAGE_SIZE],
                                      "page": page, "total_segments": len(segments)}
        return metadata


def import_audio(source: Path, *, title: str | None, mode: str) -> dict:
    if not source.is_file():
        raise Invalid("Choose a local audio file.")
    metadata = _metadata(mode=mode, started=datetime.now().isoformat(), device="import",
                         ext=source.suffix.lower().removeprefix("."), title=source.stem if title is None else title,
                         sidecar={})
    return _publish(metadata, b"{}", source)


def command(args) -> int:
    import sys

    try:
        if args.action == "list":
            rows = list_meetings()
            if args.json:
                print(json.dumps({"meetings": rows}))
            else:
                for row in rows:
                    print(f"{row['id']}  {row['state']}  {row['title']}")
            return 0
        if args.action in ("grant", "revoke"):
            result = {"meetings": change_grant(args.name, args.action, args.alias)}
            line = ", ".join(g["account"] for g in result["meetings"]) or "No meeting grants."
        elif args.action == "file":
            result = update(args.id, {"account": args.alias})
            line = f"Filed {args.id} under {args.alias}."
        elif args.action == "proposals":
            from . import meeting_proposals

            result = {"proposals": meeting_proposals.pending()}
            line = json.dumps(result, indent=2)
        elif args.action == "delete":
            delete(args.id)
            result = {"id": args.id, "deleted": True}
            line = f"Deleted {args.id}."
        elif args.action == "name":
            from . import speakers
            result = speakers.name_label(args.id, args.label, args.name)
            line = result["name"]
        else:
            result = (get(args.id) if args.action == "show"
                      else reprocess(args.id) if args.action == "reprocess"
                      else import_audio(Path(args.file), title=args.title, mode=args.mode))
            line = json.dumps(result, indent=2) if args.action == "show" else f"Queued {result['id']}."
    except (Error, OSError) as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(e, file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else line)
    return 0
