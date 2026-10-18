# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import threading
from pathlib import Path

import pytest

from test_server import call
from workbench.write.core import Handler, HTTPError, Mounts, Server, _ws, main, mount_names


@pytest.fixture
def two(tree, tmp_path):
    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    (other / "docs" / "guide.md").write_text("# Guide\n\nRead me.\n")
    return Mounts([_ws(str(tree), "", [], False), _ws(str(other), "", [], False)])


def test_mount_names():
    assert mount_names([Path("/a/x"), Path("/b/y")]) == ["x", "y"]
    assert mount_names([Path("/a/x"), Path("/b/x"), Path("/b/x")]) == ["x", "b-x", "b-x-2"]
    assert mount_names([Path("/")]) == ["root"]


def test_tree_and_files(two, tree, tmp_path):
    t = two.scan()
    assert t["multi"] and [c["name"] for c in t["children"]] == ["proj", "other"]
    assert t["children"][1]["mount"] == str(tmp_path / "other")
    assert t["children"][1]["children"][0]["children"][0]["path"] == "other/docs/guide.md"
    assert two.root == tmp_path and two.label == "proj + other"
    f = two.read_file("other/docs/guide.md")
    assert f["path"] == "other/docs/guide.md"
    two.write_file("other/docs/guide.md", "new\n", f["hash"], False)
    with pytest.raises(HTTPError) as e:
        two.write_file("other/docs/guide.md", "x", f["hash"], False)
    assert e.value.extra["disk"]["path"] == "other/docs/guide.md"
    v = two.version()
    two.write_file("proj/pkg/b.py", "y = 2\n", None, True)
    assert two.version() != v
    for bad in ("", "nope/x", "../x"):
        with pytest.raises(HTTPError):
            two.read_file(bad)


def test_grep_prefixes_the_folder(two):
    assert [h["path"] for h in two.grep("Read me")["hits"]] == ["other/docs/guide.md"]
    assert [h["path"] for h in two.grep("return 1")["hits"]] == ["proj/pkg/a.py"]


def test_notes_and_feedback_stay_per_folder(two, tree, tmp_path):
    two.set_note("proj/pkg", "package")
    two.set_note("other", "the other root")
    assert two.read_notes() == {"proj/pkg": "package", "other": "the other root"}
    assert (tmp_path / "other" / ".juranotes.json").exists()
    two.move_notes("proj/pkg", "proj/lib")
    with pytest.raises(HTTPError):
        two.move_notes("proj/lib", "other/lib")
    f = two.add_feedback(["other/docs/guide.md"], "shorter", lines=[1, 1], quote="# Guide")
    assert f["id"] == "other:1" and f["num"] == 1 and f["paths"] == ["other/docs/guide.md"]
    assert f["root"] == str(tmp_path / "other")
    assert two.add_feedback(["proj"], "tidy")["id"] == "proj:1"
    assert two.update_feedback("other:1", status="done")["status"] == "done"
    with pytest.raises(HTTPError):
        two.add_feedback([], "none")
    for bad in ("x:1", "proj:x", "7"):
        with pytest.raises(HTTPError):
            two.update_feedback(bad, status="done")
    two.delete_feedback("proj:1")
    assert [x["id"] for x in two.refresh_anchors()] == ["other:1"]
    out = two.export()
    assert out.startswith("proj/") and "\nother/" in out


def test_mutations_and_drafts(two, tree, tmp_path):
    two.create("other/new.txt", "file")
    two.rename("other/new.txt", "other/docs/new.txt")
    two.set_draft("other/docs/new.txt", {"diff": "+x"})
    assert list(two.read_drafts()) == ["other/docs/new.txt"]
    two.set_draft("other/docs/new.txt", None)
    two.delete("other/docs/new.txt")
    two.mark_ui("proj/README.md")
    for call_, args in (
        (two.rename, ("other", "proj/other")),
        (two.delete, ("other",)),
        (two.rename, ("other/docs", "proj/docs")),
        (two.create, ("top.txt", "file")),
    ):
        with pytest.raises(HTTPError):
            call_(*args)
    assert two.changes == {} and two.line_changes == {}
    assert two.changes_for(str(tmp_path / "other")) == (str(tmp_path / "other"), {})
    assert two.changes_for("/nowhere")[0] == "proj + other"


