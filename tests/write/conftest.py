# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import subprocess

import pytest

from workbench.write.core import _ws


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path_factory, monkeypatch):
    """Keep drafts and skill installs out of the real home folder."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.delenv("WORKBENCH_WRITE_URL", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def tree(tmp_path):
    """A small project: a README, a package with two modules, a binary file and an empty folder."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "README.md").write_text("# proj\n\nA small project for tests.\n")
    (root / "pkg" / "a.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    (root / "pkg" / "b.py").write_text("x = 1\n")
    (root / "blob.bin").write_bytes(b"\0\1\2\3")
    return root


@pytest.fixture
def ws(tree):
    return _ws(str(tree), "", [], False)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tree):
    git(tree, "init", "-q")
    git(tree, "config", "user.email", "test@example.com")
    git(tree, "config", "user.name", "test")
    (tree / ".gitignore").write_text("*.log\nnotes/\n")
    (tree / "debug.log").write_text("noise\n")
    (tree / "notes").mkdir()
    (tree / "notes" / "todo.md").write_text("- x\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "init")
    return tree
