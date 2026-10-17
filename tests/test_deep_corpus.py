"""Deep corpus: same-site linked pages are read one level deep, HTML by heading and PDF by page (Experiment 7)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ingest"))
from build_corpus import deepen, linked_targets, page_chunks, pdf_chunks

BASE = "https://site.example/"
PAGE = """<html><head><title>Results — Duc-Anh Nguyen</title><script>var x=1</script></head><body><h1>Results</h1>
<p>Intro paragraph that is long enough to be kept as a section of its own in the index.</p>
<h2 id="tbl">The table</h2><table><tr><th>task</th><th>acc</th></tr><tr><td>findings</td><td>+.016</td></tr></table>
<p>Closing remark about the table above, long enough to count as content for this section.</p>
<h2>Second part</h2><ul><li>first point that is quite long indeed</li><li>second point that is also long</li></ul>
<a href="/deeper.html">deeper</a></body></html>"""


def make_pdf(pages: list[str]) -> bytes:
    """A tiny valid PDF with one text line per page (uncompressed, so no library is needed to write it)."""
    objs, kids = [], []
    for i, text in enumerate(pages):
        n = 4 + i * 2
        kids.append(f"{n} 0 R")
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
        objs.append((n, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"))
        objs.append((n + 1, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
    body = {1: "<< /Type /Catalog /Pages 2 0 R >>", 2: f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", **dict(objs)}
    out, offsets = b"%PDF-1.4\n", {}
    for n in sorted(body):
        offsets[n] = len(out)
        out += f"{n} 0 obj\n{body[n]}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(body) + 1}\n0000000000 65535 f \n".encode()
    for n in sorted(body):
        out += f"{offsets[n]:010d} 00000 n \n".encode()
    return out + f"trailer\n<< /Size {len(body) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def test_html_is_split_by_heading_and_tables_keep_their_cells():
    chunks = page_chunks(PAGE, "https://site.example/results.html")
    titles = [c["title"] for c in chunks]
    assert titles == ["Results", "Results — The table", "Results — Second part"]
    table = chunks[1]
    assert "findings | +.016" in table["text"] and table["url"].endswith("results.html#tbl") and "var x" not in table["text"]
    assert chunks[2]["text"].startswith("first point") and chunks[2]["anchor"] == "results--second-part"


def test_pdf_pages_become_chunks_with_a_page_url():
    chunks = pdf_chunks(make_pdf(["The pilot is capped at 100k euro and lasts six weeks.", "Appendix with the cost model and its list prices."]),
                        "https://site.example/files/report.pdf")
    assert [c["url"] for c in chunks] == ["https://site.example/files/report.pdf#page=1", "https://site.example/files/report.pdf#page=2"]
    assert "capped at 100k" in chunks[0]["text"] and chunks[1]["title"].endswith("(PDF) — page 2")
    assert chunks[0]["anchor"] == "files-report-pdf-p1"


def test_deepen_follows_same_site_links_one_level_only_and_marks_the_parent_link_read():
    home = [{"anchor": "sec", "title": "Sec", "url": BASE + "#sec", "part": 0, "text": "x" * 50, "links": [
        {"label": "Results", "url": BASE + "results.html"}, {"label": "Report", "url": BASE + "r.pdf"},
        {"label": "GitHub", "url": "https://github.com/x/y"}, {"label": "Broken", "url": BASE + "gone.html"}]}]
    pdf = make_pdf(["Capped at 100k euro. A sentence long enough."])
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.endswith("results.html"):
            return "text/html", PAGE.encode()
        if url.endswith("r.pdf"):
            return "application/pdf", pdf
        raise OSError("404")

    out = deepen(home, BASE, fetch)
    assert sorted(fetched) == [BASE + "gone.html", BASE + "r.pdf", BASE + "results.html"]       # github is another site; deeper.html is level two
    linked = [c for c in out if c.get("source") == "linked"]
    assert linked and all(c["parent"] == "sec" for c in linked) and out[0] is home[0]
    assert [x.get("read") for x in home[0]["links"]] == [True, True, None, None]


def test_linked_targets_ignore_the_page_itself_other_hosts_and_non_documents():
    chunks = [{"anchor": "a", "links": [{"url": BASE}, {"url": BASE + "#x"}, {"url": "https://other.example/p.html"}, {"url": BASE + "img.png"},
                                        {"url": BASE + "p.html#frag"}, {"url": BASE + "p.html"}, {"url": BASE + "dir/"}]}]
    assert linked_targets(chunks, BASE) == {BASE + "p.html": "a", BASE + "dir/": "a"}


def test_the_linked_discount_prefers_the_home_page_on_a_tie():
    import asyncio

    from app.retrieval import Index
    twin = "marathon training plan"
    chunks = [{"anchor": "home", "title": "t", "url": "u", "part": 0, "text": twin},
              {"anchor": "deep", "title": "t", "url": "u2", "part": 0, "text": twin, "source": "linked"}]
    assert [c["anchor"] for c in asyncio.run(Index(chunks).search(twin, None, 2))] == ["home", "deep"]
