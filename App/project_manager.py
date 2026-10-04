"""Project persistence.

One JSON file per project under ``/data/projects`` (mounted as a volume), so
projects survive container restarts and image rebuilds.

Security notes:
  * Project ids are **server-generated** UUID4 hex and validated on every read
    and write, so a client-supplied id can never escape the projects directory.
  * Names are stored *inside* the JSON, never used as a filename, so they cannot
    influence the path either.
  * Writes are atomic (temp file + ``os.replace``) so an interrupted save cannot
    corrupt an existing project.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone

PROJECTS_DIR = os.environ.get("PROJECTS_DIR", "/data/projects")

#: Generous, because font documents carry every point of every glyph.
MAX_PROJECT_BYTES = int(os.environ.get("MAX_PROJECT_MB", "64")) * 1024 * 1024
MAX_PROJECTS = int(os.environ.get("MAX_PROJECTS", "500"))
MAX_NAME_LEN = 120

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ProjectError(RuntimeError):
    """Raised for invalid ids, missing projects or storage failures."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_dir() -> str:
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    return PROJECTS_DIR


def is_valid_id(project_id: object) -> bool:
    return isinstance(project_id, str) and bool(_ID_RE.match(project_id))


def _path(project_id: str) -> str:
    """Absolute path for a project id, refusing anything that is not an id."""
    if not is_valid_id(project_id):
        raise ProjectError("invalid project id")
    return os.path.join(_ensure_dir(), f"{project_id}.json")


def clean_name(name: object, fallback: str = "Untitled") -> str:
    """Trim, strip control characters and cap the length of a project name."""
    if not isinstance(name, str):
        return fallback
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    return cleaned[:MAX_NAME_LEN] or fallback


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ProjectError("project file is not an object")
    return data


def summary(record: dict) -> dict:
    """The lightweight shape sent to the UI (never includes the document)."""
    document = record.get("document") or {}
    glyphs = document.get("glyphs") or []
    return {
        "id": record.get("id"),
        "name": record.get("name", "Untitled"),
        "created": record.get("created"),
        "updated": record.get("updated"),
        "stats": record.get("stats") or {"glyphs": len(glyphs)},
        "glyphs": len(glyphs),
        "family_name": document.get("family_name"),
        "style_name": document.get("style_name"),
    }


def list_projects() -> list[dict]:
    """All projects, newest first. Corrupt files are skipped, not fatal."""
    directory = _ensure_dir()
    summaries = []
    for entry in os.scandir(directory):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            summaries.append(summary(_read(entry.path)))
        except (OSError, ValueError, ProjectError):
            continue
    summaries.sort(key=lambda item: item.get("updated") or "", reverse=True)
    return summaries


def load_project(project_id: str) -> dict:
    path = _path(project_id)
    if not os.path.exists(path):
        raise ProjectError("project not found")
    return _read(path)


def _storage_help(directory: str, exc: OSError) -> str:
    return (
        f"the projects directory {directory!r} is not writable by the container "
        f"user (uid 1000): {exc}. If /data is a host bind mount, make it writable "
        "by uid 1000 (see the data-volume notes in README.md), or use the default "
        "named volume."
    )


def _write_atomic(directory: str, path: str, payload: str) -> None:
    """Write `payload` to `path` atomically, so a partial save cannot corrupt
    the previous revision."""
    try:
        handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    except OSError as exc:
        raise ProjectError(_storage_help(directory, exc)) from exc

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise ProjectError(_storage_help(directory, exc)) from exc
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_project(document: dict, name: str = "Untitled", project_id: str | None = None,
                 stats: dict | None = None) -> dict:
    """Create or update a project. Returns its summary."""
    if not isinstance(document, dict) or not isinstance(document.get("glyphs"), list):
        raise ProjectError("document must be an object with a 'glyphs' list")

    directory = _ensure_dir()
    creating = project_id is None

    if creating:
        existing = sum(1 for e in os.scandir(directory) if e.name.endswith(".json"))
        if existing >= MAX_PROJECTS:
            raise ProjectError(f"project limit reached ({MAX_PROJECTS})")
        project_id = uuid.uuid4().hex
        created = _now()
    else:
        path = _path(project_id)
        if not os.path.exists(path):
            raise ProjectError("project not found")
        try:
            created = _read(path).get("created") or _now()
        except (OSError, ValueError, ProjectError):
            created = _now()

    if stats is None:
        stats = {"glyphs": len(document.get("glyphs") or [])}

    record = {
        "id": project_id,
        "name": clean_name(name),
        "created": created,
        "updated": _now(),
        "stats": stats,
        "document": document,
    }

    payload = json.dumps(record, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_PROJECT_BYTES:
        raise ProjectError(
            f"project is too large (max {MAX_PROJECT_BYTES // (1024 * 1024)} MB)"
        )

    # Atomic write: a partial save must never destroy the previous revision.
    path = _path(project_id)
    _write_atomic(directory, path, payload)

    return summary(record)


def delete_project(project_id: str) -> bool:
    path = _path(project_id)
    if not os.path.exists(path):
        return False
    os.unlink(path)
    return True


def storage_info() -> dict:
    """Report project storage usage (surfaced by /api/projects)."""
    directory = _ensure_dir()
    count = 0
    total = 0
    for entry in os.scandir(directory):
        if entry.is_file() and entry.name.endswith(".json"):
            count += 1
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return {
        "directory": directory,
        "writable": os.access(directory, os.W_OK),
        "projects": count,
        "bytes": total,
        "max_projects": MAX_PROJECTS,
        "max_project_mb": MAX_PROJECT_BYTES // (1024 * 1024),
    }
