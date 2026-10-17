"""BM25 tokenisation: stemming and a short stop-word list (Experiment 6)."""

import json
from pathlib import Path

from app.retrieval import Index, tok

CORPUS = [json.loads(line) for line in (Path(__file__).parent.parent / "ingest/corpus.jsonl").read_text().splitlines()]


def test_plain_tokenisation_is_unchanged():
    assert tok("Duc-Anh's marathon PB: 4:20:07", "plain") == ["duc", "anh", "s", "marathon", "pb", "4", "20", "07"]


def test_stemming_joins_inflections_and_keeps_names_and_numbers():
    assert tok("LLMs", "stem") == tok("LLM", "stem")
    assert tok("models fine-tuned quantized", "stem") == tok("model fine tune quantize", "stem")
    assert tok("gnhf 183x 2027", "stem") == ["gnhf", "183x", "2027"]


def test_stop_words_go_but_a_query_of_only_stop_words_keeps_its_words():
    assert tok("What is the best marathon time he has?", "stop") == ["best", "marathon", "time"]
    assert tok("what is he", "stop") == ["what", "is", "he"]


def test_the_default_analyzer_finds_inflected_questions():
    index = Index(CORPUS)                                   # default: stem+stop
    top = [c["anchor"] for c in __import__("asyncio").run(index.search("How fast does he run marathons?", None, 3))]
    assert "hobbies-and-social-activities" in top
    plain = Index(CORPUS, analyzer="plain")
    top_plain = [c["anchor"] for c in __import__("asyncio").run(plain.search("How fast does he run marathons?", None, 3))]
    assert "hobbies-and-social-activities" not in top_plain  # the old tokeniser missed "marathons" vs "marathon"
