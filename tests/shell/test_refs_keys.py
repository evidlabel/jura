"""The editor's labquote key completion reads refs/refs.yml without a YAML library (sources.ref_keys)."""

import os

from workbench.write import sources

REFS = """\
0024:main:
  type: article
  title: Om Genvej
  author: genvej.org
  url: https://genvej.org/om-genvej/
'0024:quoted':
  type: article
  title: 'It''s quoted'
0024:block:
  type: article
  title: |-
    First line
    second line
  parent:
    type: article
    title: The parent's title, not this entry's
0088:escaped:
  type: article
  title: "Nogle studier \\
    \\ og hvide \\u00e6\\nnext"
0099:folded:
  type: article
  title: A long plain title that a dumper
    folded onto a second line
  url: https://example.org/x
"""


def test_ref_keys(tmp_path):
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "refs.yml").write_text(REFS, "utf-8")
    got = {r["key"]: r for r in sources.ref_keys(tmp_path)}
    assert list(got) == ["0024:main", "0024:quoted", "0024:block", "0088:escaped", "0099:folded"]
    assert got["0024:main"] == {"key": "0024:main", "title": "Om Genvej", "url": "https://genvej.org/om-genvej/"}
    assert got["0024:quoted"]["title"] == "It's quoted"
    assert got["0024:block"]["title"] == "First line second line"  # one line: a hint beside the key
    assert got["0088:escaped"]["title"] == "Nogle studier og hvide æ next"
    assert got["0099:folded"]["title"] == "A long plain title that a dumper folded onto a second line"
    assert got["0099:folded"]["url"] == "https://example.org/x"


def test_ref_keys_long_title_and_cache(tmp_path):
    (tmp_path / "refs").mkdir()
    f = tmp_path / "refs" / "refs.yml"
    f.write_text("k:\n  title: " + "x" * 400 + "\n", "utf-8")
    [r] = sources.ref_keys(tmp_path)
    assert len(r["title"]) == 160 and r["title"].endswith("…")
    assert sources.ref_keys(tmp_path) is sources.ref_keys(tmp_path)  # cached while the file is unchanged
    f.write_text("k2:\n  title: y\n", "utf-8")
    os.utime(f, (1, 1))
    assert [r["key"] for r in sources.ref_keys(tmp_path)] == ["k2"]


def test_ref_keys_missing(tmp_path):
    assert sources.ref_keys(tmp_path) == []
