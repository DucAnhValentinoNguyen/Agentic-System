import asyncio
import hashlib
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from .booking import Calendar
from .config import settings
from .gateway.router import build_router
from .graph.build import build_graph
from .retrieval import Index
from .retrieval_client import LocalRetriever, RemoteRetriever
from .store import Store

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.EventRenamer("message"),
    structlog.processors.JSONRenderer(),
])
log = structlog.get_logger()

ORIGINS = [o for o in settings.allowed_origins.split(",") if o]
WIDGET = Path(__file__).resolve().parents[3] / "widget" / "dist" / "twin-widget.js"


class UserMsg(BaseModel):
    type: str = "user"
    session_id: str = Field(min_length=8, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=settings.max_message_chars)


@asynccontextmanager
async def lifespan(app: FastAPI):
    router = build_router()
    index = Index.load()
    vertex = router.providers[0]
    if settings.retrieval_addr:
        retriever = RemoteRetriever(settings.retrieval_addr, settings.retrieval_audience, index)
    else:
        retriever = LocalRetriever(index, vertex.token)
        try:
            await index.warm(await vertex.token())
        except Exception as e:  # noqa: BLE001 — start anyway; BM25 retrieval still works
            log.warning("warm_failed", error=str(e)[:200])
    app.state.retriever = retriever
    app.state.router = router
    app.state.store = Store()
    app.state.tasks = set()  # strong refs to fire-and-forget storage tasks
    app.state.graph = build_graph(router, retriever, Calendar(app.state.store))
    app.state.hits = defaultdict(deque)   # ip -> recent turn timestamps
    app.state.turns = defaultdict(int)    # session -> turn count
    app.state.done_turns = {}             # (session, turn_id) -> final event (idempotent replay)
    log.info("startup", chunks=len(index.chunks), retrieval="remote" if settings.retrieval_addr else "local",
             providers=[p.name for p in router.providers])
    yield


