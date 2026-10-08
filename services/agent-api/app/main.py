import asyncio
import hashlib
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from . import cancel, stt
from .booking import Calendar
from .checkpoint import FirestoreSaver
from .config import settings
from .gateway.router import ProviderError, build_router
from .graph.build import build_graph, site_topics
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


class AudioMsg(BaseModel):
    """A push-to-talk recording: 16 kHz mono 16-bit WAV, base64. Held in memory only; never stored or logged."""
    type: Literal["audio"]
    session_id: str = Field(min_length=8, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    format: Literal["wav"] = "wav"
    data: str = Field(min_length=100, max_length=settings.max_audio_b64_chars)


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
    app.state.vocab = stt.vocabulary([c["title"] for c in index.chunks])
    app.state.router = router
    app.state.store = Store()
    app.state.tasks = set()  # strong refs to fire-and-forget storage tasks
    saver = (FirestoreSaver(app.state.store.db())
             if settings.session_store == "firestore" and app.state.store.enabled else None)
    app.state.durability = "exit" if saver else "async"  # one Firestore write per turn instead of one per step
    app.state.calendar = Calendar(app.state.store)
    app.state.graph = build_graph(router, retriever, app.state.calendar,
                                  topics=site_topics([c["title"] for c in index.chunks]), checkpointer=saver)
    app.state.hits = defaultdict(deque)   # ip -> recent turn timestamps
    app.state.turns = defaultdict(int)    # session -> turn count
    app.state.done_turns = {}             # (session, turn_id) -> final event (idempotent replay)
    app.state.audio_day, app.state.audio_clips = time.strftime("%Y-%m-%d"), 0
    app.state.ip_day = {}                 # salted daily hash of the address -> turns today
    app.state.conns = defaultdict(int)    # address -> open chat connections (this instance)
    app.state.turns_shared, app.state.turns_pending = 0, 0  # turns today: persisted total, and not yet written
    await router.sync_spend(app.state.store)  # a restart must not reset the day's spend
    await sync_turns()

    async def spend_loop() -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await router.sync_spend(app.state.store)
                await sync_turns()
            except Exception as e:  # noqa: BLE001
                log.warning("spend_sync_failed", error=str(e)[:200])

    spend_task = asyncio.create_task(spend_loop())
    log.info("startup", chunks=len(index.chunks), retrieval="remote" if settings.retrieval_addr else "local",
             sessions="firestore" if saver else "memory",
             providers=[p.name for p in router.providers])
    yield
    spend_task.cancel()
    await router.sync_spend(app.state.store)  # do not lose the last seconds of spend on shutdown
    await sync_turns()


app = FastAPI(title="Twin agent-api", lifespan=lifespan)
app.include_router(cancel.make_router(
    lambda: app.state.calendar, lambda: settings.cancel_secret,
    lambda request: ip_limited("cancel:" + client_ip_http(request), 20)))
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
        "spent_today_usd": round(max(r.spent_today_usd, r.shared_day + r.pending_usd), 5),
        "spent_month_usd": round(r.shared_month + r.pending_usd, 5),
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


def client_ip_http(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def rate_limited(ws: WebSocket) -> bool:
    return ip_limited(client_ip(ws), settings.rate_per_minute)


async def sync_turns() -> None:
    """Write the turns served here since the last call to the shared daily total, then read the total back."""
    state = app.state
    n, state.turns_pending = state.turns_pending, 0
    if n and not await state.store.add_turns(n):
        state.turns_pending += n
    got = await state.store.get_turns()
    if got is not None:
        state.turns_shared = got


def capacity_reached() -> bool:
    return app.state.turns_shared + app.state.turns_pending >= settings.daily_turns_total


async def ip_day_limited(ws: WebSocket) -> bool:
    """Per-visitor daily cap that survives restarts and new chats. The key is a salted daily hash, not the address."""
    key = hashlib.sha256(f"{client_ip(ws)}|{time.strftime('%Y-%m-%d')}".encode()).hexdigest()[:16]
    seen = app.state.ip_day
    if len(seen) > 5000:
        seen.clear()
    n = seen.get(key)
    if n is None:
        n = await app.state.store.get_ip_turns(key) or 0
    if n >= settings.daily_turns_per_ip:
        seen[key] = n
        return True
    seen[key] = n + 1
    task = asyncio.create_task(app.state.store.bump_ip(key))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    return False


async def admit(ws: WebSocket, session_id: str, turn_id: str) -> bool:
    """Gate shared by typed and spoken turns. Sends the refusal itself and returns False if refused."""
    key = (session_id, turn_id)
    if key in app.state.done_turns:
        # A reconnect resent a finished turn: replay the result, don't run it twice.
        await ws.send_json(app.state.done_turns[key])
        return False
    if rate_limited(ws):
        await ws.send_json({"type": "error", "code": "rate_limited", "turn_id": turn_id,
                            "text": "Too many messages. Please wait a minute."})
        return False
    if capacity_reached():
        await ws.send_json({"type": "error", "code": "daily_capacity", "turn_id": turn_id,
                            "text": "Twin has reached its capacity for today. Please come back tomorrow or email "
                                    f"{settings.contact_email}."})
        return False
    if await ip_day_limited(ws):
        await ws.send_json({"type": "error", "code": "daily_limit", "turn_id": turn_id,
                            "text": "You have reached today's limit from this connection. Please come back "
                                    f"tomorrow or email {settings.contact_email}."})
        return False
    if app.state.turns[session_id] >= settings.max_turns_per_session:
        await ws.send_json({"type": "error", "code": "session_limit", "turn_id": turn_id,
                            "text": "This conversation reached its limit. Email "
                                    f"{settings.contact_email} to continue."})
        return False
    app.state.turns_pending += 1
    return True


def audio_cap_reached() -> bool:
    today = time.strftime("%Y-%m-%d")
    if today != app.state.audio_day:
        app.state.audio_day, app.state.audio_clips = today, 0
    return app.state.audio_clips >= settings.daily_audio_clips


async def transcribe_turn(ws: WebSocket, a: AudioMsg) -> UserMsg | None:
    """Turn a recording into a UserMsg, or send an error and return None. The audio is never logged."""
    def refuse(code: str, text: str):
        return ws.send_json({"type": "error", "code": code, "turn_id": a.turn_id, "text": text})

    if ip_limited(f"audio:{client_ip(ws)}", settings.audio_per_minute) or audio_cap_reached():
        await refuse("audio_rate_limited", "Too many recordings for now. Please type your question instead.")
        return None
    if app.state.router.over_budget():
        await refuse("stt_failed", "Voice input is paused for today. Please type your question.")
        return None
    app.state.audio_clips += 1
    t0 = time.monotonic()
    try:
        wav, seconds = stt.decode_wav(a.data)
        tr = await stt.transcribe(app.state.router, wav, seconds, app.state.vocab)
    except stt.AudioError as e:
        await refuse(e.code, e.message)
        return None
    except ProviderError:
        await refuse("stt_failed", "I couldn't transcribe that just now. Please type your question.")
        return None
    last = tr.records[-1] if tr.records else None
    log.info("stt", session_id=a.session_id, turn_id=a.turn_id, audio_s=round(tr.seconds, 1),
             latency_ms=round((time.monotonic() - t0) * 1000), chars=len(tr.text),
             cost_usd=round(sum(r.cost_usd for r in tr.records), 6),
             provider=last.provider if last else None)
    if settings.langfuse_public_key:  # a record of the transcription itself, without any audio
        with get_client().start_as_current_observation(
            name="stt", as_type="generation", model=last.model if last else None,
            input=f"[voice clip, {tr.seconds:.1f} s]", output=tr.text,
        ):
            pass
    await ws.send_json({"type": "transcript", "turn_id": a.turn_id, "text": tr.text})
    return UserMsg(session_id=a.session_id, turn_id=a.turn_id, text=tr.text)


@app.websocket("/ws/chat")
async def chat(ws: WebSocket):
    if ws.headers.get("origin") not in ORIGINS:
        await ws.close(code=1008)
        return
    ip = client_ip(ws)
    if app.state.conns[ip] >= settings.max_connections_per_ip:
        await ws.close(code=1013)  # try again later
        return
    await ws.accept()
    app.state.conns[ip] += 1
    try:
        while True:
            try:
                payload = await asyncio.wait_for(ws.receive_json(), settings.idle_timeout_s)
            except TimeoutError:  # nobody is typing: free the connection (the widget reconnects when needed)
                await ws.close(code=1000)
                return
            spoken = isinstance(payload, dict) and payload.get("type") == "audio"
            try:
                if spoken:
                    audio = AudioMsg.model_validate(payload)
                    session_id, turn_id = audio.session_id, audio.turn_id
                else:
                    msg = UserMsg.model_validate(payload)
                    session_id, turn_id = msg.session_id, msg.turn_id
            except ValidationError:
                await ws.send_json({
                    "type": "error", "code": "bad_audio" if spoken else "bad_request",
                    "text": "That recording was empty or too long." if spoken else "Message is empty or too long."})
                continue
            if not await admit(ws, session_id, turn_id):
                continue
            if spoken:
                msg = await transcribe_turn(ws, audio)
                if msg is None:
                    continue
            app.state.turns[session_id] += 1
            await run_turn(ws, msg, voice=spoken)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.conns[ip] -= 1
        if app.state.conns[ip] <= 0:
            app.state.conns.pop(ip, None)


async def run_turn(ws: WebSocket, msg: UserMsg, voice: bool = False) -> None:
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
        "degraded": bool(final.get("degraded")), "choices": final.get("choices") or [], "links": final.get("links") or [], "image": final.get("image"),
    }
    app.state.done_turns[(msg.session_id, msg.turn_id)] = done
    await ws.send_json(done)
    task = asyncio.create_task(app.state.store.save_turn(trace_id, {
        "session_id": msg.session_id, "question": msg.text, "answer": done["text"],
        "input": "voice" if voice else "text",
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
        intent=final.get("intent"), input="voice" if voice else "text", variant=assign_variant(msg.session_id),
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
        graph_input = Command(resume=msg.text, update={"question": msg.text, "session_id": msg.session_id})
    else:
        graph_input = {"question": msg.text, "records": [], "session_id": msg.session_id,
                       "variant": assign_variant(msg.session_id)}
    async for mode, ev in app.state.graph.astream(
        graph_input, cfg, stream_mode=["custom", "updates"], durability=app.state.durability
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
