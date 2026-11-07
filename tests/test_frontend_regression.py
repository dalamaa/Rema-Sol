from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not available")
def test_wallet_frontend_success_clears_progress_state():
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/frontend_wallet_regression.js"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
