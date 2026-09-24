"""A background worker must also keep ordinary child commands invisible."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_state  # noqa: E402


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console inheritance")
def test_worker_and_unflagged_descendant_share_a_hidden_console(tmp_path, monkeypatch):
    probe = """
import ctypes, json
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32')
user = ctypes.WinDLL('user32')
kernel.GetConsoleWindow.restype = wintypes.HWND
user.IsWindowVisible.argtypes = [wintypes.HWND]
window = kernel.GetConsoleWindow()
print(json.dumps({'window': window, 'visible': bool(user.IsWindowVisible(window))}))
"""
    # Capture our own console and a normal descendant's; no hidden flags on
    # the descendant. That is how git/Codex wrappers behave inside a worker.
    worker = """
import contextlib, io, json, subprocess, sys
from pathlib import Path
probe = sys.argv[2]
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    exec(probe)
own = json.loads(buffer.getvalue())
child = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True)
Path(sys.argv[1]).write_text(json.dumps([own, json.loads(child.stdout)]))
"""
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    result = tmp_path / "console.json"
    error = tmp_path / "error.log"
    assert memory_state.spawn_detached(
        [sys.executable, "-c", worker, str(result), probe], stderr_path=error
    ) is not None
    deadline = time.monotonic() + 15
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result.exists(), error.read_text()
    own, child = json.loads(result.read_text())
    assert own["window"], "a consoleless worker lets child commands allocate visible consoles"
    assert not own["visible"]
    assert child["window"] == own["window"]
    assert not child["visible"]
