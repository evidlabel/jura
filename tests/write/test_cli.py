# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import json
import sys

import pytest

from workbench.write.core import _ws, main


def run(capsys, *argv):
    main(list(argv))
    return capsys.readouterr().out


def test_print(capsys, tree):
    out = run(capsys, "print", str(tree))
    assert out.startswith("proj/") and "README.md" in out
    assert "\033[" in run(capsys, "print", str(tree), "--color")
    assert "lines" not in run(capsys, "print", str(tree), "--no-counts")


def test_print_not_a_folder(tmp_path):
    with pytest.raises(SystemExit, match="is not a folder"):
        main(["print", str(tmp_path / "nope")])


def test_annotations(capsys, tree):
    run(capsys, "annotation", "-C", str(tree), "set", "pkg", "the package")
    run(capsys, "annotation", "-C", str(tree), "set", "old", "stale note")
    assert run(capsys, "annotation", "-C", str(tree), "get", "pkg") == "the package\n"
    assert run(capsys, "annotation", "-C", str(tree), "ls") == "old\tstale note  (gone)\npkg\tthe package\n"
    run(capsys, "annotation", "-C", str(tree), "mv", "pkg", "lib")
    with pytest.raises(SystemExit) as e:
        main(["annotation", "-C", str(tree), "get", "pkg"])
    assert e.value.code == 1


def test_feedback(capsys, tree):
    r = str(tree)
    assert run(capsys, "fb", "-C", r, "add", "tidy", "pkg/a.py", "--lines", "1-2") == "#1\n"
    assert run(capsys, "fb", "-C", r, "add", "merge", "pkg/a.py", "pkg/b.py") == "#2\n"
    out = run(capsys, "fb", "-C", r, "ls")
    assert "#1 L1-2  open  pkg/a.py" in out and "    | def a():" in out and "    tidy" in out
    run(capsys, "fb", "-C", r, "reply", "1", "renamed\nand moved", "--done")
    assert "#1" not in run(capsys, "fb", "-C", r, "ls")
    out = run(capsys, "fb", "-C", r, "ls", "--all")
    assert "reply: renamed\n           and moved" in out
    run(capsys, "fb", "-C", r, "reopen", "1")
    run(capsys, "fb", "-C", r, "done", "2")
    run(capsys, "fb", "-C", r, "rm", "2")
    assert [f["id"] for f in _ws(r, "", [], False).read_feedback()] == [1]
    with pytest.raises(SystemExit, match="no feedback #7"):
        main(["fb", "-C", r, "rm", "7"])


def test_edits_without_window(capsys, tree):
    out = run(capsys, "edits", "-C", str(tree))
    assert "unsaved in the editor: none" in out and "saved in the editor: unknown" in out
    _ws(str(tree), "", [], False).set_draft("README.md", {"diff": "-a\n+b\n"})
    out = run(capsys, "edits", "-C", str(tree), "--url", "http://127.0.0.1:9")
    assert "README.md  (draft, just now)" in out and "    +b" in out and "no Write window at" in out


def test_context_without_window():
    with pytest.raises(SystemExit, match="no Write window"):
        main(["context", "--url", "http://127.0.0.1:9"])


def test_schema(capsys):
    with pytest.raises(SystemExit):
        main(["-j"])
    out = capsys.readouterr().out
    assert json.loads(out)["name"] == "write"


def test_module_entry(tree):
    import subprocess

    r = subprocess.run(
        [sys.executable, "-m", "workbench.write", "print", str(tree), "--no-counts"], capture_output=True, text=True, check=True
    )
    assert "pkg/" in r.stdout
