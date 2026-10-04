"""Font Builder Studio - parent-side font engine.

Runs :mod:`font_worker` as a **one-shot child process** under resource limits
and a hard timeout. The web worker never imports FontForge:

* FontForge corrupts its global state across operations in one interpreter, 
  so each operation needs a fresh process.
* Containing crashes and enforcing limits is much easier with a subprocess.

All functions are pure with respect to the web process - no font state is kept
between calls, which is what makes the HTTP layer safe to scale.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import font_model

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(APP_DIR, "font_worker.py")
RESULT_SUFFIX = ".result.json"

# --- Limits -----------------------------------------------------------------
DEFAULT_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT_SECONDS", "60"))
MEMORY_LIMIT_MB = int(os.environ.get("WORKER_MEMORY_LIMIT_MB", "512"))
CPU_LIMIT_SECONDS = int(os.environ.get("WORKER_CPU_LIMIT_SECONDS", "30"))
MAX_OPEN_FILES = 256
MAX_PROCS = 64


class EngineError(RuntimeError):
    """Raised when the font engine fails, times out or is missing."""


def _apply_limits() -> None:
    """preexec_fn: cap CPU, address space, file descriptors and processes.

    Runs in the child between fork and exec, so it must not allocate or log.
    POSIX only - the container is Linux, but the guard keeps local dev on
    Windows working.
    """
    try:
        import resource  # noqa: PLC0415
    except ImportError:  # pragma: no cover - non-POSIX
        return

    def _set(what, soft, hard=None):
        try:
            resource.setrlimit(what, (soft, hard if hard is not None else soft))
        except (ValueError, OSError):
            pass

    _set(resource.RLIMIT_CPU, CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 5)
    _set(resource.RLIMIT_AS, MEMORY_LIMIT_MB * 1024 * 1024)
    _set(resource.RLIMIT_NOFILE, MAX_OPEN_FILES)
    if hasattr(resource, "RLIMIT_NPROC"):
        _set(resource.RLIMIT_NPROC, MAX_PROCS)


def _run_worker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    preexec = _apply_limits if os.name == "posix" else None
    try:
        return subprocess.run(
            [sys.executable, WORKER, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=APP_DIR,
            preexec_fn=preexec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError(f"font engine timed out after {timeout}s") from exc


def _read_envelope(output_path: str, proc: subprocess.CompletedProcess) -> dict:
    """Read the worker's envelope, or explain why there isn't one."""
    envelope_path = f"{output_path}{RESULT_SUFFIX}"
    if os.path.exists(envelope_path):
        try:
            with open(envelope_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(f"font engine wrote an unreadable result: {exc}") from exc

    if proc.returncode < 0:
        raise EngineError(f"font engine crashed (signal {-proc.returncode})")
    stderr = (proc.stderr or "").strip()
    raise EngineError(
        f"font engine produced no result (exit {proc.returncode})"
        + (f": {stderr[-400:]}" if stderr else "")
    )


def engine_info() -> dict:
    """Round-trip the worker's ``ping`` command to report engine availability."""
    proc = _run_worker(["ping"], DEFAULT_TIMEOUT)
    if proc.returncode != 0:
        return {"available": False, "error": (proc.stderr or "").strip()[-400:]}
    try:
        return {"available": True, **json.loads(proc.stdout.strip().splitlines()[-1])}
    except (ValueError, IndexError):
        return {"available": True, "fontforge": "unknown"}


def parse_font_bytes(data: bytes, filename: str) -> dict:
    """Parse uploaded font bytes into a validated document."""
    if not data:
        raise EngineError("empty upload")

    # FontForge infers the format from the extension, so preserve it.
    suffix = os.path.splitext(filename or "")[1].lower() or ".tmp"

    with tempfile.TemporaryDirectory(prefix="fbs-parse-") as tmp:
        source = os.path.join(tmp, f"input{suffix}")
        with open(source, "wb") as fh:
            fh.write(data)

        doc_path = os.path.join(tmp, "document.json")
        proc = _run_worker(["parse", "--input", source, "--output", doc_path], DEFAULT_TIMEOUT)
        envelope = _read_envelope(doc_path, proc)

        if not envelope.get("ok"):
            raise EngineError(envelope.get("error") or "font engine failed to parse the font")

        try:
            with open(doc_path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(f"font engine produced an unreadable document: {exc}") from exc

    return font_model.validate_document(doc)


def build_font_bytes(doc: dict, fmt: str) -> tuple[bytes, dict]:
    """Build font bytes (and stats) from a document.

    Returns ``(data, stats)``.
    """
    fmt = font_model.normalise_format(fmt)
    font_model.validate_document(doc)

    with tempfile.TemporaryDirectory(prefix="fbs-build-") as tmp:
        doc_path = os.path.join(tmp, "document.json")
        with open(doc_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

        out_path = os.path.join(tmp, f"output.{fmt}")
        proc = _run_worker(
            ["build", "--input", doc_path, "--output", out_path, "--format", fmt],
            DEFAULT_TIMEOUT,
        )
        envelope = _read_envelope(out_path, proc)

        if not envelope.get("ok"):
            raise EngineError(envelope.get("error") or "font engine failed to build the font")

        if not os.path.exists(out_path):
            raise EngineError("font engine reported success but produced no file")

        with open(out_path, "rb") as fh:
            data = fh.read()

    return data, envelope.get("stats", {})


# The operation list lives in the worker (see font_worker.OP_SPECS) so there is
# a single source of truth. It is fetched once and cached; --list-ops needs no
# fontforge import, so this is cheap.
_OPERATIONS_CACHE: list[str] | None = None
_FALLBACK_OPERATIONS = [
    "addExtrema",
    "autoWidth",
    "correctDirection",
    "removeOverlap",
    "round",
    "simplify",
]


def list_operations() -> list[str]:
    global _OPERATIONS_CACHE
    if _OPERATIONS_CACHE is None:
        try:
            proc = _run_worker(["--list-ops"], DEFAULT_TIMEOUT)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            _OPERATIONS_CACHE = list(payload.get("operations") or _FALLBACK_OPERATIONS)
        except Exception:  # noqa: BLE001 - never break the UI over a probe
            _OPERATIONS_CACHE = list(_FALLBACK_OPERATIONS)
    return _OPERATIONS_CACHE


def apply_op(
    doc: dict, op: str, glyph_names: list[str] | None = None
) -> tuple[dict, dict]:
    """Apply a FontForge operation and return ``(document, info)``.

    ``glyph_names`` selects the glyphs to touch; ``None`` means the whole font.
    """
    font_model.validate_document(doc)

    with tempfile.TemporaryDirectory(prefix="fbs-op-") as tmp:
        doc_path = os.path.join(tmp, "document.json")
        with open(doc_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

        out_path = os.path.join(tmp, "result.json")
        args = ["op", "--input", doc_path, "--output", out_path, "--op", op]
        if glyph_names:
            args += ["--glyphs", ",".join(glyph_names)]

        proc = _run_worker(args, DEFAULT_TIMEOUT)
        envelope = _read_envelope(out_path, proc)

        if not envelope.get("ok"):
            raise EngineError(envelope.get("error") or f"font engine failed to run {op}")

        try:
            with open(out_path, encoding="utf-8") as fh:
                result = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(f"font engine produced an unreadable document: {exc}") from exc

    return font_model.validate_document(result), envelope