def test_git_per_folder(two, repo):
    log = two.git_log("proj/README.md", False, 5)
    assert log["scope"] == "proj/README.md" and log["commits"][0]["subject"] == "init"
    assert "init" in two.git_show("proj", True, log["commits"][0]["short"])
    with pytest.raises(HTTPError):
        two.git_log("", True, 5)


def test_server_with_two_folders(two, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Handler, "ws", two, raising=False)
    monkeypatch.setattr(Handler, "terms", None)
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        status, t = call(base, "GET", "/api/tree")
        assert status == 200 and t["root"] == "proj + other" and t["tree"]["multi"]
        fb = call(base, "POST", "/api/feedback", {"paths": ["other/docs"], "text": "why"})[1]
        assert call(base, "PUT", "/api/feedback", {"id": fb["id"], "status": "done"})[1]["status"] == "done"
        assert call(base, "POST", "/api/feedback-delete", {"id": fb["id"]})[0] == 200
        assert call(base, "PUT", "/api/note", {"path": "", "note": "x"})[0] == 200
        top = call(base, "POST", "/api/feedback", {"paths": [""], "text": "load all skills"})[1]
        assert top["id"] == "*:1" and top["paths"] == [""]
        t = call(base, "GET", "/api/tree")[1]
        assert t["notes"]["."] == "x" and [f["id"] for f in t["feedback"]] == ["*:1"]
        assert call(base, "PUT", "/api/feedback", {"id": "*:1", "reply": "ok", "status": "done"})[1]["status"] == "done"
        main(["edits", "-C", str(tmp_path / "other"), "--url", base])
        assert "saved in the editor: none recently" in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()


def test_print_and_open_several(capsys, tree, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("x\n")
    main(["print", str(tree), str(other), "--no-counts"])
    out = capsys.readouterr().out
    assert out.startswith("proj/") and "\nother/\n" in out


def test_top_level_and_cross_folder(two, tree, tmp_path, capsys):
    v = two.version()
    two.set_note("", "both projects")
    assert two.version() != v
    assert two.read_notes()["."] == "both projects"
    assert (tmp_path / ".juranotes.json").exists()
    top = two.add_feedback([""], "load every skill")
    assert top["id"] == "*:1" and top["paths"] == [""] and top["root"] == str(tmp_path)
    cross = two.add_feedback(["proj/pkg", "other/docs"], "merge these")
    assert cross["id"] == "*:2" and cross["paths"] == ["other/docs", "proj/pkg"]
    assert two.update_feedback("*:1", reply="done", status="done")["id"] == "*:1"
    assert [f["id"] for f in two.refresh_anchors()] == ["*:1", "*:2"]
    out = two.export()
    assert "# on the opened folders together" in out and "! #2 other/docs, proj/pkg: merge these" in out
    assert "load every skill" not in out
    main(["fb", "-C", str(tmp_path), "ls"])  # the agent pane starts in the common parent
    assert "#2  open  other/docs, proj/pkg" in capsys.readouterr().out
    two.delete_feedback("*:2")
    with pytest.raises(HTTPError):
        two.delete_feedback("*:2")


def test_parent_opened_with_child_shares_notes(tree):
    nested = Mounts([_ws(str(tree), "", [], False), _ws(str(tree / "pkg"), "", [], False)])
    assert not nested.own_top
    f = nested.add_feedback([""], "top")
    assert f["id"] == "proj:1" and f["paths"] == ["proj"]
    assert nested.update_feedback("proj:1", status="done")["status"] == "done"
