"""The inline scripts of the workbench pages parse: a syntax error blanks the whole page."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "workbench"
PAGES = [SRC / "static" / "shell.html", SRC / "write" / "page.html"]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_inline_scripts_parse(page, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", page.read_text("utf-8"), re.S)
    assert scripts
    for i, code in enumerate(scripts):
        f = tmp_path / f"{page.stem}-{i}.js"
        f.write_text(code, "utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
