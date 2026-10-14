# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import json
import os
import time

import pytest

from conftest import git
from workbench.write.core import HTTPError, Mounts, Workspace, _ws


def names(node):
    return [c["name"] for c in node["children"]]


def find(node, path):
    if node["path"] == path:
        return node
    for c in node.get("children") or []:
        hit = find(c, path)
        if hit:
            return hit
    return None


def test_scan(ws, tree):
    os.symlink("pkg/a.py", tree / "link.py")
    os.symlink("missing", tree / "broken")
    os.symlink("pkg", tree / "pkglink")
    (tree / "__pycache__").mkdir()
    t = ws.scan()
    assert names(t)[:3] == ["empty", "pkg", "pkglink"]
    assert "__pycache__" not in names(t)
    assert find(t, "README.md")["words"] == 7
    assert find(t, "pkg/a.py")["lines"] == 6
    assert find(t, "blob.bin")["kind"] == "binary"
    assert find(t, "link.py")["link"] == "pkg/a.py"
    assert find(t, "broken")["type"] == "broken"
    assert find(t, "pkglink")["linked"] is True


def test_symlink_outside_root(ws, tree, tmp_path):
    out = tmp_path / "outside.txt"
    out.write_text("secret\n")
    os.symlink(out, tree / "out")
    assert find(ws.scan(), "out")["kind"] == "outside"
    with pytest.raises(HTTPError) as e:
        ws.read_file("out")
    assert e.value.status == 403


