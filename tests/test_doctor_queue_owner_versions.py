"""Doctor must read ownership rows after Reliability V3 adoption."""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doctor  # noqa: E402


@pytest.mark.parametrize("columns", [
    "token TEXT, role TEXT, pid INTEGER, expires_at TEXT",
    "owner_token TEXT, domain_role TEXT, process_id INTEGER, expires_at TEXT",
])
def test_counts_live_worker_with_either_ownership_schema(columns, monkeypatch):
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    try:
        database.execute(f"CREATE TABLE queue_ownership ({columns})")
        database.execute("INSERT INTO queue_ownership VALUES ('owner', 'worker', 123, '2099-01-01T00:00:00Z')")
        monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)
        details = {"live_workers": 0, "live_migrations": 0, "deletion_codes": []}
        doctor._count_queue_ownership(database, {"queue_ownership"}, details, datetime.now(timezone.utc))
        assert details == {"live_workers": 1, "live_migrations": 0, "deletion_codes": []}
    finally:
        database.close()