app = FastAPI(title="Twin agent-api", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"],
                   allow_headers=["*"])


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/widget.js")
async def widget():
    return FileResponse(WIDGET, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/v1/status")
async def status():
    r = app.state.router
    ok = [c for c in r.calls if not c.error]
    ttft = sorted(c.ttft_ms for c in ok if c.ttft_ms)
    return {
        "calls": len(r.calls),
        "errors": len(r.calls) - len(ok),
        "fallbacks": sum(1 for c in ok if c.fallback_from),
        "ttft_ms_p50": ttft[len(ttft) // 2] if ttft else None,
        "ttft_ms_p95": ttft[int(len(ttft) * 0.95)] if ttft else None,
        "spent_today_usd": round(r.spent_today_usd, 5),
        "breakers": {p.name: p.breaker.failures for p in r.providers},
    }


class Feedback(BaseModel):
    trace_id: str = Field(min_length=8, max_length=64, pattern=r"^[0-9a-f]+$")
    value: int = Field(ge=-1, le=1)


@app.post("/v1/feedback")
async def feedback(fb: Feedback, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?")
    if ip_limited(ip, 30):
        return JSONResponse({"ok": False}, status_code=429)
    stored = await app.state.store.set_feedback(fb.trace_id, fb.value)
    if stored and settings.langfuse_public_key:  # same score, next to the trace in Langfuse
        get_client().create_score(trace_id=get_client().create_trace_id(seed=fb.trace_id),
                                  name="user_feedback", value=float(fb.value), data_type="NUMERIC")
    log.info("feedback", trace_id=fb.trace_id, value=fb.value, stored=stored)
    return JSONResponse({"ok": True})


def assign_variant(session_id: str) -> str:
    """Stable A/B assignment per session: A = plain RAG, B = RAG + claim verification."""
    if settings.ab_variant in ("A", "B"):
        return settings.ab_variant
    return "B" if int(hashlib.sha256(session_id.encode()).hexdigest(), 16) % 2 else "A"


def client_ip(ws: WebSocket) -> str:
    fwd = ws.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (ws.client.host if ws.client else "?")


def ip_limited(ip: str, per_minute: int) -> bool:
    q = app.state.hits[ip]
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        return True
    q.append(now)
    return False


def rate_limited(ws: WebSocket) -> bool:
    return ip_limited(client_ip(ws), settings.rate_per_minute)


@app.websocket("/ws/chat")
async def chat(ws: WebSocket):
    if ws.headers.get("origin") not in ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            try:
                msg = UserMsg.model_validate(await ws.receive_json())
            except ValidationError:
                await ws.send_json({"type": "error", "code": "bad_request",
                                    "text": "Message is empty or too long."})
                continue
            key = (msg.session_id, msg.turn_id)
            if key in app.state.done_turns:
                # A reconnect resent a finished turn: replay the result, don't run it twice.
                await ws.send_json(app.state.done_turns[key])
                continue
            if rate_limited(ws):
                await ws.send_json({"type": "error", "code": "rate_limited", "turn_id": msg.turn_id,
                                    "text": "Too many messages. Please wait a minute."})
                continue
            if app.state.turns[msg.session_id] >= settings.max_turns_per_session:
                await ws.send_json({"type": "error", "code": "session_limit",
                                    "turn_id": msg.turn_id,
                                    "text": "This conversation reached its limit. Email "
                                            f"{settings.contact_email} to continue."})
                continue
            app.state.turns[msg.session_id] += 1
            await run_turn(ws, msg)
    except WebSocketDisconnect:
        pass


async def run_turn(ws: WebSocket, msg: UserMsg) -> None:
    t0 = time.monotonic()
    trace_id = uuid.uuid4().hex
    cfg = {
        "configurable": {"thread_id": msg.session_id},
        "callbacks": [CallbackHandler()],
        "metadata": {"langfuse_session_id": msg.session_id, "langfuse_tags": ["twin"],
                     "trace_id": trace_id},
    }
    final: dict = {}
    lf = get_client()
    try:
        # One parent span per turn: graph nodes and model calls all nest under it.
        with lf.start_as_current_observation(
            name="twin-turn", as_type="span", input=msg.text,
            trace_context={"trace_id": lf.create_trace_id(seed=trace_id)},  # lets us score it later
        ) as span, propagate_attributes(session_id=msg.session_id, tags=["twin", f"variant:{assign_variant(msg.session_id)}"]):
            await _stream_graph(ws, msg, cfg, final)
            span.update(output=final.get("answer"))
    except WebSocketDisconnect:
        raise
    except Exception as e:  # noqa: BLE001
        log.error("turn_failed", trace_id=trace_id, error=f"{type(e).__name__}: {e}"[:300])
        await ws.send_json({"type": "error", "code": "internal", "turn_id": msg.turn_id,
                            "text": "Something went wrong. Please try again."})
        return
    recs = final.get("records") or []
    done = {
        "type": "done", "turn_id": msg.turn_id, "trace_id": trace_id,
        "text": final.get("answer", ""), "citations": final.get("citations", []),
        "degraded": bool(final.get("degraded")), "choices": final.get("choices") or [], "links": final.get("links") or [],
    }
    app.state.done_turns[(msg.session_id, msg.turn_id)] = done
    await ws.send_json(done)
    task = asyncio.create_task(app.state.store.save_turn(trace_id, {
        "session_id": msg.session_id, "question": msg.text, "answer": done["text"],
        "intent": final.get("intent"), "variant": assign_variant(msg.session_id),
        "citations": [c["anchor"] for c in done["citations"]], "degraded": done["degraded"],
        "sources": [{"anchor": c["anchor"], "title": c["title"], "text": c["text"][:1500]}
                    for c in (final.get("chunks") or [])],
        "research_steps": final.get("steps", 0), "verify": final.get("verify"),
        "latency_ms": round((time.monotonic() - t0) * 1000),
        "cost_usd": round(sum(r.cost_usd for r in recs), 6),
        "providers": [r.provider for r in recs if not r.error],
        "fallback": any(r.fallback_from for r in recs if not r.error)}))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    if settings.langfuse_public_key:
        await asyncio.to_thread(get_client().flush)  # after the reply, so it adds no latency
    log.info(
        "turn", trace_id=trace_id, session_id=msg.session_id, turn_id=msg.turn_id,
        intent=final.get("intent"), variant=assign_variant(msg.session_id),
        verify=final.get("verify"), question=msg.text, answer=done["text"],
        citations=[c["anchor"] for c in done["citations"]], degraded=done["degraded"],
        latency_ms=round((time.monotonic() - t0) * 1000),
        cost_usd=round(sum(r.cost_usd for r in recs), 6),
        providers=[r.provider for r in recs if not r.error],
        fallback=any(r.fallback_from for r in recs if not r.error),
    )


async def _stream_graph(ws: WebSocket, msg: UserMsg, cfg: dict, final: dict) -> None:
    snap = await app.state.graph.aget_state(cfg)
    if snap.next:  # paused at the booking confirmation: this message is the visitor's answer
        graph_input = Command(resume=msg.text, update={"question": msg.text})
    else:
        graph_input = {"question": msg.text, "records": [],
                       "variant": assign_variant(msg.session_id)}
    async for mode, ev in app.state.graph.astream(
        graph_input, cfg, stream_mode=["custom", "updates"]
    ):
        if mode == "custom":
            if ev.get("type") == "step":
                final["steps"] = final.get("steps", 0) + 1
            await ws.send_json({**ev, "turn_id": msg.turn_id})
        else:
            for node, upd in ev.items():
                if node == "__interrupt__":
                    final["paused"] = True
                    continue
                for k, v in (upd or {}).items():
                    if k == "records":  # every node reports its own calls: accumulate, don't overwrite
                        final.setdefault("records", []).extend(v)
                    else:
                        final[k] = v
                await ws.send_json({"type": "node", "node": node, "turn_id": msg.turn_id})
