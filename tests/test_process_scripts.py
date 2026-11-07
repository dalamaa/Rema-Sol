from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_process_scripts_are_executable_and_target_only_remasol():
    start = ROOT / "start.sh"
    stop = ROOT / "stop.sh"
    assert os.access(start, os.X_OK)
    assert os.access(stop, os.X_OK)
    assert subprocess.run(["bash", "-n", str(start)]).returncode == 0
    assert subprocess.run(["bash", "-n", str(stop)]).returncode == 0

    start_text = start.read_text()
    stop_text = stop.read_text()
    assert '"$ROOT_DIR/app.py"' in start_text
    assert 'export PORT=5007' in start_text
    assert 'process_is_remasol' in start_text
    assert 'kill -TERM "$pid"' in stop_text
    assert '"$ROOT_DIR/app.py"' in stop_text
    assert "pkill" not in start_text + stop_text
    assert "kill -9" not in start_text + stop_text
