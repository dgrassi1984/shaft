"""Fixtures for the instance's mind store (supervisor/mind.py): an instance laid out as after the cutover — its
branch holds code and skills, its mind is ignored by git and versioned in its own store."""
from __future__ import annotations

from pathlib import Path

from supervisor import mind
from supervisor.core import MIND_PATHS

IGNORE_MIND = "".join(f"/{p}/\n" for p in MIND_PATHS)    # the template's .gitignore lines


def commit_at(name: str, project: Path, root: Path, when: str, message: str) -> str:
    """Commit the mind on disk to the store, dated `when` (ISO, local time) like a cycle that ended then."""
    mind.init(name, project)
    paths = [p for p in MIND_PATHS if (root / p).exists()]
    mind.git(name, "add", "-f", "-A", "--", *paths, project=project, root=root)
    mind.git(name, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message,
             project=project, root=root, env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    return mind.head(name, project)


def store_state(name: str, project: Path | None = None) -> dict[str, bytes]:
    """Every file of the store itself (refs, index, objects, logs): equal before and after = nothing written."""
    root = mind.store(name, project)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
