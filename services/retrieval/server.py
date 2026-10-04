"""gRPC retrieval service. Same image as agent-api, different entrypoint.

  python services/retrieval/server.py   (listens on $PORT, plaintext h2c; Cloud Run terminates TLS)
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

import google.auth
import google.auth.transport.requests
import grpc
import structlog
from app.retrieval import Index
from app.rpc import retrieval_pb2, retrieval_pb2_grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

log = structlog.get_logger()
structlog.configure(processors=[structlog.processors.add_log_level,
                                structlog.processors.EventRenamer("message"),
                                structlog.processors.JSONRenderer()])


class Servicer(retrieval_pb2_grpc.RetrievalServicer):
    def __init__(self, index: Index):
        self.index, self.creds = index, None

    async def _token(self) -> str:
        if self.creds is None:
            self.creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not self.creds.valid:
            await asyncio.to_thread(self.creds.refresh, google.auth.transport.requests.Request())
        return self.creds.token

    async def Search(self, request, context):
        if not request.query.strip() or len(request.query) > 2000:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query must be 1-2000 chars")
        k = min(max(request.k or 5, 1), 10)
        try:
            token = await self._token()
        except Exception:  # noqa: BLE001
            token = None
        chunks = await self.index.search(request.query, token, k)
        used = bool(self.index.vectors and token)
        log.info("search", k=k, used_embeddings=used, anchors=[c["anchor"] for c in chunks])
        return retrieval_pb2.SearchResponse(
            used_embeddings=used,
            chunks=[retrieval_pb2.Chunk(anchor=c["anchor"], title=c["title"], url=c["url"],
                                        text=c["text"], part=c.get("part", 0)) for c in chunks])


async def serve() -> None:
    index = Index.load()
    svc = Servicer(index)
    try:
        await index.warm(await svc._token())
    except Exception as e:  # noqa: BLE001
        log.warning("warm_failed", error=str(e)[:200])
    server = grpc.aio.server()
    retrieval_pb2_grpc.add_RetrievalServicer_to_server(svc, server)
    hs = health.HealthServicer()
    hs.set("", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(hs, server)
    port = os.environ.get("PORT", "50051")
    server.add_insecure_port(f"0.0.0.0:{port}")
    await server.start()
    log.info("startup", port=port, chunks=len(index.chunks), embedded=index.vectors is not None)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
