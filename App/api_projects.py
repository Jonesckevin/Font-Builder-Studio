"""Font Builder Studio - project persistence API.

Projects are stored as one JSON file each under ``/data/projects`` (see
``project_manager.py``). All ids are server-generated; see that module for the
path-traversal guarantees.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

import font_model
import project_manager
from extensions import limiter

log = logging.getLogger("fontbuilder.projects")

bp = Blueprint("projects_api", __name__, url_prefix="/api/projects")


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _status_for(exc: Exception) -> int:
    """Map a storage failure to a meaningful HTTP status."""
    text = str(exc)
    if "not writable" in text:
        return 503
    if "too large" in text:
        return 413
    if "not found" in text:
        return 404
    if "limit reached" in text:
        return 507
    return 500


@bp.get("")
def list_projects():
    try:
        return jsonify({"ok": True, "projects": project_manager.list_projects(),
                        "storage": project_manager.storage_info()})
    except project_manager.ProjectError as exc:
        return _error(str(exc), 500)


@bp.post("")
@limiter.limit("60 per minute")
def create_project():
    """Create a new project from the posted document."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("expected a JSON object with 'document' and 'name'")

    document = payload.get("document")
    if not isinstance(document, dict):
        return _error("missing 'document' object")

    try:
        summary = project_manager.save_project(
            document,
            name=payload.get("name") or document.get("family_name") or "Untitled",
            stats=font_model.document_stats(document),
        )
    except project_manager.ProjectError as exc:
        return _error(str(exc), _status_for(exc))

    log.info("created project %s (%s)", summary["id"], summary["name"])
    return jsonify({"ok": True, "project": summary}), 201


@bp.get("/<project_id>")
def get_project(project_id: str):
    try:
        record = project_manager.load_project(project_id)
    except project_manager.ProjectError as exc:
        return _error(str(exc), 404)
    return jsonify(
        {
            "ok": True,
            "project": {**project_manager.summary(record), "document": record.get("document")},
        }
    )


@bp.put("/<project_id>")
@limiter.limit("120 per minute")
def update_project(project_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("expected a JSON object")

    document = payload.get("document")
    if not isinstance(document, dict):
        return _error("missing 'document' object")

    try:
        existing = project_manager.load_project(project_id)
    except project_manager.ProjectError as exc:
        return _error(str(exc), 404)

    try:
        summary = project_manager.save_project(
            document,
            name=payload.get("name") or existing.get("name") or "Untitled",
            project_id=project_id,
            stats=font_model.document_stats(document),
        )
    except project_manager.ProjectError as exc:
        return _error(str(exc), _status_for(exc))

    return jsonify({"ok": True, "project": summary})


@bp.delete("/<project_id>")
@limiter.limit("60 per minute")
def delete_project(project_id: str):
    try:
        removed = project_manager.delete_project(project_id)
    except project_manager.ProjectError as exc:
        return _error(str(exc), 400)
    if not removed:
        return _error("project not found", 404)
    return jsonify({"ok": True})
