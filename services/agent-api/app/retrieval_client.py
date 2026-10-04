"""Retriever used by the graph: remote gRPC service with a deadline, in-process fallback.

If the gRPC call fails or exceeds its deadline, retrieval falls back to the in-process keyword
index, so a retrieval outage degrades ranking quality instead of failing the conversation.
"""

import time

import grpc
import structlog
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from .config import settings
from .retrieval import Index
from .rpc import retrieval_pb2, retrieval_pb2_grpc

log = structlog.get_logger()


class LocalRetriever:
    def __init__(self, index: Index, token_fn):
        self.index, self.token_fn = index, token_fn

    async def search(self, query: str, k: int = 5) -> list[dict]:
        try:
            token = await self.token_fn()
        except Exception:  # noqa: BLE001 - keyword ranking still works without a token
            token = None
        return await self.index.search(query, token, k)


class RemoteRetriever:
    def __init__(self, addr: str, audience: str, fallback: Index):
        self.fallback, self.audience, self.addr = fallback, audience, addr
        self.stats = {"remote": 0, "fallback": 0}
        self._tok: tuple[str, float] = ("", 0.0)
        if addr.startswith("localhost"):
            self.channel = grpc.aio.insecure_channel(addr)
        else:
            creds = grpc.composite_channel_credentials(
                grpc.ssl_channel_credentials(),
                grpc.metadata_call_credentials(self._auth_plugin))
            self.channel = grpc.aio.secure_channel(addr, creds)
        self.stub = retrieval_pb2_grpc.RetrievalStub(self.channel)

    def _auth_plugin(self, context, callback):
        # Cloud Run service-to-service auth: a Google-signed ID token for the callee's URL.
        try:
            tok, exp = self._tok
            if time.time() > exp:
                tok = id_token.fetch_id_token(Request(), self.audience)
                self._tok = (tok, time.time() + 1800)
            callback((("authorization", f"Bearer {tok}"),), None)
        except Exception as e:  # noqa: BLE001
            callback(None, e)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        try:
            resp = await self.stub.Search(
                retrieval_pb2.SearchRequest(query=query, k=k),
                timeout=settings.retrieval_deadline_s)
            self.stats["remote"] += 1
            return [{"anchor": c.anchor, "title": c.title, "url": c.url, "text": c.text,
                     "part": c.part} for c in resp.chunks]
        except (TimeoutError, grpc.aio.AioRpcError) as e:
            code = e.code().name if isinstance(e, grpc.aio.AioRpcError) else "TIMEOUT"
            log.warning("retrieval_fallback", code=code)
            self.stats["fallback"] += 1
            return await self.fallback.search(query, None, k)  # keyword-only, in process