def test_large_file(ws, tree, monkeypatch):
    monkeypatch.setattr("workbench.write.core.MAX_TEXT", 10)
    (tree / "big.txt").write_text("x" * 50)
    assert find(ws.scan(), "big.txt")["kind"] == "large"
    assert ws.read_file("big.txt")["kind"] == "large"


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0P\x0f\x00\x04\x85\x01\x80\x84\xa9\x8c!\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_plots(ws, tree):
    (tree / "fig.png").write_bytes(PNG)
    (tree / "FIG.SVG").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"></svg>\n')
    (tree / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    (tree / "link.png").symlink_to("fig.png")
    scanned = ws.scan()
    png = find(scanned, "fig.png")
    assert png["kind"] == "plot" and png["mime"] == "image/png" and "content" not in png and "words" not in png
    assert find(scanned, "FIG.SVG")["mime"] == "image/svg+xml"
    assert find(scanned, "doc.pdf")["kind"] == "plot"
    assert find(scanned, "link.png")["kind"] == "plot"
    got = ws.read_file("fig.png")
    assert got["kind"] == "plot" and got["mime"] == "image/png" and "content" not in got and got["size"] == len(PNG)
    svg = ws.read_file("FIG.SVG")
    assert svg["content"].startswith("<svg") and svg["mime"] == "image/svg+xml"
    assert ws.raw_file("fig.png") == (PNG, "image/png")
    assert ws.raw_file("link.png")[0] == PNG
    with pytest.raises(HTTPError) as e:
        ws.raw_file("README.md")
    assert e.value.status == 415
    (tree / "bad.svg").write_bytes(b"<svg>\0</svg>")
    assert "content" not in ws.read_file("bad.svg")
    with pytest.raises(HTTPError) as e:
        ws.raw_file("missing.png")
    assert e.value.status == 404
    (tree / "dir.pdf").mkdir()
    with pytest.raises(HTTPError) as e:
        ws.raw_file("dir.pdf")
    assert e.value.status == 400
    assert "(plot," in ws.export()


def test_plot_too_large(ws, tree, monkeypatch):
    monkeypatch.setattr("workbench.write.core.RAW_MAX", 8)
    (tree / "big.png").write_bytes(PNG)
    assert find(ws.scan(), "big.png")["kind"] == "plot"
    with pytest.raises(HTTPError) as e:
        ws.raw_file("big.png")
    assert e.value.status == 413


def test_plot_larger_than_text(ws, tree, monkeypatch):
    monkeypatch.setattr("workbench.write.core.MAX_TEXT", 4)
    (tree / "wide.png").write_bytes(PNG)
    assert find(ws.scan(), "wide.png")["kind"] == "plot"
    assert ws.read_file("wide.png")["kind"] == "plot"


def test_change_tracking(ws, tree):
    ws.scan()
    p = tree / "pkg" / "b.py"
    p.write_text("x = 1\ny = 2\n")
    os.utime(p, (time.time() + 5, time.time() + 5))
    ws.scan()
    assert ws.changes["pkg/b.py"]["by"] == "agent"
    assert ws.line_changes["pkg/b.py"][-1]["ranges"] == [[2, 2, "add"]]
    p.write_text("y = 2\n")
    os.utime(p, (time.time() + 10, time.time() + 10))
    ws.scan()
    assert ws.line_changes["pkg/b.py"][-1]["ranges"] == [[1, 1, "del"]]
    p.unlink()
    ws.scan()
    assert "pkg/b.py" not in ws.changes


def test_later_edit_keeps_earlier_line_age(ws, tree):
    ws.scan()
    p = tree / "pkg" / "b.py"
    p.write_text("x = 1\ny = 2\n")
    past = time.time() - 200
    os.utime(p, (past, past))
    ws.scan()
    first = ws.line_changes["pkg/b.py"][0]
    assert ws.changes["pkg/b.py"]["at"] == first["at"]
    p.write_text("x = 1\ny = 2\nz = 3\n")
    os.utime(p, None)
    ws.scan()
    assert ws.line_changes["pkg/b.py"][0]["at"] == first["at"]
    assert ws.line_changes["pkg/b.py"][0]["ranges"] == [[2, 2, "add"]]
    assert ws.changes["pkg/b.py"]["at"] == ws.line_changes["pkg/b.py"][-1]["at"]
    assert ws.changes["pkg/b.py"]["at"] != first["at"]


def test_touch_does_not_refresh_line_age(ws, tree):
    ws.scan()
    p = tree / "pkg" / "b.py"
    p.write_text("x = 1\ny = 2\n")
    past = time.time() - 200
    os.utime(p, (past, past))
    ws.scan()
    at = ws.line_changes["pkg/b.py"][0]["at"]
    os.utime(p, None)
    ws.scan()
    assert ws.line_changes["pkg/b.py"][0]["at"] == at
    assert ws.changes["pkg/b.py"]["at"] == at


def test_history_reloads_and_plot_is_file_level(ws, tree):
    (tree / "fig.png").write_bytes(PNG)
    ws.scan()
    p = tree / "pkg" / "b.py"
    p.write_text("x = 1\ny = 2\n")
    os.utime(p, None)
    (tree / "fig.png").write_bytes(PNG + b"\n")
    os.utime(tree / "fig.png", None)
    ws.scan()
    assert ws.history_path.is_file()
    assert "fig.png" not in ws.line_changes
    again = _ws(str(tree), "", [], False)
    assert again.line_changes["pkg/b.py"][-1]["ranges"] == [[2, 2, "add"]]
    assert again.changes["pkg/b.py"]["by"] == "agent"
    assert any(e["path"] == "fig.png" and e["ranges"] == [] for e in again.history())
    again.scan()  # a restart's first look is a baseline: it must not record the same edit twice
    assert len(again.line_changes["pkg/b.py"]) == len(ws.line_changes["pkg/b.py"])


def test_version_changes(ws, tree):
    v = ws.version()
    assert v == ws.version()
    (tree / "new.txt").write_text("hi")
    assert ws.version() != v


def test_notes(ws, tree):
    ws.set_note("pkg", "the package")
    ws.set_note(".", "root note")
    ws.set_note("pkg/a.py", "module a")
    assert ws.read_notes() == {".": "root note", "pkg": "the package", "pkg/a.py": "module a"}
    ws.move_notes("pkg", "lib")
    assert ws.read_notes() == {".": "root note", "lib": "the package", "lib/a.py": "module a"}
    ws.drop_notes("lib")
    assert ws.read_notes() == {".": "root note"}
    ws.set_note(".", "")
    assert json.loads(ws.notes_path.read_text()) == {}


def test_move_notes_merges(ws):
    ws.set_note("a", "one")
    ws.set_note("b", "two")
    ws.move_notes("a", "b")
    assert ws.read_notes() == {"b": "two\n\none"}


def test_bad_notes_file(ws):
    ws.notes_path.write_text("{not json")
    with pytest.raises(HTTPError) as e:
        ws.read_notes()
    assert e.value.status == 500


def test_feedback_lifecycle(ws):
    e = ws.add_feedback(["pkg/a.py"], "rename b", lines=[5, 4], quote="\ndef b():")
    assert e["id"] == 1 and e["lines"] == [4, 5] and e["status"] == "open"
    e2 = ws.add_feedback(["pkg", "README.md"], "merge these")
    assert e2["id"] == 2 and e2["paths"] == ["README.md", "pkg"]
    assert ws.update_feedback(1, reply="done it", status="done")["status"] == "done"
    with pytest.raises(HTTPError):
        ws.update_feedback(1, status="maybe")
    with pytest.raises(HTTPError):
        ws.update_feedback(9, reply="x")
    ws.set_note("pkg", "kept alongside feedback")
    data = json.loads(ws.notes_path.read_text())
    assert set(data) == {"notes", "feedback"}
    ws.delete_feedback(1)
    assert [f["id"] for f in ws.read_feedback()] == [2]
    with pytest.raises(HTTPError):
        ws.delete_feedback(1)
    ws.move_notes("pkg", "lib")
    assert ws.read_feedback()[0]["paths"] == ["README.md", "lib"]


@pytest.mark.parametrize("paths,text,lines", [([], "x", None), (["a"], "  ", None), (["a", "b"], "x", [1, 2])])
def test_feedback_validation(ws, paths, text, lines):
    with pytest.raises(HTTPError) as e:
        ws.add_feedback(paths, text, lines)
    assert e.value.status == 400


def test_refresh_anchors(ws, tree):
    ws.add_feedback(["pkg/a.py"], "look", lines=[5, 6], quote="def b():\n    return 2")
    p = tree / "pkg" / "a.py"
    p.write_text("# header\n" + p.read_text())
    assert ws.refresh_anchors()[0]["lines"] == [6, 7]
    p.write_text("gone\n")
    f = ws.refresh_anchors()[0]
    assert f["stale"] is True
    p.unlink()
    assert ws.refresh_anchors()[0]["stale"] is True


def test_read_write_file(ws, tree):
    f = ws.read_file("pkg/b.py")
    assert f["content"] == "x = 1\n" and f["kind"] == "text"
    out = ws.write_file("pkg/b.py", "x = 2\n", f["hash"], False)
    assert (tree / "pkg" / "b.py").read_text() == "x = 2\n"
    with pytest.raises(HTTPError) as e:
        ws.write_file("pkg/b.py", "x = 3\n", f["hash"], False)
    assert e.value.status == 409 and e.value.extra["disk"]["hash"] == out["hash"]
    ws.write_file("pkg/b.py", "x = 3\n", None, True)
    assert (tree / "pkg" / "b.py").read_text() == "x = 3\n"
    assert ws.read_file("blob.bin")["kind"] == "binary"
    for bad, status in (("nope", 404), ("pkg", 400)):
        with pytest.raises(HTTPError) as e:
            ws.read_file(bad)
        assert e.value.status == status
    with pytest.raises(HTTPError):
        ws.write_file("", "x", None, True)


def test_crlf_preserved(ws, tree):
    (tree / "win.txt").write_bytes(b"a\r\nb\r\n")
    f = ws.read_file("win.txt")
    assert f["crlf"] and f["content"] == "a\nb\n"
    ws.write_file("win.txt", "a\nc\n", f["hash"], False)
    assert (tree / "win.txt").read_bytes() == b"a\r\nc\r\n"


def test_create_rename_delete(ws, tree):
    assert ws.create("docs/new.md", "file") is False  # not a git repository
    ws.create("lib", "dir")
    assert (tree / "docs" / "new.md").is_file() and (tree / "lib").is_dir()
    with pytest.raises(HTTPError):
        ws.create("lib", "dir")
    with pytest.raises(HTTPError):
        ws.create("", "file")
    ws.set_note("docs/new.md", "a note")
    ws.set_draft("docs/new.md", {"base_hash": "x", "diff": "@@"})
    ws.rename("docs", "guide")
    assert (tree / "guide" / "new.md").is_file()
    assert ws.read_notes() == {"guide/new.md": "a note"}
    assert list(ws.read_drafts()) == ["guide/new.md"]
    for src, dst, status in (("", "x", 400), ("guide", "guide/x", 400), ("nope", "x", 404), ("guide", "lib", 409)):
        with pytest.raises(HTTPError) as e:
            ws.rename(src, dst)
        assert e.value.status == status
    ws.delete("guide")
    ws.delete("README.md")
    assert not (tree / "guide").exists() and not (tree / "README.md").exists()
    assert ws.read_notes() == {} and ws.read_drafts() == {}
    with pytest.raises(HTTPError):
        ws.delete("guide")
    with pytest.raises(HTTPError):
        ws.delete("")


def test_drafts(ws):
    assert ws.read_drafts() == {}
    ws.set_draft("a.txt", {"diff": "-a\n+b\n"})
    ws.set_draft("b.txt", {"full": "x"})
    assert set(ws.read_drafts()) == {"a.txt", "b.txt"}
    ws.set_draft("a.txt", None)
    ws.set_draft("a.txt", None)
    ws.set_draft("b.txt", None)
    assert not ws.drafts_path.exists()
    with pytest.raises(HTTPError):
        ws.set_draft("", {"diff": ""})
    ws.drafts_path.parent.mkdir(parents=True, exist_ok=True)
    ws.drafts_path.write_text("[]")
    assert ws.read_drafts() == {}


def test_outside_root(ws):
    with pytest.raises(HTTPError):
        ws.node_path("../x")


def test_export(ws, tree):
    ws.set_note(".", "root\nsecond line")
    ws.set_note("pkg/a.py", "module a")
    ws.set_note("gone.txt", "old")
    ws.add_feedback(["README.md"], "shorter please")
    ws.add_feedback(["pkg/a.py", "pkg/b.py"], "merge")
    done = ws.add_feedback(["pkg/b.py"], "done one")
    ws.update_feedback(done["id"], status="done")
    out = ws.export()
    assert out.startswith("proj/")
    assert "# root" in out and "# second line" in out
    assert "a.py  6 lines, 8 words" in out
    assert "# ! #1 shorter please" in out
    assert "#   ! #2 pkg/a.py, pkg/b.py: merge" in out
    assert "done one" not in out
    assert "#   gone.txt: old" in out
    assert "(binary, 4 bytes)" in out
    assert "\033[" in ws.export(color=True)
    assert "lines" not in ws.export(counts=False)


def test_export_skill_tree(ws, tree):
    (tree / "SKILL.md").write_text("---\nname: x\n---\n" + "word " * 100)
    out = ws.export()
    assert "tokens" in out and "# tokens (~4 chars each)" in out


def test_gitignore_and_show(repo):
    from workbench.write.core import _ws

    t = _ws(str(repo), "", [], False).scan()
    assert find(t, "debug.log") is None
    assert find(t, "notes/todo.md") is not None
    t = _ws(str(repo), "", [], False, show=[]).scan()
    assert find(t, "notes") is None
    t = _ws(str(repo), "", [], False, no_gitignore=True).scan()
    assert find(t, "debug.log") is not None


def test_git_history(repo):
    from workbench.write.core import _ws

    ws = _ws(str(repo), "", [], False)
    (repo / "README.md").write_text("changed\n")
    log = ws.git_log("README.md", False, 10)
    assert [c["subject"] for c in log["commits"]] == ["init"]
    assert log["status"] == [" M README.md"] and log["scope"] == "README.md"
    assert ws.git_log("", True, 10)["scope"] == "repository"
    diff = ws.git_show("README.md", False, log["commits"][0]["short"])
    assert "A small project" in diff
    with pytest.raises(HTTPError):
        ws.git_show("", True, "not-a-hash")
    with pytest.raises(HTTPError):
        ws.git_show("", True, "deadbeef")
    assert ws.create("added.txt", "file") is True
    assert "A  added.txt" in git(repo, "status", "--short")


def test_grep_walks_when_there_is_no_git(ws, tree):
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "hidden.py").write_text("return 1\n")
    (tree / "bin.dat").write_bytes(b"return 1\0rest")
    (tree / ".treenotes.json").write_text("return 1\n")
    (tree / "dots.txt").write_text("a.b\naxb\n")
    (tree / "long.txt").write_text(("word " * 80) + "zephyr\n")
    (tree / "many.txt").write_text("".join(f"hit {i}\n" for i in range(5)))
    out = ws.grep("return 1")
    assert out["truncated"] is False
    assert [(h["path"], h["line"]) for h in out["hits"]] == [("pkg/a.py", 2)]
    assert ws.grep("RETURN 1")["hits"] == out["hits"]
    assert ws.grep("RETURN 1", case=True)["hits"] == []
    assert [h["line"] for h in ws.grep("a.b")["hits"]] == [1]
    long = ws.grep("zephyr")["hits"]
    assert long[0]["path"] == "long.txt" and long[0]["text"].startswith("…") and long[0]["text"].endswith("zephyr")
    capped = ws.grep("hit", limit=2)
    assert [h["line"] for h in capped["hits"]] == [1, 2] and capped["truncated"] is True
    with pytest.raises(HTTPError):
        ws.grep("  ")
    with pytest.raises(HTTPError):
        ws.grep("x" * 401)


