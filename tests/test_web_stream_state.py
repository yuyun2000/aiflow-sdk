from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_assistant_stream_state_machine() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests" / "assistant_stream_state.test.cjs")],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
