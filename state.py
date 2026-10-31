"""Bounded, schema-checked MacroForge state storage.

The caller supplies the application-support path. This module does not select
an install-specific directory or expose any HTTP routes.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 256_000
MAX_STEPS = 120
MAX_ITEMS = 30
SAFE_FIELDS = {"work-code", "summary"}
SAFE_ACTIONS = {"add-item", "mark-ready"}
SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,23}$", re.I)
SENSITIVE_VALUE = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\d{3}-\d{2}-\d{4}\b|"
    r"\b(?:\d[ -]?){13,19}\b|\b(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}\b|"
    r"\b(?:sk|pk|api|token)[-_][a-z0-9_-]{8,}\b",
    re.I,
)


class StateError(ValueError):
    """Base class for invalid, corrupt, or conflicting state."""


class StateCorruptError(StateError):
    """Persisted bytes are invalid; the source file is deliberately preserved."""


class StateConflictError(StateError):
    """A write used a stale revision or attempted a second bootstrap."""


class StateValidationError(StateError):
    """A state payload is outside the schema or safety limits."""


@dataclass(frozen=True)
class StateSnapshot:
    state: dict[str, Any]
    persisted: bool
    bootstrap_allowed: bool


def empty_state() -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": 0,
        "macroName": "Follow-up flow",
        "settings": {"maxLoops": 2},
        "steps": [],
        "items": [],
    }


def _string(value: Any, label: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise StateValidationError(f"{label} must be a string of at most {maximum} characters.")
    if any(ord(char) < 32 for char in value):
        raise StateValidationError(f"{label} cannot contain control characters.")
    return value


def _safe_value(value: str, label: str) -> None:
    if SENSITIVE_VALUE.search(value):
        raise StateValidationError(f"{label} resembles sensitive data and was rejected.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Saved state has duplicate object keys.")
        result[key] = value
    return result


def _validate_step(step: Any, index: int) -> dict[str, Any]:
    label = f"steps[{index}]"
    if not isinstance(step, dict):
        raise StateValidationError(f"{label} must be an object.")
    if step.get("target") != "crm-demo":
        raise StateValidationError(f"{label} target is not allowed.")
    kind = step.get("type")
    step_id = _string(step.get("id"), f"{label}.id", 64, allow_empty=False)
    if kind == "input":
        if set(step) != {"id", "type", "target", "field", "value"} or step.get("field") not in SAFE_FIELDS:
            raise StateValidationError(f"{label} input fields are not allowed.")
        value = _string(step.get("value"), f"{label}.value", 240)
        _safe_value(value, f"{label}.value")
        return {"id": step_id, "type": kind, "target": "crm-demo", "field": step["field"], "value": value}
    if kind == "click":
        action = step.get("action")
        allowed_keys = {"id", "type", "target", "action"}
        if action == "mark-ready":
            allowed_keys.add("key")
        if set(step) != allowed_keys or action not in SAFE_ACTIONS:
            raise StateValidationError(f"{label} click action is not allowed.")
        result = {"id": step_id, "type": kind, "target": "crm-demo", "action": action}
        if action == "mark-ready":
            key = _string(step.get("key"), f"{label}.key", 24, allow_empty=False)
            if not SAFE_CODE.fullmatch(key):
                raise StateValidationError(f"{label}.key is not a valid demo work code.")
            _safe_value(key, f"{label}.key")
            result["key"] = key
        return result
    if kind in {"wait", "scroll"}:
        if set(step) != {"id", "type", "target", "value"}:
            raise StateValidationError(f"{label} fields are not allowed.")
        value = step.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateValidationError(f"{label}.value must be an integer.")
        lower, upper = (100, 3000) if kind == "wait" else (0, 10000)
        if not lower <= value <= upper:
            raise StateValidationError(f"{label}.value is outside the allowed range.")
        return {"id": step_id, "type": kind, "target": "crm-demo", "value": value}
    raise StateValidationError(f"{label}.type is not allowed.")


def validate_state(value: Any) -> dict[str, Any]:
    """Return a canonical copy or reject the entire payload."""
    if not isinstance(value, dict):
        raise StateValidationError("State must be an object.")
    expected = {"schemaVersion", "revision", "macroName", "settings", "steps", "items"}
    if set(value) != expected:
        raise StateValidationError("State contains missing or unknown fields.")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != SCHEMA_VERSION:
        raise StateValidationError("Unsupported state schema version.")
    revision = value["revision"]
    if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
        raise StateValidationError("State revision must be a nonnegative safe integer.")
    name = _string(value["macroName"], "macroName", 60, allow_empty=False)
    _safe_value(name, "macroName")
    settings = value["settings"]
    if not isinstance(settings, dict) or set(settings) != {"maxLoops"}:
        raise StateValidationError("Settings contains missing or unknown fields.")
    loops = settings["maxLoops"]
    if type(loops) is not int or loops not in (1, 2, 3):
        raise StateValidationError("settings.maxLoops must be 1, 2, or 3.")
    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
        raise StateValidationError(f"steps must contain at most {MAX_STEPS} items.")
    steps = [_validate_step(item, index) for index, item in enumerate(raw_steps)]
    step_ids = [step["id"] for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise StateValidationError("Step IDs must be unique.")
    raw_items = value["items"]
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise StateValidationError(f"items must contain at most {MAX_ITEMS} items.")
    items = []
    codes: set[str] = set()
    for index, item in enumerate(raw_items):
        label = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != {"code", "summary", "status"}:
            raise StateValidationError(f"{label} fields are not allowed.")
        code = _string(item["code"], f"{label}.code", 24, allow_empty=False)
        if not SAFE_CODE.fullmatch(code) or code in codes:
            raise StateValidationError(f"{label}.code must be unique and valid.")
        _safe_value(code, f"{label}.code")
        summary = _string(item["summary"], f"{label}.summary", 100)
        _safe_value(summary, f"{label}.summary")
        status = item["status"]
        if status not in ("Open", "Ready"):
            raise StateValidationError(f"{label}.status is not allowed.")
        codes.add(code)
        items.append({"code": code, "summary": summary, "status": status})
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "revision": revision,
        "macroName": name,
        "settings": {"maxLoops": loops},
        "steps": steps,
        "items": items,
    }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise StateValidationError(f"State exceeds the {MAX_STATE_BYTES}-byte limit.")
    return result


class MacroStateStore:
    """Thread-safe store that atomically persists a validated state document."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _process_lock(self):
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise StateCorruptError("State lock is not a regular file.")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StateCorruptError("Saved state is not a regular file; it was preserved.")
        if info.st_size > MAX_STATE_BYTES:
            raise StateCorruptError("Saved state exceeds its size limit; it was preserved.")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self.path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise StateCorruptError("Saved state changed while opening; it was preserved.")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_STATE_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_STATE_BYTES:
                raise StateCorruptError("Saved state exceeds its size limit; it was preserved.")
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            return validate_state(parsed)
        except StateError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise StateCorruptError("Saved state cannot be read; it was preserved.") from exc

    def load(self) -> StateSnapshot:
        with self._lock:
            try:
                state = self._read()
            except FileNotFoundError:
                return StateSnapshot(empty_state(), persisted=False, bootstrap_allowed=True)
            except StateValidationError as exc:
                raise StateCorruptError(f"Saved state is invalid; it was preserved. {exc}") from exc
            return StateSnapshot(state, persisted=True, bootstrap_allowed=False)

    def bootstrap(self, state: Any) -> StateSnapshot:
        """Create state once, for a one-time same-origin legacy browser bootstrap."""
        with self._lock, self._process_lock():
            if self.path.exists() or self.path.is_symlink():
                raise StateConflictError("Saved state already exists; bootstrap was not applied.")
            canonical = validate_state(state)
            canonical["revision"] = 1
            self._write(canonical)
            return StateSnapshot(canonical, persisted=True, bootstrap_allowed=False)

    def save(self, state: Any, *, expected_revision: int) -> StateSnapshot:
        with self._lock, self._process_lock():
            try:
                current = self._read()
            except FileNotFoundError as exc:
                raise StateConflictError("Saved state has not been bootstrapped.") from exc
            except StateValidationError as exc:
                raise StateCorruptError(f"Saved state is invalid; it was preserved. {exc}") from exc
            if type(expected_revision) is not int or expected_revision != current["revision"]:
                raise StateConflictError("Saved state changed; reload before saving again.")
            canonical = validate_state(state)
            canonical["revision"] = current["revision"] + 1
            if canonical["revision"] > 2**53 - 1:
                raise StateValidationError("State revision limit was reached.")
            self._write(canonical)
            return StateSnapshot(canonical, persisted=True, bootstrap_allowed=False)

    def _write(self, state: dict[str, Any]) -> None:
        encoded = (json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise StateValidationError(f"State exceeds the {MAX_STATE_BYTES}-byte limit.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".macro-state-", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