def test_grep_respects_gitignore_and_notes(repo):
    from workbench.write.core import _ws

    (repo / "notes" / "only-here.md").write_text("zephyr token\n")
    (repo / "flag.txt").write_text("use -n here\n")
    ws = _ws(str(repo), "", [], False)
    assert [(h["path"], h["line"]) for h in ws.grep("return")["hits"]] == [("pkg/a.py", 2), ("pkg/a.py", 6)]
    assert ws.grep("noise")["hits"] == []
    assert [h["path"] for h in ws.grep("zephyr")["hits"]] == ["notes/only-here.md"]
    assert ws.grep("-n")["hits"][0]["path"] == "flag.txt"
    assert _ws(str(repo), "", [], False, show=[]).grep("zephyr")["hits"] == []
    assert [h["path"] for h in _ws(str(repo), "", [], False, no_gitignore=True).grep("noise")["hits"]] == ["debug.log"]
    git(repo / "notes", "init", "-q")
    (repo / "notes" / ".gitignore").write_text("secret.md\n")
    (repo / "notes" / "secret.md").write_text("zephyr hidden\n")
    (repo / "notes" / "open.md").write_text("zephyr shown\n")
    assert [h["path"] for h in _ws(str(repo), "", [], False).grep("zephyr")["hits"]] == [
        "notes/only-here.md",
        "notes/open.md",
    ]


