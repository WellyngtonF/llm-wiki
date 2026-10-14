"""Refused attempts the owner has looked at and accepted (ADR 0007).

A refused attempt doctor cannot prove resolved stayed an `error` for good: a lost
breadcrumb never comes back, and a compile retried after a fix may publish its
notes under other slugs. Acknowledging one records who decided, when, and why, in
one owner-only runtime file. Nothing is deleted: the transaction stays quarantined
and still blocks deleting `run/`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from reliable_memory import (
    capture_runtime_file_identity,
    publish_runtime_file,
    read_runtime_bytes,
    sha256_bytes,
)
from secret_redact import redact_secrets

FILE_NAME = "acknowledged-refusals.json"
SCHEMA_VERSION = "acknowledged-refusals/v1"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_REASON_CHARS = 500
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")


class AcknowledgementError(ValueError):
    """An acknowledgement that cannot be recorded as asked."""


def acknowledgements_path(state_root: Path) -> Path:
    return Path(state_root) / "run" / FILE_NAME


def read_acknowledged(state_root: Path) -> dict[str, dict[str, str]]:
    """The acknowledged attempts by transaction id; an absent file is none.

    A file that is present but unreadable raises: doctor must then keep every
    refused attempt a finding rather than guess which ones were accepted.
    """
    return _parsed(_current(state_root)[0])


def acknowledge(
    state_root: Path,
    transaction_ids: list[str],
    reason: str,
    *,
    now: datetime | None = None,
) -> int:
    """Record the given attempts as acknowledged; return how many were new."""
    note = _required_reason(reason)
    for identifier in transaction_ids:
        if _TRANSACTION_ID.fullmatch(identifier) is None:
            raise AcknowledgementError(f"not a transaction id: {identifier!r}")
    raw, identity = _current(state_root)
    entries = _parsed(raw)
    stamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    added = 0
    for identifier in transaction_ids:
        if identifier not in entries:
            entries[identifier] = {"acknowledged_at": stamp, "reason": note}
            added += 1
    if added:
        _publish(state_root, entries, raw, identity)
    return added


def _required_reason(reason: str) -> str:
    note = redact_secrets(" ".join(str(reason).split()))
    if not note:
        raise AcknowledgementError("an acknowledgement needs a reason")
    if len(note) > MAX_REASON_CHARS:
        raise AcknowledgementError(f"the reason is longer than {MAX_REASON_CHARS} characters")
    return note


def _current(state_root: Path) -> tuple[bytes | None, object]:
    path = acknowledgements_path(state_root)
    if not path.exists():
        return None, None
    identity = capture_runtime_file_identity(path, state_root=Path(state_root))
    return read_runtime_bytes(path, Path(state_root), max_bytes=MAX_FILE_BYTES), identity


def _parsed(raw: bytes | None) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise AcknowledgementError("acknowledged refusals file has an unknown schema")
    entries = document.get("acknowledged")
    if not isinstance(entries, dict) or not all(
        isinstance(key, str) and _TRANSACTION_ID.fullmatch(key) and isinstance(value, dict)
        for key, value in entries.items()
    ):
        raise AcknowledgementError("acknowledged refusals file is malformed")
    return entries


def _publish(
    state_root: Path, entries: dict[str, dict[str, str]], raw: bytes | None, identity: object
) -> None:
    data = (
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "acknowledged": dict(sorted(entries.items()))},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise AcknowledgementError("acknowledged refusals file would exceed its bound")
    publish_runtime_file(
        acknowledgements_path(state_root),
        data,
        state_root=Path(state_root),
        create_only=raw is None,
        expected=identity if raw is not None else None,
        expected_sha256=sha256_bytes(raw) if raw is not None else None,
    )
