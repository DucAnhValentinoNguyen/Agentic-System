"""Turn the portfolio site into anchored chunks: one chunk per element with an id.

Each chunk keeps a deep link (site_url#anchor) so answers can cite and scroll to it.
"""

import argparse
import json
import sys

import httpx
from bs4 import BeautifulSoup

SITE = "https://ducanhvalentinonguyen.com/"
SKIP_IDS = {"toggle-all"}
MAX_CHARS = 2000


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
        for i in range(0, len(text), MAX_CHARS):
            chunks.append({
                "anchor": anchor,
                "title": title,
                "url": f"{base_url}#{anchor}",
                "part": i // MAX_CHARS,
                "text": text[i:i + MAX_CHARS],
            })
    return chunks


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=SITE)
    p.add_argument("--out", default="ingest/corpus.jsonl")
    a = p.parse_args()
    html = httpx.get(a.url, timeout=20, follow_redirects=True).text
    chunks = build(html, a.url)
    with open(a.out, "w") as f:
        f.writelines(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    print(f"{len(chunks)} chunks from {a.url} -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
