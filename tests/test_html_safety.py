"""Pages made from a notebook or a saved web page (.mht) hold nothing that runs or reaches out when opened: no
scripts or their on...= handlers, javascript: links, frames, plugins, refreshes, or pictures and stylesheets from
the web. What the file itself holds stays: tables, text, its own pictures (data URIs) and its own styles. Each
check has a control: the same page must still show the content that was meant to stay."""

import base64
import json
import re
from email.message import EmailMessage
from pathlib import Path

import pytest

from latheport.converters import email as mail
from latheport.converters import notebooks

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8cfc0f01f0005000201a5f6e5"
    "3c0000000049454e44ae426082")).decode()

HOSTILE = ('<script>alert("x")</script>'
           '<img src="https://tracker.example/pixel.png" onerror="steal()" alt="pixel">'
           '<iframe src="https://evil.example/frame"></iframe>'
           '<a href="java\tscript:alert(1)">click</a>'
           '<object data="https://evil.example/x.swf"></object>'
           '<meta http-equiv="refresh" content="0;url=https://evil.example/">'
           '<div style="background:url(https://tracker.example/bg.png)">styled</div>'
           '<svg><script>alert(2)</script><circle r="1" onload="steal()"/></svg>')
KEEP = '<table class="dataframe"><tr><td>forty-two</td></tr></table><b>kept bold</b>'


def _assert_safe(page: str) -> None:
    low = page.lower()
    found = [bad for bad in ("<script", "onerror", "onload", "javascript:", "<iframe", "<object", "http-equiv",
                             "tracker.example", "evil.example") if bad in low]
    assert not found, found
    assert re.search(r"java\s*script:", low) is None


def _assert_kept(page: str) -> None:
    assert "forty-two" in page and "<table" in page and "<b>kept bold</b>" in page
    assert f"data:image/png;base64,{PNG}" in page
    assert "click" in page and "styled" in page  # the text of a cleaned link and div stays


def _notebook(tmp_path: Path) -> Path:
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n\n", HOSTILE]},
        {"cell_type": "code", "metadata": {}, "execution_count": 1, "source": ["df"], "outputs": [
            {"output_type": "execute_result", "execution_count": 1, "metadata": {},
             "data": {"text/html": [HOSTILE + KEEP], "text/plain": ["df"]}},
            {"output_type": "display_data", "metadata": {}, "data": {"image/png": PNG}},
        ]},
    ]}
    src = tmp_path / "hostile.ipynb"
    src.write_text(json.dumps(nb), encoding="utf-8")
    return src


def test_a_notebook_page_runs_nothing_and_fetches_nothing(tmp_path):
    src = _notebook(tmp_path)
    dst = tmp_path / "out.html"
    notebooks.ipynb_to_html(src, dst, "ipynb", "html")
    page = dst.read_text(encoding="utf-8")
    _assert_safe(page)
    _assert_kept(page)
    assert "<h1>Title</h1>" in page


def _mht(tmp_path: Path) -> Path:
    root = EmailMessage()
    root["Subject"] = "Saved page"
    root.set_content("x")
    root.make_related()
    page = ('<html><head><title>Saved page</title><meta charset="windows-1252">'
            '<link rel="stylesheet" href="http://site.example/look.css">'
            '<link rel="stylesheet" href="https://evil.example/remote.css"></head>'
            f'<body>{HOSTILE}{KEEP}<img src="http://site.example/pic.png" alt="own"></body></html>')
    root.get_payload()[0].set_content(page, subtype="html")
    root.add_related(base64.b64decode(PNG), "image", "png", headers=["Content-Location: http://site.example/pic.png"])
    css = ".local-rule{color:rgb(1,2,3)} .tracked{background:url(https://tracker.example/css.png)}"
    root.add_related(css, "css", headers=["Content-Location: http://site.example/look.css"])
    src = tmp_path / "saved.mht"
    src.write_bytes(bytes(root))
    return src


def test_a_saved_web_page_runs_nothing_and_fetches_nothing(tmp_path):
    src = _mht(tmp_path)
    dst = tmp_path / "out.html"
    mail.mht_to_html(src, dst, "mht", "html")
    page = dst.read_text(encoding="utf-8")
    _assert_safe(page)
    _assert_kept(page)
    assert ".local-rule{color:rgb(1,2,3)}" in page.replace(" ", "")  # its own stylesheet, now inline
    assert "<title>Saved page</title>" in page


PLAIN = ['<table border="1" class="dataframe"><thead><tr><th>a</th></tr></thead><tr><td colspan=2>x</td></tr></table>',
         '<pre><code class="language-python">a &lt; b</code></pre>', "no tags at all", "<p>one<br/>two</p>"]
TRICKS = ['<td title="a>b" onclick=x>', "<td/onclick=x>", '<td title=x"onclick=y>', "<td onclick=x>",
          "<TD ONCLICK=x>", "<img src=x onerror=y", "<!-- <td onclick=x> -->", '<a href="javascript:x">a</a>',
          '<td style="background:url(http://t.example/p.png)">', '<td title="a<b">', "<svg onload=x>",
          "text < more", '<td class="a" onmouseover=x>', "<td\nonclick=x>", "<p\tonclick='x'>"]


@pytest.mark.parametrize("fragment", PLAIN)
def test_plain_fragments_are_left_as_they_are(fragment):
    assert mail._plainly_safe(fragment)
    assert mail.clean_fragment(fragment) == fragment


@pytest.mark.parametrize("fragment", TRICKS)
def test_tricks_are_never_taken_as_plain_and_come_out_inert(fragment):
    from bs4 import BeautifulSoup
    assert not mail._plainly_safe(fragment)
    out = BeautifulSoup(mail.clean_fragment(fragment), "html.parser")
    handlers = [(t.name, a) for t in out.find_all(True) for a in t.attrs if a.lower().startswith("on")]
    assert not handlers
    assert not out.find_all(["script", "svg"]) or not out.find_all("script")
    assert "javascript:" not in str(out).lower() and "t.example" not in str(out)


@pytest.mark.parametrize("make", [_notebook, _mht])
def test_the_hostile_parts_were_really_in_the_files(tmp_path, make):
    """The control for both: the files hold every hostile part these tests look for."""
    raw = make(tmp_path).read_bytes().decode("utf-8", "replace").lower()
    if b"\r\n\r\n" in make(tmp_path).read_bytes():
        import email as stdlib_email
        from email import policy
        msg = stdlib_email.message_from_bytes(make(tmp_path).read_bytes(), policy=policy.default)
        raw = "".join(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html").lower()
    for bad in ("<script", "onerror", "<iframe", "<object", "http-equiv", "tracker.example", "evil.example"):
        assert bad in raw, bad
