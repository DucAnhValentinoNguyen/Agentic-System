"""Retrieval over the curated site corpus: Vertex embeddings + BM25, fused by rank.

If the embedding call fails, BM25 alone still answers (graceful degradation).
"""

import functools
import json
import math
import re
import time

import httpx
import snowballstemmer
import structlog
from rank_bm25 import BM25Okapi

from .config import settings

log = structlog.get_logger()
TOKEN = re.compile(r"[a-z0-9]+")
# Words that carry no topic in a question about a person's work. BM25 already down-weights them by idf, but in a 28-chunk corpus
# "the" and "and" still add noise to every score, so they are dropped. Deliberately short: no content words, no numbers.
STOP = frozenset(
    ["a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "from", "by", "with", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those", "he", "him", "his", "she", "her", "they", "them", "their", "what", "which", "who", "whom", "how", "why", "when", "where", "do", "does", "did", "done", "can", "could", "would", "should", "will", "has", "have", "had", "having", "about", "into", "than", "then", "so", "if", "also", "there", "here", "you", "your", "me", "my", "i", "we", "our", "us"])
_STEM = snowballstemmer.stemmer("english")


@functools.lru_cache(maxsize=20000)
def stem(word: str) -> str:
    out = _STEM.stemWord(word)
    # Snowball keeps "llms" and "vlms": it only strips an "s" that follows a vowel. Plural acronyms are common on this site.
    if out.endswith("s") and len(out) > 3 and not out.endswith("ss") and not any(v in out[:-1] for v in "aeiou"):
        out = out[:-1]
    return out


def tok(s: str, analyzer: str = "stem+stop") -> list[str]:
    """Lowercase alphanumeric tokens. analyzer: "plain" (the original), "stop" (drop stop words), "stem" (Snowball English stems, so
    "models" matches "model" and "LLMs" matches "LLM"), or "stem+stop"."""
    words = TOKEN.findall(s.lower())
    if "stop" in analyzer:
        words = [w for w in words if w not in STOP] or words      # a query made only of stop words keeps its words
    if "stem" in analyzer:
        words = [stem(w) for w in words]
    return words


class Index:
    def __init__(self, chunks: list[dict], analyzer: str | None = None):
        self.chunks = chunks
        self.analyzer = analyzer or settings.lexical_analyzer
        self.bm25 = BM25Okapi([tok(c["title"] + " " + c["text"], self.analyzer) for c in chunks])
        self.vectors: list[list[float]] | None = None
        self._warm_at = 0.0

    @classmethod
    def load(cls) -> "Index":
        with open(settings.corpus_path) as f:
            chunks = [json.loads(line) for line in f]
        chunks.append({
            "anchor": "contact", "title": "Contact", "url": settings.site_url, "part": 0,
            "text": f"Contact Duc-Anh Nguyen by email at {settings.contact_email}. Based in "
                    "Munich, Germany. GitHub: github.com/DucAnhValentinoNguyen. LinkedIn: "
                    "linkedin.com/in/duc-anh-nguyen-ml. He reads every message about machine "
                    "learning, LLM or MLOps work in Munich.",
        })
        if settings.extra_corpus_path:
            with open(settings.extra_corpus_path) as f:
                chunks += [json.loads(line) for line in f]
        return cls(chunks)

    async def _embed(self, texts: list[str], task: str, token: str) -> list[list[float]]:
        url = (
            f"https://{settings.embed_location}-aiplatform.googleapis.com/v1/projects/"
            f"{settings.gcp_project}/locations/{settings.embed_location}/publishers/google/"
            f"models/{settings.embed_model}:predict"
        )
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {token}"}, json={
                "instances": [{"content": t, "task_type": task} for t in texts]})
            r.raise_for_status()
            return [p["embeddings"]["values"] for p in r.json()["predictions"]]

    async def warm(self, token: str) -> None:
        self._warm_at = time.monotonic()
        try:
            self.vectors = await self._embed(
                [c["title"] + "\n" + c["text"] for c in self.chunks], "RETRIEVAL_DOCUMENT", token)
        except Exception as e:  # noqa: BLE001
            log.warning("embed_warm_failed", error=str(e)[:200])

    async def search(self, query: str, token: str | None, k: int = 5) -> list[dict]:
        n = len(self.chunks)
        scores = self.bm25.get_scores(tok(query, self.analyzer))
        ranks = [sorted(range(n), key=lambda i: -scores[i])]
        # Startup embedding can fail (e.g. IAM not propagated yet): retry at most once a minute.
        if self.vectors is None and token and time.monotonic() - self._warm_at > 60:
            await self.warm(token)
        if self.vectors and token:
            try:
                q = (await self._embed([query], "RETRIEVAL_QUERY", token))[0]
                qn = math.sqrt(sum(x * x for x in q))
                sims = [sum(a * b for a, b in zip(q, v)) / (qn * math.sqrt(sum(x * x for x in v)))
                        for v in self.vectors]
                ranks.append(sorted(range(n), key=lambda i: -sims[i]))
            except Exception as e:  # noqa: BLE001
                log.warning("embed_query_failed", error=str(e)[:200])
        # Reciprocal rank fusion.
        fused = [0.0] * n
        for r in ranks:
            for pos, i in enumerate(r):
                fused[i] += 1.0 / (60 + pos)
        top = sorted(range(n), key=lambda i: -fused[i])[:k]
        return [self.chunks[i] for i in top]
