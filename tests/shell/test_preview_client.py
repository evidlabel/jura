"""Preview startup races and edit delivery, without a live browser or compiler."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_preview_client():
    script = Path(__file__).with_name("preview_client.cjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
