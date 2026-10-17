"""The corpus is embedded in small batches: one request with every section exceeded Vertex's token limit (Experiment 7)."""

from app.retrieval import EMBED_BATCH, Index


async def test_warm_embeds_in_batches_and_keeps_order():
    chunks = [{"anchor": f"a{i}", "title": f"t{i}", "text": f"text {i}", "url": "u", "part": 0} for i in range(EMBED_BATCH * 2 + 3)]
    index = Index(chunks)
    sizes = []

    async def fake_embed(texts, task, token):
        sizes.append(len(texts))
        assert task == "RETRIEVAL_DOCUMENT"
        return [[float(t.split("\n")[0][1:]), 1.0] for t in texts]            # the vector encodes the section number

    index._embed = fake_embed
    await index.warm("token")
    assert sizes == [EMBED_BATCH, EMBED_BATCH, 3] and len(index.vectors) == len(chunks)
    assert [v[0] for v in index.vectors] == [float(i) for i in range(len(chunks))]
