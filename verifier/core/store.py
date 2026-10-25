"""Store core: the only JSON writer/hasher and the only writer under `runs/` (design doc 8.1, 7.1).

Rules (J1):
- Canonical JSON: `json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\\n"`, UTF-8.
- Write-once: writing an existing path raises `WriteOnceError`, unless the bytes are identical
  (then nothing happens). `meta.json` helpers (the only way to replace a file) come in J7.
- Atomic: each write goes to a temp file in the same folder, then is hard-linked into place.
- Validated by file name (see `schema_name_for`); other files are not validated.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from verifier.contracts.errors import WriteOnceError

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "contracts" / "schemas"


# ── JSON, hashes, time ────────────────────────────────────────────────────────

def dump_json(obj: Any) -> bytes:
    """Canonical JSON bytes (design doc 8.1)."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_json(obj: Any) -> str:
    return sha256_bytes(dump_json(obj))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    """UTC, whole seconds, trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Run folders ───────────────────────────────────────────────────────────────

def _runs_dir() -> Path:
    try:
        from verifier.contracts.config import env  # lane A's config.py
        value = env("RUNS_DIR")
    except ImportError:  # config.py not there yet
        value = os.environ.get("RUNS_DIR") or "runs"
    return Path(value or "runs")


def run_dir(application_id: str) -> Path:
    """`{RUNS_DIR}/{application_id}`, created if missing."""
    path = _runs_dir() / str(application_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Validation ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    path = SCHEMAS_DIR / f"{schema_name}.schema.json"
    if not path.is_file():
        raise ValueError(f"unknown schema: {schema_name!r}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def validate(obj: Any, schema_name: str) -> None:
    """Raise `jsonschema.ValidationError` if `obj` doesn't match `schema_name`."""
    _validator(schema_name).validate(obj)


_CLAIMS_RE = re.compile(r"^claims\.v\d+\.json$")
_VERDICT_RE = re.compile(r"^verdict_\d+\.json$")


def schema_name_for(path: Path) -> str | None:
    """Schema a stored file is validated against, chosen by its name; None = not validated."""
    path = Path(path)
    name, parent = path.name, path.parent.name
    if _CLAIMS_RE.match(name):
        return "claims"
    if name == "evidence.json":
        return "evidence"
    if parent == "run_traces" and name.endswith(".json"):
        return "run_trace"
    if parent == "verdicts" and _VERDICT_RE.match(name):
        return "verdict"
    if parent == "generated" and name.endswith(".json"):
        return "generated"
    return None


# ── Writing (write-once, atomic) ──────────────────────────────────────────────

def write_bytes(path: Path, data: bytes) -> str:
    """Write-once, atomic. Returns the sha256 of `data`.

    An existing file with identical bytes is left alone; different bytes raise `WriteOnceError`.
    """
    path = Path(path)
    digest = sha256_bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _check_existing() -> str:
        if path.read_bytes() == data:
            return digest
        raise WriteOnceError(f"refusing to overwrite {path}")

    if path.exists():
        return _check_existing()

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600
        try:
            os.link(tmp, path)  # fails if `path` appeared meanwhile: never overwrites
        except FileExistsError:
            return _check_existing()
    finally:
        tmp.unlink(missing_ok=True)
    return digest


def write_text(path: Path, text: str) -> str:
    return write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, obj: Any) -> str:
    """Validate by file name, then write canonical JSON write-once. Returns its sha256."""
    schema_name = schema_name_for(path)
    if schema_name is not None:
        validate(obj, schema_name)
    return write_bytes(path, dump_json(obj))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── Version counters ──────────────────────────────────────────────────────────

def _next_number(folder: Path, pattern: re.Pattern[str]) -> int:
    highest = 0
    if folder.is_dir():
        for entry in folder.iterdir():
            m = pattern.match(entry.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def next_claims_version(run_dir: Path) -> int:
    """1 + the highest `claims.vN.json` in the run folder (1 if none)."""
    return _next_number(Path(run_dir), re.compile(r"^claims\.v(\d+)\.json$"))


def next_verdict_n(run_dir: Path) -> int:
    """1 + the highest `verdicts/verdict_N.json` (1 if none)."""
    return _next_number(Path(run_dir) / "verdicts", re.compile(r"^verdict_(\d+)\.json$"))


# ── meta.json: the only file that is ever replaced (J7, design doc 7.2) ──────

def meta_path(application_id: str) -> Path:
    """`{RUNS_DIR}/{application_id}/meta.json` (does not create anything)."""
    return _runs_dir() / str(application_id) / "meta.json"


@contextmanager
def _meta_lock(application_id: str) -> Iterator[None]:
    """Exclusive per-application file lock (flock on `.meta.lock`), across threads and processes."""
    fd = os.open(run_dir(application_id) / ".meta.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _replace_meta(application_id: str, meta: Any) -> None:
    """Validate, then replace `meta.json` atomically (temp file, then `os.replace`)."""
    validate(meta, "meta")
    path = meta_path(application_id)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".meta.json.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(dump_json(meta))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_meta(application_id: str) -> dict:
    """The current `meta.json` (always a complete file, because writes are atomic replaces).
    Raises FileNotFoundError if the application has none."""
    return json.loads(meta_path(application_id).read_text(encoding="utf-8"))


def init_meta(application_id: str, meta: dict) -> tuple[dict, bool]:
    """Create `meta.json` unless it exists. Returns (meta on disk, created)."""
    with _meta_lock(application_id):
        if meta_path(application_id).is_file():
            return read_meta(application_id), False
        _replace_meta(application_id, meta)
        return copy.deepcopy(meta), True


def update_meta(application_id: str, fn: Callable[[dict], dict | None]) -> dict:
    """Read, apply `fn` to a copy (it mutates in place, or returns a replacement), validate,
    replace atomically, all under the per-application file lock. Returns the new meta.
    `fn` must not call `update_meta` itself."""
    with _meta_lock(application_id):
        new = copy.deepcopy(read_meta(application_id))
        result = fn(new)
        if result is not None:
            new = result
        _replace_meta(application_id, new)
        return new


def list_metas() -> list[dict]:
    """Every application's meta (folders starting with `_` are runtime files, not applications)."""
    root = _runs_dir()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("_") and (child / "meta.json").is_file():
            try:
                out.append(json.loads((child / "meta.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out
