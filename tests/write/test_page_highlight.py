"""The editors' colours and the Typst setup fold (page.html JavaScript, run in node)."""

import json
import shutil
import subprocess
from importlib import resources

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def chunk(src: str, start: str, end: str) -> str:
    a = src.index(start)
    return src[a : src.index(end, a)]


@pytest.fixture(scope="module")
def lib():
    src = (resources.files("workbench.write") / "page.html").read_text("utf-8")
    hl = chunk(src, "const HL_MAX = 200000;", "function renderHL()")  # LANGS, tokens(), hl()
    fold = chunk(src, "function bracketDelta(line)", "function docText()")  # preambleEnd()
    return hl + "\n" + fold


def js(lib: str, expr: str):
    prog = lib + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    return json.loads(subprocess.run([NODE, "-e", prog], capture_output=True, text=True, check=True).stdout)


def picked(text, toks):
    return [(text[a:b], cls) for a, b, cls in toks]


def test_tokens_typst(lib):
    text = '#set page(paper: "a4")\n= Memo\nSee #cite-ref("0024:main") and <lab> // why\n'
    got = picked(text, js(lib, f"tokens({json.dumps(text)}, 'typ')"))
    assert ("#set", "f") in got and ('"a4"', "s") in got and ("= Memo", "h") in got
    assert ("#cite-ref", "f") in got and ('"0024:main"', "s") in got and ("<lab>", "d") in got and ("// why", "c") in got


def test_tokens_are_ordered_and_disjoint(lib):
    text = '<p a="1"><script>let x = "y"; // c\n</script><style>.a{color:#fff}</style></p>'
    toks = js(lib, f"tokens({json.dumps(text)}, 'html')")
    assert all(a < b for a, b, _ in toks) and all(toks[i][1] <= toks[i + 1][0] for i in range(len(toks) - 1))
    got = picked(text, toks)
    assert ("let", "k") in got and ('"y"', "s") in got and ("// c", "c") in got  # the script body is JavaScript
    assert ("#fff", "n") in got  # the style body is CSS


def test_hl_html_matches_tokens(lib):
    text = 'def f(x):  # a <b>\n    return "s"\n'
    html = js(lib, f"hl({json.dumps(text)}, 'py')")
    assert html.startswith('<span class="t-k">def</span> <span class="t-f">f</span>(x):')
    assert '<span class="t-c"># a &lt;b&gt;</span>' in html and '<span class="t-s">"s"</span>' in html
    assert js(lib, "hl('a < b', 'nope')") == "a &lt; b"  # an unknown language is only escaped


def test_preamble_end(lib):
    doc = '#import "x": *\n#set page(\n  paper: "a4",\n)\n\n= Body\n'
    assert js(lib, f"preambleEnd({json.dumps(doc)})") == 5
    assert js(lib, "preambleEnd('= Body\\n#set x\\n')") == 0
