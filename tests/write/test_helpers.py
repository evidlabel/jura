# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import io
import struct

import pytest

from workbench.write import core as cli
from workbench.write.term import ws_accept, ws_frame, ws_recv


def test_decode_text_and_binary():
    assert cli.decode(b"hello") == "hello"
    assert cli.decode(b"a\0b") is None
    assert cli.decode(b"\xff\xfe") is None


@pytest.mark.parametrize(
    "name,mime",
    [
        ("fig.png", "image/png"),
        ("dir/FIG.PNG", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("a.svg", "image/svg+xml"),
        ("a.pdf", "application/pdf"),
        ("a.tif", "image/tiff"),
        ("notes.txt", None),
        ("dir.png/readme", None),
        ("noext", None),
    ],
)
def test_plot_mime(name, mime):
    assert cli.plot_mime(name) == mime


@pytest.mark.parametrize("text,n", [("", 0), ("a", 1), ("a\n", 1), ("a\nb", 2), ("a\nb\n", 2)])
def test_count_lines(text, n):
    assert cli.count_lines(text) == n


def test_atomic_write_keeps_mode(tmp_path):
    p = tmp_path / "sub" / "f.sh"
    cli.atomic_write(p, b"one")
    p.chmod(0o755)
    cli.atomic_write(p, b"two")
    assert p.read_bytes() == b"two"
    assert p.stat().st_mode & 0o777 == 0o755
    assert [x.name for x in p.parent.iterdir()] == ["f.sh"]


def test_locate_follows_moved_quote():
    text = "a\nb\nc\nd\n"
    assert cli.locate([2, 3], "b\nc", text) == (2, 3, False)
    assert cli.locate([2, 3], "b\nc", "x\ny\n" + text) == (4, 5, False)
    assert cli.locate([2, 3], "zz", text) == (2, 3, True)


def test_labels():
    assert cli.fb_label({"id": 3}) == "! #3"
    assert cli.fb_label({"id": 3, "lines": [10, 14]}) == "! #3 L10-14"
    assert cli.fb_label({"id": 3, "lines": [5, 5], "stale": True}) == "! #3 L5 (moved?)"
    assert cli.first_line("\n  hi\nthere") == "hi"
    assert cli.first_line("") == ""
    assert cli.plural(1, "line") == "1 line"
    assert cli.plural(2, "line") == "2 lines"


@pytest.mark.parametrize("n,s", [(12, "12"), (1234, "1.2k"), (12345, "12k")])
def test_fmt_tokens(n, s):
    assert cli.fmt_tokens(n) == s


def test_rank_by_size_and_heat():
    tree = {
        "path": "",
        "type": "dir",
        "children": [
            {"path": "d", "type": "dir", "children": [{"path": "d/x", "type": "file", "words": 10}]},
            {"path": "y", "type": "file", "words": 100},
            {"path": "z", "type": "file", "words": 0},
        ],
    }
    words = cli.word_totals(tree)
    assert words == {"": 110, "d": 10, "d/x": 10, "y": 100, "z": 0}
    dirs = cli.dir_paths(tree)
    assert dirs == {"", "d"}
    ranks = cli.rank_by_size(words, dirs)
    assert ranks[""] == 0.5 and "z" not in ranks
    assert ranks["d/x"] < ranks["y"]
    assert cli.heat_ansi(None, False) == "\033[38;5;240m"
    assert cli.heat_ansi(0.5, False) == "\033[0m"
    assert cli.heat_ansi(0.5, True) == "\033[1m"
    assert cli.heat_ansi(0.99, True) == "\033[1;38;5;196m"


def test_skill_tokens():
    tree = {
        "type": "dir",
        "name": "",
        "children": [
            {
                "type": "dir",
                "name": "s",
                "children": [
                    {"type": "file", "name": "SKILL.md", "kind": "text", "chars": 40},
                    {"type": "file", "name": "leaf.md", "kind": "text", "chars": 400},
                    {"type": "file", "name": "img.png", "kind": "binary"},
                ],
            },
            {"type": "dir", "name": "other", "children": []},
        ],
    }
    cli.skill_tokens(tree)
    s = tree["children"][0]
    assert tree["skills"] is True
    assert (s["tmin"], s["tmax"]) == (10, 110)
    assert (tree["tmin"], tree["tmax"]) == (10, 110)
    assert "tmin" not in tree["children"][1]


def test_skill_tokens_no_skill():
    tree = {"type": "dir", "children": [{"type": "file", "name": "a.md"}]}
    cli.skill_tokens(tree)
    assert "skills" not in tree


def test_parts_rejects_parent():
    assert cli.Workspace.parts("./a//b/") == ["a", "b"]
    with pytest.raises(cli.HTTPError) as e:
        cli.Workspace.parts("a/../b")
    assert e.value.status == 400


def _masked(op, payload, fin=True):
    mask = b"\1\2\3\4"
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op])
    head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def test_websocket_frames():
    assert ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert ws_frame(1, b"hi") == b"\x81\x02hi"
    assert ws_frame(2, b"x" * 200)[:4] == b"\x82\x7e\x00\xc8"
    assert ws_frame(2, b"x" * 70000)[:2] == b"\x82\x7f"
    stream = io.BytesIO(_masked(9, b"p") + _masked(1, b"he", fin=False) + _masked(0, b"llo") + _masked(2, b"y" * 300))
    assert ws_recv(stream) == (9, b"p")
    assert ws_recv(stream) == (1, b"hello")
    assert ws_recv(stream) == (2, b"y" * 300)
    assert ws_recv(stream) is None
    assert ws_recv(io.BytesIO(b"\x81\x05ab")) is None
