"""gRPC retrieval: typed round trip, deadline -> in-process fallback, input validation."""

import asyncio
import json
import sys
from pathlib import Path

import grpc
import pytest
from app.config import settings
from app.retrieval import Index
from app.retrieval_client import RemoteRetriever
from app.rpc import retrieval_pb2, retrieval_pb2_grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "retrieval"))
import server as retrieval_server


@pytest.fixture
def index(tmp_path, monkeypatch):
    corpus = tmp_path / "c.jsonl"
    rows = [{"anchor": "scipali", "title": "SciPaLI", "url": "u#scipali", "part": 0,
             "text": "PaliGemma2 fine-tuned with LoRA on ScienceQA, 72.2% exact match"},
            {"anchor": "edgeloop", "title": "EdgeLoop", "url": "u#edgeloop", "part": 0,
             "text": "ML from training set to device firmware in C"}]
    corpus.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(settings, "corpus_path", str(corpus))
    return Index.load()


class Slow(retrieval_server.Servicer):
    async def Search(self, request, context):
        await asyncio.sleep(2)
        return await super().Search(request, context)


async def start(servicer):
    server = grpc.aio.server()
    retrieval_pb2_grpc.add_RetrievalServicer_to_server(servicer, server)
    port = server.add_insecure_port("localhost:0")
    await server.start()
    return server, port


async def test_round_trip(index):
    server, port = await start(retrieval_server.Servicer(index))
    r = RemoteRetriever(f"localhost:{port}", "", index)
    chunks = await r.search("LoRA ScienceQA accuracy", 2)
    assert chunks[0]["anchor"] == "scipali" and r.stats == {"remote": 1, "fallback": 0}
    await server.stop(None)


async def test_deadline_falls_back_to_local_keyword_search(index, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_deadline_s", 0.3)
    server, port = await start(Slow(index))
    r = RemoteRetriever(f"localhost:{port}", "", index)
    chunks = await r.search("LoRA ScienceQA accuracy", 2)
    assert chunks[0]["anchor"] == "scipali"  # still answered, by the in-process index
    assert r.stats == {"remote": 0, "fallback": 1}
    await server.stop(None)


async def test_server_down_falls_back(index):
    r = RemoteRetriever("localhost:1", "", index)
    assert (await r.search("firmware in C", 2))[0]["anchor"] == "edgeloop"
    assert r.stats["fallback"] == 1


async def test_rejects_empty_query(index):
    server, port = await start(retrieval_server.Servicer(index))
    async with grpc.aio.insecure_channel(f"localhost:{port}") as ch:
        with pytest.raises(grpc.aio.AioRpcError) as e:
            await retrieval_pb2_grpc.RetrievalStub(ch).Search(retrieval_pb2.SearchRequest(query=" "))
        assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    await server.stop(None)
