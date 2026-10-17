"""Turn the portfolio site into anchored chunks: one chunk per element with an id.

Each chunk keeps a deep link (site_url#anchor) so answers can cite and scroll to it.

Deep mode (default) also reads the pages the site links to, one level deep and on the same site only: HTML pages are split by
heading, PDFs by page. Those chunks carry source = "linked", the section that linked them (parent) and their own page url, so a
citation points at the page the fact came from. Pages are fetched at build time; the running service never fetches anything.
"""

import argparse
import io
import json
import re
import sys
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

SITE = "https://ducanhvalentinonguyen.com/"
SKIP_IDS = {"toggle-all"}
MAX_CHARS = 2000


def section_links(el, base_url: str) -> list[dict]:
    """Where a section points to: [{"label", "url"}] for its outgoing links (not in-page anchors, mail or script links).

    The assistant does not read these pages; it only knows that they exist and what they are called.
    """
    seen, out = set(), []
    for a in el.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href)
        label = " ".join(a.get_text(" ", strip=True).replace("→", " ").split())
        if url in seen or not label:
            continue
        seen.add(url)
        out.append({"label": label, "url": url})
    return out


def build(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    chunks = []
    for el in soup.select("[id]"):
        anchor = el["id"]
        if anchor in SKIP_IDS:
            continue
        # Skip parents whose text is mostly made of anchored children; keep the leaves.
        nested = el.select("[id]")
        text = " ".join(el.get_text(" ", strip=True).split())
        if nested:
            child_len = sum(len(" ".join(c.get_text(" ", strip=True).split())) for c in nested)
            if child_len > 0.8 * len(text):
                continue
        if not text:
            continue
        heading = el.find(["h1", "h2", "h3", "h4"])
        title = heading.get_text(" ", strip=True) if heading else anchor.replace("-", " ")
        links = section_links(el, base_url)
        for i in range(0, len(text), MAX_CHARS):
            chunks.append({
                "anchor": anchor,
                "title": title,
                "url": f"{base_url}#{anchor}",
                "part": i // MAX_CHARS,
                "text": text[i:i + MAX_CHARS],
                "links": links,
            })
    return chunks


MAX_LINKED_PAGES = 12
MAX_PAGE_BYTES = 4_000_000
BLOCKS = ["h1", "h2", "h3", "h4", "p", "li", "tr", "pre", "figcaption", "blockquote", "dt", "dd"]
INSIDE = ["tr", "li", "pre", "figcaption", "blockquote", "dt", "dd"]      # text of these is taken whole, once


def slug(s: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "page"


def split_text(text: str) -> list[str]:
    return [text[i:i + MAX_CHARS] for i in range(0, len(text), MAX_CHARS)]


def page_chunks(html: str, url: str) -> list[dict]:
    """Split one HTML page into chunks at its h1-h3 headings. Table rows keep their cells ("a | b | c"), so numbers stay readable."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else ""
    for t in soup(["script", "style", "svg", "noscript", "nav", "footer", "head"]):
        t.decompose()
    h1 = soup.find("h1")
    page_title = re.sub(r"\s+[—–-]\s+Duc-Anh Nguyen$", "", page_title or (h1.get_text(" ", strip=True) if h1 else url))
    stem = slug(urlparse(url).path.strip("/").removesuffix(".html").replace("/", "-") or "home", 40)
    sections: list[dict] = [{"heading": page_title, "id": None, "lines": []}]
    for el in (soup.body or soup).find_all(BLOCKS):
        if el.name not in INSIDE and el.find_parent(INSIDE):
            continue
        if el.name in ("h1", "h2", "h3"):
            sections.append({"heading": el.get_text(" ", strip=True), "id": el.get("id") or (el.find_parent(id=True) or {}).get("id"),
                             "lines": []})
            continue
        text = (" | ".join(c.get_text(" ", strip=True) for c in el.find_all(["th", "td"])) if el.name == "tr"
                else el.get_text(" ", strip=True))
        text = " ".join(text.split())
        if text:
            sections[-1]["lines"].append(("**" + text + "**") if el.name == "h4" else text)
    out, used = [], set()
    for sec in sections:
        text = " ".join(sec["lines"])
        if len(text) < 40:
            continue
        base = f"{stem}--{slug(sec['heading'])}"
        anchor, n = base, 2
        while anchor in used:
            anchor, n = f"{base}-{n}", n + 1
        used.add(anchor)
        title = page_title if sec["heading"] == page_title else f"{page_title} — {sec['heading']}"
        for i, part in enumerate(split_text(text)):
            out.append({"anchor": anchor, "title": title, "url": f"{url}#{sec['id']}" if sec["id"] else url, "part": i,
                        "text": part, "links": []})
    return out


def pdf_chunks(data: bytes, url: str) -> list[dict]:
    """One chunk per PDF page (long pages split further). Extraction happens here, at build time (pypdf), never in the service."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    meta = (reader.metadata.title if reader.metadata else None) or urlparse(url).path.rsplit("/", 1)[-1]
    stem = slug(urlparse(url).path.strip("/").removesuffix(".pdf").replace("/", "-"), 40)
    out = []
    for n, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = re.sub(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b", lambda m: m.group(0).replace(" ", ""), text)   # letter-spaced headings
        text = " ".join(text.split())
        if len(text) < 40:
            continue
        for i, part in enumerate(split_text(text)):
            out.append({"anchor": f"{stem}-pdf-p{n}", "title": f"{meta} (PDF) — page {n}", "url": f"{url}#page={n}", "part": i,
                        "text": part, "links": []})
    return out


def linked_targets(chunks: list[dict], base_url: str) -> dict[str, str]:
    """Same-site pages the chunks link to (url without fragment -> first section that links it), in order, capped."""
    host, found = urlparse(base_url).netloc, {}
    for c in chunks:
        for link in c.get("links", []):
            u = urlparse(link["url"])
            clean = u._replace(fragment="", query="").geturl()
            if u.netloc != host or clean.rstrip("/") == base_url.rstrip("/") or clean in found:
                continue
            if u.path.endswith((".html", ".htm", ".pdf", "/")):
                found[clean] = c["anchor"]
    return dict(list(found.items())[:MAX_LINKED_PAGES])


def deepen(chunks: list[dict], base_url: str, fetch: Callable[[str], tuple[str, bytes]]) -> list[dict]:
    """Add the chunks of every linked same-site page (one level, no further). A fetch or parse failure skips that page."""
    extra = []
    for url, parent in linked_targets(chunks, base_url).items():
        try:
            ctype, data = fetch(url)
            if len(data) > MAX_PAGE_BYTES:
                raise ValueError(f"{len(data)} bytes is over the limit")
            if "pdf" in ctype or url.endswith(".pdf"):
                new = pdf_chunks(data, url)
            elif "html" in ctype:
                new = page_chunks(data.decode("utf-8", "replace"), url)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            print(f"skipped {url}: {e}", file=sys.stderr)
            continue
        for c in new:
            c.update({"source": "linked", "parent": parent})
        if new:
            extra += new
            for c in chunks:                      # the parent's link is now read: the assistant must not call it "not read"
                for link in c.get("links", []):
                    if urlparse(link["url"])._replace(fragment="", query="").geturl() == url:
                        link["read"] = True
    return chunks + extra


def fetch_http(url: str) -> tuple[str, bytes]:
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.headers.get("content-type", ""), r.content


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=SITE)
    p.add_argument("--out", default="ingest/corpus.jsonl")
    p.add_argument("--no-deep", action="store_true", help="only the home page (the corpus before Experiment 7)")
    a = p.parse_args()
    html = httpx.get(a.url, timeout=20, follow_redirects=True).text
    chunks = build(html, a.url)
    if not a.no_deep:
        chunks = deepen(chunks, a.url, fetch_http)
    with open(a.out, "w") as f:
        f.writelines(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    print(f"{len(chunks)} chunks from {a.url} -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