def test_git_log_outside_repo(ws):
    with pytest.raises(HTTPError) as e:
        ws.git_log("", True, 5)
    assert e.value.status == 404


def test_custom_notes_path(tree, tmp_path):
    ws = Workspace(tree, tmp_path / "elsewhere.json", [], False)
    ws.set_note("pkg", "x")
    assert (tmp_path / "elsewhere.json").exists()


def test_notes_folder_holds_the_file(tree):
    (tree / "notes").mkdir()
    (tree / ".treenotes.json").write_text('{"pkg": "keep"}\n')
    ws = _ws(str(tree), "", [], False)
    assert ws.notes_path == (tree / "notes" / ".treenotes.json").resolve()
    assert json.loads(ws.notes_path.read_text()) == {"pkg": "keep"}
    assert not (tree / ".treenotes.json").exists()
    assert find(ws.scan(), "notes/.treenotes.json") is None


def test_new_notes_go_into_the_notes_folder(tree):
    (tree / "notes").mkdir()
    ws = _ws(str(tree), "", [], False)
    assert not ws.notes_path.exists()
    ws.set_note(".", "hi")
    assert ws.notes_path == (tree / "notes" / ".treenotes.json").resolve()
    assert not (tree / ".treenotes.json").exists()


def test_both_notes_files_keep_the_root_one(tree):
    (tree / "notes").mkdir()
    (tree / ".treenotes.json").write_text('{".": "root"}\n')
    (tree / "notes" / ".treenotes.json").write_text('{".": "inner"}\n')
    ws = _ws(str(tree), "", [], False)
    assert ws.notes_path == (tree / ".treenotes.json").resolve()
    assert ws.read_notes() == {".": "root"}


def test_explicit_notes_path_ignores_the_notes_folder(tree, tmp_path):
    (tree / "notes").mkdir()
    ws = _ws(str(tree), str(tmp_path / "elsewhere.json"), [], False)
    ws.set_note("pkg", "x")
    assert (tmp_path / "elsewhere.json").is_file()
    assert not (tree / "notes" / ".treenotes.json").exists()


def test_product_git_does_not_see_the_notes_file(repo):
    ws = _ws(str(repo), "", [], False)
    ws.set_note("pkg", "x")
    assert ".treenotes.json" not in git(repo, "status", "--short")


def test_shared_top_notes_follow_a_notes_folder(tree, tmp_path):
    (tmp_path / "notes").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_text("a\n")
    mounts = Mounts([_ws(str(tree), "", [], False), _ws(str(other), "", [], False)])
    mounts.set_note("", "together")
    assert (tmp_path / "notes" / ".treenotes.json").is_file()
    assert not (tmp_path / ".treenotes.json").exists()
    assert mounts.read_notes()["."] == "together"
