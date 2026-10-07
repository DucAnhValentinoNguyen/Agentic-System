// Twin chat widget: one script tag, no dependencies. Talks to agent-api over WebSocket.

type Citation = { n: number; anchor: string; title: string; url: string };
type ServerEvent =
  | { type: "delta"; text: string; turn_id: string }
  | { type: "retract"; turn_id: string }
  | { type: "node"; node: string; turn_id: string }
  | { type: "step"; text: string; turn_id: string }
  | { type: "transcript"; text: string; turn_id: string }
  | { type: "done"; turn_id: string; trace_id?: string; text: string; citations: Citation[]; degraded: boolean; choices?: string[]; links?: { label: string; url: string; note?: string; until?: string }[] }
  | { type: "error"; code: string; text: string; turn_id?: string };

const script = document.currentScript as HTMLScriptElement | null;
// Static hosts can serve the UI independently of the agent's cold start.
const API = new URL(script?.dataset.api ?? script?.src ?? location.href).origin;
const WS_URL = API.replace(/^http/, "ws") + "/ws/chat";

function sessionId(): string {
  try {
    let id = localStorage.getItem("twin-session");
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem("twin-session", id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}

const STYLE = `
:host{all:initial;font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:16px;line-height:1.45}
*,*::before,*::after{box-sizing:border-box}
.safe-area{position:fixed;visibility:hidden;pointer-events:none;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
.fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;border:0;border-radius:999px;padding:12px 18px;
 background:#173f56;color:#fff;border:2px solid #7dd3fc;font:inherit;font-weight:600;cursor:grab;box-shadow:0 4px 20px rgba(0,0,0,.4);max-width:calc(100vw - 24px);text-align:left;touch-action:none;user-select:none;min-height:48px}
.fab.dragging{cursor:grabbing;animation:none}
.fab-icon{font-size:22px;vertical-align:middle;margin-right:8px}
.fab-short{display:none}
.fab:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #0284c7;outline-offset:3px}
.panel{position:fixed;right:20px;bottom:76px;z-index:2147483000;width:min(380px,calc(100vw - 32px));
 height:min(540px,calc(100vh - 110px));display:none;flex-direction:column;background:#fff;color:#1a1a1a;
 border:1px solid #d9d9d9;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.22);overflow:hidden}
.panel.open{display:flex}
.head{flex-shrink:0;padding:12px 144px 12px 38px;border-bottom:1px solid #e6e6e6;font-weight:600}
.head small{display:block;font-weight:400;color:#666;margin-top:2px}
.head{cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none}
.head.dragging{cursor:grabbing}
.head button{cursor:pointer}
.connection{display:block;font-size:12px;font-weight:400;color:#666;margin-top:4px}
.log{flex:1;min-height:0;overscroll-behavior:contain;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:88%;padding:8px 11px;border-radius:12px;white-space:pre-wrap;overflow-wrap:anywhere}
.user{align-self:flex-end;background:#1a1a1a;color:#fff}
.bot{align-self:flex-start;background:#f1f1f1}
.bot.err{background:#fdecec;color:#8a1f1f}
.cites{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.cite{border:1px solid #bbb;border-radius:999px;padding:2px 9px;background:#fff;color:#1a1a1a;font:inherit;
 font-size:12px;cursor:pointer}
.cite{text-decoration:none;display:inline-block}
.cite:hover{background:#1a1a1a;color:#fff}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.report-bar{flex-shrink:0;padding:6px 14px;border-bottom:1px solid #e6e6e6;background:#fafafa}
.report{border:1px solid #bbb;border-radius:999px;padding:3px 10px;background:#fff;color:#1a1a1a;font:inherit;font-size:12px;cursor:pointer}
.report:hover{background:#1a1a1a;color:#fff}
.fb{margin-top:6px;font-size:12px;color:#777}
.thumb{border:0;background:transparent;cursor:pointer;font-size:15px;padding:0 6px 0 0;opacity:.7}
.thumb:hover{opacity:1}
.head{position:relative}
.mute{position:absolute;right:52px;top:6px;width:44px;height:44px;border:0;background:transparent;cursor:pointer;font-size:14px;opacity:.6}
.close{position:absolute;right:6px;top:6px;width:44px;height:44px;border:0;background:transparent;color:#1a1a1a;font-size:26px;cursor:pointer}
.expand{position:absolute;right:96px;top:6px;width:44px;height:44px;border:0;background:transparent;color:#1a1a1a;font-size:24px;cursor:pointer}
.resize{position:absolute;left:0;top:0;width:32px;height:32px;padding:0;border:0;background:transparent;color:#666;font-size:20px;cursor:nwse-resize;touch-action:none;user-select:none;z-index:1}
.mute:hover{opacity:1}
.rz{position:absolute;z-index:2;touch-action:none;user-select:none}
.rz-n{top:0;left:32px;right:10px;height:6px;cursor:ns-resize}
.rz-s{bottom:0;left:10px;right:10px;height:6px;cursor:ns-resize}
.rz-e{right:0;top:10px;bottom:10px;width:6px;cursor:ew-resize}
.rz-w{left:0;top:32px;bottom:10px;width:6px;cursor:ew-resize}
.rz-ne{top:0;right:0;width:10px;height:10px;z-index:3;cursor:nesw-resize}
.rz-sw{bottom:0;left:0;width:10px;height:10px;z-index:3;cursor:nesw-resize}
.rz-se{bottom:0;right:0;width:10px;height:10px;z-index:3;cursor:nwse-resize}
.panel.expanded .rz{display:none}
.dot{position:absolute;top:-3px;right:-3px;width:12px;height:12px;border-radius:50%;background:#e5484d;border:2px solid #fff}
.fab.seen .dot{display:none}
@media (prefers-reduced-motion:no-preference){
.fab:not(.seen){animation:pulse 2.4s ease-out 3}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(26,26,26,.45),0 4px 16px rgba(0,0,0,.25)}70%{box-shadow:0 0 0 14px rgba(26,26,26,0),0 4px 16px rgba(0,0,0,.25)}100%{box-shadow:0 0 0 0 rgba(26,26,26,0),0 4px 16px rgba(0,0,0,.25)}}}
.mic{flex-shrink:0;width:44px;min-height:44px;padding:0;border:1px solid #c9c9c9;border-radius:10px;background:#fff;color:#1a1a1a;cursor:pointer;display:flex;align-items:center;justify-content:center}
.mic:hover{background:#f1f1f1}
.mic[hidden]{display:none}
.mic.rec{background:#e5484d;border-color:#e5484d;color:#fff}
.mic:disabled{opacity:.5;cursor:default}
.voice-status{flex-shrink:0;padding:0 14px 6px;font-size:13px;color:#b42318}
.voice-status[hidden]{display:none}
@media (prefers-reduced-motion:no-preference){.mic.rec{animation:recpulse 1.4s ease-out infinite}
@keyframes recpulse{0%{box-shadow:0 0 0 0 rgba(229,72,77,.5)}70%{box-shadow:0 0 0 10px rgba(229,72,77,0)}100%{box-shadow:0 0 0 0 rgba(229,72,77,0)}}}
.steps{margin-bottom:4px}
.step{font-size:12px;color:#777;line-height:1.35}
form{flex-shrink:0;display:flex;gap:8px;padding:10px;border-top:1px solid #e6e6e6}
input{flex:1;min-width:0;padding:9px 11px;border:1px solid #c9c9c9;border-radius:10px;font:inherit;font-size:16px;min-height:44px;color:#1a1a1a;background:#fff}
button.send{border:0;border-radius:10px;padding:0 14px;background:#1a1a1a;color:#fff;font:inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.note{flex-shrink:0;padding:0 14px 2px;color:#777;font-size:12px}
.credit{flex-shrink:0;padding:0 14px 8px;color:#777;font-size:12px}
/* While the chat is open the button steps aside; it keeps its box so the window's anchor math still works. */
:host(.chat-open) .fab{visibility:hidden;pointer-events:none}
@media(max-width:600px){
.rz{display:none}
.fab-long{display:none}.fab-short{display:inline}
.fab{padding:10px 16px}
:host(.chat-open) .fab{display:none}
.panel{border-radius:16px}
.cite{font-size:14px;padding:7px 10px;min-height:36px}
.thumb{min-width:40px;min-height:40px}
}
`;

function mount(): void {
  const host = document.createElement("div");
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>${STYLE}</style>
  <div class="safe-area" aria-hidden="true"></div>
  <button class="fab" aria-expanded="false" aria-label="Chat with Duc-Anh’s twin" title="Tap to chat, or drag to move"><span class="fab-icon" aria-hidden="true">✦</span><span class="fab-long">Hi, I'm Duc-Anh's twin. Ask me about him</span><span class="fab-short">Ask my twin</span><span class="dot" aria-hidden="true"></span></button>
  <div class="panel" role="dialog" aria-label="Chat about Duc-Anh Nguyen">
    <button class="resize" data-edge="nw" type="button" aria-label="Resize chat. Drag any edge or corner, or use the arrow keys on this corner." title="Drag to resize; arrow keys also work">↖</button>
    <div class="rz rz-n" data-edge="n"></div><div class="rz rz-s" data-edge="s"></div><div class="rz rz-e" data-edge="e"></div><div class="rz rz-w" data-edge="w"></div><div class="rz rz-ne" data-edge="ne"></div><div class="rz rz-sw" data-edge="sw"></div><div class="rz rz-se" data-edge="se"></div>
    <div class="head">Duc-Anh's twin<button class="expand" type="button" aria-label="Expand chat" title="Expand chat">⛶</button><button class="close" type="button" aria-label="Close chat">×</button><button class="mute" type="button" aria-label="Mute notification sound" title="Mute notification sound"></button><small>Answers come from this site, with links to the source section.</small><span class="connection" role="status"></span></div>
    <div class="report-bar"><button class="report" type="button" title="Tell Duc-Anh that the chatbot did something wrong">⚠ Report an issue with this chatbot to Duc-Anh</button> <button class="report mycall" type="button" hidden title="Opens the page where you can cancel this call"></button></div>
    <div class="log" aria-live="polite"></div>
    <div class="note">An AI assistant. Messages are logged to improve it; don't share private data. Voice is transcribed on our server and not stored.</div>
    <div class="credit">Made with ❤️ by Đức Anh Valentino Nguyễn</div>
    <div class="voice-status" role="status" aria-live="polite" hidden></div>
    <form><button class="mic" type="button" aria-pressed="false" aria-label="Speak your question" title="Speak your question (up to 30 seconds). The recording is transcribed on our server and not stored."><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="currentColor" d="M12 15a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 0 0-7 0v5.5A3.5 3.5 0 0 0 12 15Zm6-3.5a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.91V20H9.5a1 1 0 1 0 0 2h5a1 1 0 1 0 0-2H13v-2.59A6 6 0 0 0 18 11.5Z"/></svg></button><input maxlength="1000" placeholder="Ask a question" aria-label="Your question"><button class="send">Send</button></form>
  </div>`;
  document.body.appendChild(host);

  const fab = root.querySelector(".fab") as HTMLButtonElement;
  const panel = root.querySelector(".panel") as HTMLDivElement;
  const logEl = root.querySelector(".log") as HTMLDivElement;
  const form = root.querySelector("form") as HTMLFormElement;
  const input = root.querySelector("input") as HTMLInputElement;
  const send = root.querySelector(".send") as HTMLButtonElement;
  const mic = root.querySelector(".mic") as HTMLButtonElement;
  const voiceStatus = root.querySelector(".voice-status") as HTMLDivElement;
  const connection = root.querySelector(".connection") as HTMLSpanElement;
  let rememberBooking: (l: { url: string; note?: string; until?: string }) => void = () => {};
  const sid = sessionId();

  // Wake the API while the visitor reads. This never blocks mounting the UI
  // and does not hold an idle WebSocket open.
  void fetch(API + "/health", { signal: AbortSignal.timeout(30000) }).catch(() => undefined);

  let ws: WebSocket | null = null;
  let pending: {
    turnId: string;
    text: string;
    el: HTMLDivElement;
    body: HTMLSpanElement;
    steps: HTMLDivElement;
    raw: string;
    audio?: string; // the recording (base64 WAV) until its transcript arrives, so a reconnect can resend it
    userEl?: HTMLDivElement; // the visitor's bubble, filled in with the transcript
  } | null = null;

  const add = (cls: string, text = ""): HTMLDivElement => {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.textContent = text;
    logEl.appendChild(el);
    logEl.scrollTop = logEl.scrollHeight;
    return el;
  };

  const addBot = (): { el: HTMLDivElement; body: HTMLSpanElement; steps: HTMLDivElement } => {
    const el = add("bot");
    const steps = document.createElement("div");
    steps.className = "steps";
    const body = document.createElement("span");
    body.textContent = "…";
    el.append(steps, body);
    return { el, body, steps };
  };

  const busy = (b: boolean): void => {
    send.disabled = b;
    mic.disabled = b;
  };

  const goTo = (c: Citation): void => {
    const target = document.getElementById(c.anchor);
    if (!target) {
      window.open(c.url, "_blank", "noopener");
      return;
    }
    // Sections may sit inside collapsed <details>; open them on the way up.
    for (let p: HTMLElement | null = target; p; p = p.parentElement) {
      if (p instanceof HTMLDetailsElement) p.open = true;
    }
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    history.replaceState(null, "", "#" + c.anchor);
  };

  const finish = (
    text: string,
    cites: Citation[],
    choices: string[] = [],
    links: { label: string; url: string; note?: string; until?: string }[] = [],
    traceId?: string,
  ): void => {
    if (!pending) return;
    const el = pending.el;
    // Citations are shown as chips, so drop the inline [n] markers.
    pending.body.textContent = text.replace(/\s*\[\d+(?:\s*,\s*\d+)*\]/g, "");
    if (cites.length) {
      const row = document.createElement("div");
      row.className = "cites";
      for (const c of cites) {
        const b = document.createElement("button");
        b.className = "cite";
        b.type = "button";
        b.textContent = "↗ " + c.title.split(" — ")[0];
        b.title = c.title;
        b.onclick = () => goTo(c);
        row.appendChild(b);
      }
      el.appendChild(row);
    }
    if (links.length) {
      const row = document.createElement("div");
      row.className = "cites";
      for (const l of links) {
        const a = document.createElement("a");
        a.className = "cite";
        a.href = l.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "\u2197 " + l.label;
        row.appendChild(a);
        if (l.label === "Cancel this call" && l.until) rememberBooking(l);
      }
      el.appendChild(row);
    }
    if (choices.length) {
      // Quick replies (booking slots, confirm/cancel) send their label as the next message.
      const row = document.createElement("div");
      row.className = "cites chips";
      for (const label of choices) {
        const b = document.createElement("button");
        b.className = "cite";
        b.type = "button";
        b.textContent = label;
        b.onclick = () => {
          row.remove();
          ask(label);
        };
        row.appendChild(b);
      }
      el.appendChild(row);
    }
    if (traceId && links.length + choices.length === 0 && text.length > 40) {
      // Thumbs feed the online quality score for this turn.
      const fb = document.createElement("div");
      fb.className = "fb";
      for (const [icon, value, label] of [["\ud83d\udc4d", 1, "Helpful"], ["\ud83d\udc4e", -1, "Not helpful"]] as const) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "thumb";
        b.textContent = icon;
        b.title = label;
        b.setAttribute("aria-label", label);
        b.onclick = () => {
          fetch(API + "/v1/feedback", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ trace_id: traceId, value }),
          }).catch(() => undefined);
          fb.textContent = "Thanks for the feedback";
        };
        fb.appendChild(b);
      }
      el.appendChild(fb);
    }
    pending = null;
    busy(false);
    logEl.scrollTop = logEl.scrollHeight;
  };

  const onEvent = (ev: ServerEvent): void => {
    if (!pending || ("turn_id" in ev && ev.turn_id && ev.turn_id !== pending.turnId)) return;
    if (ev.type === "delta") {
      pending.raw += ev.text;
      pending.body.textContent = pending.raw;
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.type === "retract") {
      pending.raw = "";
      pending.body.textContent = "…";
    } else if (ev.type === "step") {
      const s = document.createElement("div");
      s.className = "step";
      s.textContent = "\u2022 " + ev.text;
      pending.steps.appendChild(s);
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.type === "done") {
      finish(ev.text, ev.citations, ev.choices, ev.links, ev.trace_id);
    } else if (ev.type === "transcript") {
      if (pending.userEl) pending.userEl.textContent = ev.text;
      pending.text = ev.text;
      pending.audio = undefined; // from here a reconnect resends the text, never the recording again
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.type === "error") {
      if (pending.audio) pending.userEl?.remove(); // the recording was not understood: drop the empty bubble
      pending.el.classList.add("err");
      pending.body.textContent = ev.text;
      pending = null;
      busy(false);
    }
  };

  let retries = 0;
  const connect = (): void => {
    connection.textContent = retries ? "Reconnecting…" : "Connecting to Twin…";
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      connection.textContent = "Connected";
      retries = 0;
      // Resend an unfinished turn under the same turn id; the server runs it at most once
      // after completion, so a reconnect cannot duplicate it.
      if (pending) {
        pending.raw = "";
        ws!.send(JSON.stringify(pending.audio
          ? { type: "audio", session_id: sid, turn_id: pending.turnId, format: "wav", data: pending.audio }
          : { session_id: sid, turn_id: pending.turnId, text: pending.text }));
      }
    };
    ws.onmessage = (m) => onEvent(JSON.parse(m.data) as ServerEvent);
    ws.onclose = () => {
      ws = null;
      connection.textContent = "Disconnected";
      if (!panel.classList.contains("open") && !pending) return;
      if (retries++ < 4) {
        connection.textContent = "Reconnecting…";
        setTimeout(connect, 500 * 2 ** retries);
      } else if (pending) {
        pending.el.classList.add("err");
        pending.body.textContent = "Connection lost. Please try again.";
        pending = null;
        busy(false);
      }
    };
  };

  const ask = (text: string): void => {
    if (pending || !text.trim()) return;
    add("user", text);
    pending = { turnId: crypto.randomUUID(), text, ...addBot(), raw: "" };
    busy(true);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ session_id: sid, turn_id: pending.turnId, text }));
    } else if (!ws) {
      connect();
    }
  };

  // --- Voice input: push-to-talk. Record, convert to 16 kHz mono WAV, send over the same WebSocket. ---
  const MAX_SECONDS = 30;
  const MIN_SECONDS = 0.4;
  const AudioCtx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  const canRecord = Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined" && Boolean(AudioCtx);
  mic.hidden = !canRecord;

  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let recStarted = 0;
  let timer = 0;
  let discard = false;

  const status = (text: string): void => {
    voiceStatus.textContent = text;
    voiceStatus.hidden = !text;
  };
  const releaseMic = (): void => {
    window.clearInterval(timer);
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    recorder = null;
    mic.classList.remove("rec");
    mic.setAttribute("aria-pressed", "false");
    mic.setAttribute("aria-label", "Speak your question");
  };

  // Decode whatever the browser recorded (webm/opus, mp4/aac) and resample to 16 kHz mono 16-bit PCM WAV.
  const toWav = async (blob: Blob): Promise<string> => {
    const ctx = new AudioCtx();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    void ctx.close();
    const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * 16000)), 16000);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);
    src.start();
    const pcm = (await off.startRendering()).getChannelData(0);
    const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const str = (o: number, s: string): void => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
    str(0, "RIFF");
    out.setUint32(4, 36 + pcm.length * 2, true);
    str(8, "WAVEfmt ");
    out.setUint32(16, 16, true);
    out.setUint16(20, 1, true); // PCM
    out.setUint16(22, 1, true); // mono
    out.setUint32(24, 16000, true);
    out.setUint32(28, 32000, true);
    out.setUint16(32, 2, true);
    out.setUint16(34, 16, true);
    str(36, "data");
    out.setUint32(40, pcm.length * 2, true);
    pcm.forEach((v, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, v)) * 0x7fff, true));
    const bytes = new Uint8Array(out.buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  };

  const askVoice = (audio: string): void => {
    if (pending) return;
    const userEl = add("user", "…");
    pending = { turnId: crypto.randomUUID(), text: "", audio, userEl, ...addBot(), raw: "" };
    busy(true);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "audio", session_id: sid, turn_id: pending.turnId, format: "wav", data: audio }));
    } else if (!ws) {
      connect();
    }
  };

  const problem = (text: string): void => {
    const el = add("bot", text);
    el.classList.add("err");
  };

  const startRecording = async (): Promise<void> => {
    if (pending || recorder) return;
    status("Waiting for microphone permission…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      status("");
      problem("I can't use the microphone. Allow it for this site in your browser's settings, or type your question instead.");
      return;
    }
    chunks = [];
    discard = false;
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
      const seconds = (Date.now() - recStarted) / 1000;
      const mime = recorder?.mimeType || chunks[0]?.type || "audio/webm";
      releaseMic();
      status("");
      if (discard) return;
      if (seconds < MIN_SECONDS || !chunks.length) {
        problem("I didn't catch anything. Tap the microphone, speak, then tap it again.");
        return;
      }
      status("Transcribing…");
      try {
        askVoice(await toWav(new Blob(chunks, { type: mime })));
      } catch {
        problem("I couldn't process that recording. Please try again or type your question.");
      } finally {
        status("");
      }
    };
    recStarted = Date.now();
    recorder.start();
    mic.classList.add("rec");
    mic.setAttribute("aria-pressed", "true");
    mic.setAttribute("aria-label", "Stop recording and send");
    const tick = (): void => {
      const s = Math.floor((Date.now() - recStarted) / 1000);
      status(`Recording… ${s}s. Tap the microphone to send.`);
      if (s >= MAX_SECONDS) recorder?.stop();
    };
    tick();
    timer = window.setInterval(tick, 250);
  };

  const cancelRecording = (): void => {
    if (recorder && recorder.state !== "inactive") {
      discard = true;
      recorder.stop();
    } else {
      releaseMic();
      status("");
    }
  };

  mic.onclick = () => {
    if (recorder && recorder.state !== "inactive") recorder.stop();
    else void startRecording();
  };
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && recorder) cancelRecording();
  });

  // --- Announce arrival: pulse + dot (always), and a soft chime (once per visit, mutable). ---
  const store = {
    get: (k: string, s: Storage): string | null => {
      try {
        return s.getItem(k);
      } catch {
        return null;
      }
    },
    set: (k: string, v: string, s: Storage): void => {
      try {
        s.setItem(k, v);
      } catch {
        /* storage may be blocked; the cue just repeats */
      }
    },
  };
  const muteBtn = root.querySelector(".mute") as HTMLButtonElement;
  const muted = (): boolean => store.get("twin-muted", localStorage) === "1";
  const paintMute = (): void => {
    muteBtn.textContent = muted() ? "\ud83d\udd15" : "\ud83d\udd14";
    muteBtn.title = muted() ? "Unmute notification sound" : "Mute notification sound";
  };
  muteBtn.onclick = () => {
    store.set("twin-muted", muted() ? "0" : "1", localStorage);
    paintMute();
  };
  paintMute();

  // A fresh AudioContext per attempt: one created before a user gesture can stay stuck "suspended".
  const tryChime = async (): Promise<boolean> => {
    try {
      const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctx) return true; // nothing to play with: treat as handled
      const audio = new Ctx();
      if (audio.state === "suspended") {
        // Allowed only after a user gesture. Never wait on a blocked resume(): it can stay pending forever.
        await Promise.race([audio.resume().catch(() => undefined), new Promise((r) => window.setTimeout(r, 400))]);
      }
      if (audio.state !== "running") {
        void audio.close().catch(() => undefined);
        return false; // autoplay blocked: wait for a user gesture
      }
      const t0 = audio.currentTime;
      for (const [freq, at] of [[880, 0], [1318.5, 0.16]] as const) {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.exponentialRampToValueAtTime(0.07, t0 + at + 0.02); // quiet on purpose
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.45);
        osc.connect(gain).connect(audio.destination);
        osc.start(t0 + at);
        osc.stop(t0 + at + 0.5);
      }
      window.setTimeout(() => void audio.close().catch(() => undefined), 1500);
      return true;
    } catch {
      return true;
    }
  };
  const announce = async (): Promise<void> => {
    if (store.get("twin-pinged", sessionStorage) === "1" || muted()) return;
    const finish = (): void => store.set("twin-pinged", "1", sessionStorage);
    if (await tryChime()) return finish();
    // Only some events count as the "user activation" that unlocks audio (not pointerdown in Chrome).
    const gestures = ["click", "pointerup", "keydown", "touchend"];
    const onGesture = async (): Promise<void> => {
      for (const g of gestures) window.removeEventListener(g, onGesture, true);
      if (!panel.classList.contains("open") && !muted() && (await tryChime())) finish();
    };
    for (const g of gestures) window.addEventListener(g, onGesture, true);
  };
  if (store.get("twin-opened", sessionStorage) === "1") fab.classList.add("seen");
  else window.setTimeout(() => void announce(), 1200);

  const mobile = window.matchMedia("(max-width:600px)");
  const safeArea = root.querySelector(".safe-area") as HTMLDivElement;
  const viewport = () => {
    const vv = window.visualViewport;
    const safe = getComputedStyle(safeArea);
    return {
      x: (vv?.offsetLeft ?? 0) + 12 + (parseFloat(safe.paddingLeft) || 0),
      y: (vv?.offsetTop ?? 0) + 12 + (parseFloat(safe.paddingTop) || 0),
      right: (vv?.offsetLeft ?? 0) + (vv?.width ?? window.innerWidth) - 12 - (parseFloat(safe.paddingRight) || 0),
      bottom: (vv?.offsetTop ?? 0) + (vv?.height ?? window.innerHeight) - 12 - (parseFloat(safe.paddingBottom) || 0),
    };
  };
  const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(value, Math.max(min, max)));
  // Relative coordinates survive rotation and window resizing.
  let position: { x: number; y: number } | null = null;
  try {
    const saved = JSON.parse(localStorage.getItem("twin-position") ?? "null");
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      position = { x: clamp(saved.x, 0, 1), y: clamp(saved.y, 0, 1) };
    }
  } catch { /* storage may be unavailable */ }

  let size: { width: number; height: number } | null = null;
  let panelPosition: { x: number; y: number } | null = null;
  let expanded = false;
  try {
    const saved = JSON.parse(localStorage.getItem("twin-size") ?? "null");
    if (saved && Number.isFinite(saved.width) && Number.isFinite(saved.height)
        && saved.width > 0 && saved.height > 0) size = saved;
  } catch { /* optional preference */ }

  const layout = (): void => {
    const v = viewport();
    if (fab.offsetWidth) {
      fab.style.left = `${v.x + (position?.x ?? 1) * Math.max(0, v.right - v.x - fab.offsetWidth)}px`;
      fab.style.top = `${v.y + (position?.y ?? 1) * Math.max(0, v.bottom - v.y - fab.offsetHeight)}px`;
      fab.style.right = fab.style.bottom = "auto";
    }
    const availableWidth = Math.max(0, v.right - v.x);
    const availableHeight = Math.max(0, v.bottom - v.y);
    const width = expanded ? availableWidth : clamp(size?.width ?? (mobile.matches ? availableWidth : 380), Math.min(280, availableWidth), availableWidth);
    const height = expanded ? availableHeight : clamp(size?.height ?? (mobile.matches ? availableHeight : 540), Math.min(300, availableHeight), availableHeight);
    const f = fab.getBoundingClientRect();
    panel.style.width = `${width}px`;
    panel.style.height = `${height}px`;
    panel.style.left = `${expanded ? v.x : clamp(panelPosition?.x ?? (mobile.matches ? v.x : f.right - width), v.x, v.right - width)}px`;
    const top = f.top - height - 12 >= v.y ? f.top - height - 12 : f.bottom + 12;
    panel.style.top = `${expanded ? v.y : clamp(panelPosition?.y ?? (mobile.matches ? v.y : top), v.y, v.bottom - height)}px`;
    panel.style.right = panel.style.bottom = "auto";
  };
  window.addEventListener("resize", layout);
  window.visualViewport?.addEventListener("resize", layout);
  window.visualViewport?.addEventListener("scroll", layout);
  layout();

  const resize = root.querySelector(".resize") as HTMLButtonElement;
  const expand = root.querySelector(".expand") as HTMLButtonElement;
  // A booked call is remembered in this browser (only the cancel link and the time), so the same device can cancel it
  // later without a login. The page the link opens is the only thing that can cancel; it asks for one more click.
  const mycall = root.querySelector(".mycall") as HTMLButtonElement;
  const showBooking = (): void => {
    let b: { url: string; note: string; until: string } | null = null;
    try { b = JSON.parse(store.get("twin-booking", localStorage) || "null"); } catch { b = null; }
    if (!b || !b.url || !(Date.parse(b.until) > Date.now())) { mycall.hidden = true; return; }
    mycall.textContent = "\ud83d\udcc5 My call: " + b.note.replace(" (Berlin time)", "") + " \u00b7 cancel";
    mycall.onclick = () => window.open(b!.url, "_blank", "noopener,noreferrer");
    mycall.hidden = false;
  };
  rememberBooking = (l) => {
    store.set("twin-booking", JSON.stringify({ url: l.url, note: l.note || "", until: l.until }), localStorage);
    showBooking();
  };
  showBooking();
  // Always visible, unlike the example chips: one tap starts a problem report that is emailed to Duc-Anh.
  (root.querySelector(".report") as HTMLButtonElement).onclick = () => ask("Report an issue with this chatbot to Duc-Anh");
  const paintExpand = (): void => {
    expand.title = expanded ? "Restore chat size" : "Expand chat";
    expand.setAttribute("aria-label", expand.title);
    expand.setAttribute("aria-pressed", String(expanded));
    panel.classList.toggle("expanded", expanded);
  };
  const saveSize = (): void => {
    try { localStorage.setItem("twin-size", JSON.stringify(size)); } catch { /* optional */ }
  };
  type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
  // Move the dragged edges by (dx, dy); the opposite edges stay put. Always kept inside the viewport.
  const resizeBy = (edge: Edge, rect: DOMRect, dx: number, dy: number): void => {
    const v = viewport();
    const minW = Math.min(280, v.right - v.x), minH = Math.min(300, v.bottom - v.y);
    let { left, right, top, bottom } = rect;
    if (edge.includes("w")) left = clamp(rect.left + dx, v.x, rect.right - minW);
    if (edge.includes("e")) right = clamp(rect.right + dx, rect.left + minW, v.right);
    if (edge.includes("n")) top = clamp(rect.top + dy, v.y, rect.bottom - minH);
    if (edge.includes("s")) bottom = clamp(rect.bottom + dy, rect.top + minH, v.bottom);
    size = { width: right - left, height: bottom - top };
    panelPosition = { x: left, y: top };
    expanded = false;
    paintExpand();
    layout();
  };
  let resizing: { id: number; edge: Edge; x: number; y: number; rect: DOMRect } | null = null;
  const endResize = (e: PointerEvent): void => {
    if (!resizing || resizing.id !== e.pointerId) return;
    resizing = null;
    saveSize();
  };
  for (const h of [resize, ...Array.from(root.querySelectorAll<HTMLElement>(".rz"))]) {
    h.addEventListener("pointerdown", (e) => {
      if (!e.isPrimary || e.button !== 0 || expanded) return;
      e.preventDefault();
      resizing = { id: e.pointerId, edge: (h.dataset.edge ?? "nw") as Edge, x: e.clientX, y: e.clientY,
                   rect: panel.getBoundingClientRect() };
      h.setPointerCapture(e.pointerId);
    });
    h.addEventListener("pointermove", (e) => {
      if (!resizing || resizing.id !== e.pointerId) return;
      resizeBy(resizing.edge, resizing.rect, e.clientX - resizing.x, e.clientY - resizing.y);
    });
    h.addEventListener("pointerup", endResize);
    h.addEventListener("pointercancel", endResize);
    h.addEventListener("lostpointercapture", endResize);
  }
  resize.addEventListener("keydown", (e) => {
    const dx = e.key === "ArrowLeft" ? -32 : e.key === "ArrowRight" ? 32 : 0;
    const dy = e.key === "ArrowUp" ? -32 : e.key === "ArrowDown" ? 32 : 0;
    if (!dx && !dy) return;
    e.preventDefault();
    resizeBy("nw", panel.getBoundingClientRect(), dx, dy);
    saveSize();
  });
  expand.onclick = () => {
    expanded = !expanded;
    panelPosition = null;
    paintExpand();
    layout();
  };
  paintExpand();

  let drag: { id: number; x: number; y: number; left: number; top: number; moved: boolean } | null = null;
  let suppressClick = false;
  fab.addEventListener("pointerdown", (e) => {
    if (!e.isPrimary || e.button !== 0) return;
    const rect = fab.getBoundingClientRect();
    suppressClick = false;
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, left: rect.left, top: rect.top, moved: false };
    fab.setPointerCapture(e.pointerId);
  });
  fab.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 8) return;
    drag.moved = true;
    fab.classList.add("dragging");
    const v = viewport();
    const left = clamp(drag.left + dx, v.x, v.right - fab.offsetWidth);
    const top = clamp(drag.top + dy, v.y, v.bottom - fab.offsetHeight);
    position = {
      x: (left - v.x) / Math.max(1, v.right - v.x - fab.offsetWidth),
      y: (top - v.y) / Math.max(1, v.bottom - v.y - fab.offsetHeight),
    };
    panelPosition = null;
    layout();
  });
  const endDrag = (e: PointerEvent): void => {
    if (!drag || drag.id !== e.pointerId) return;
    suppressClick = drag.moved;
    // Touch browsers may omit the synthetic click after pointer capture.
    // Activate a stationary touch directly, and consume any later synthetic click.
    if (e.type === "pointerup" && !drag.moved && e.pointerType === "touch") {
      setOpen(!panel.classList.contains("open"));
      suppressClick = true;
    }
    if (drag.moved && position) {
      try { localStorage.setItem("twin-position", JSON.stringify(position)); } catch { /* optional */ }
    }
    drag = null;
    fab.classList.remove("dragging");
  };
  fab.addEventListener("pointerup", endDrag);
  fab.addEventListener("pointercancel", endDrag);
  fab.addEventListener("lostpointercapture", endDrag);

  // Drag the chat window by its header bar, anywhere on screen. Off on phones (the window fills the
  // screen there) and while expanded; the header buttons keep working.
  const head = root.querySelector(".head") as HTMLDivElement;
  let moving: { id: number; x: number; y: number; left: number; top: number } | null = null;
  head.addEventListener("pointerdown", (e) => {
    if (!e.isPrimary || e.button !== 0 || expanded || mobile.matches) return;
    if ((e.target as HTMLElement).closest("button")) return;
    const rect = panel.getBoundingClientRect();
    moving = { id: e.pointerId, x: e.clientX, y: e.clientY, left: rect.left, top: rect.top };
    head.setPointerCapture(e.pointerId);
    head.classList.add("dragging");
  });
  head.addEventListener("pointermove", (e) => {
    if (!moving || moving.id !== e.pointerId) return;
    const v = viewport();
    panelPosition = {
      x: clamp(moving.left + e.clientX - moving.x, v.x, v.right - panel.offsetWidth),
      y: clamp(moving.top + e.clientY - moving.y, v.y, v.bottom - panel.offsetHeight),
    };
    layout();
  });
  const endMove = (e: PointerEvent): void => {
    if (!moving || moving.id !== e.pointerId) return;
    moving = null;
    head.classList.remove("dragging");
  };
  head.addEventListener("pointerup", endMove);
  head.addEventListener("pointercancel", endMove);
  head.addEventListener("lostpointercapture", endMove);

  let greeted = false;
  const setOpen = (open: boolean): void => {
    panel.classList.toggle("open", open);
    host.classList.toggle("chat-open", open);
    fab.setAttribute("aria-expanded", String(open));
    fab.classList.add("seen");
    store.set("twin-opened", "1", sessionStorage);
    if (!open) {
      cancelRecording();
      input.blur();
      layout();
      fab.focus({ preventScroll: true });
      return;
    }
    layout();
    if (!ws) connect();
    if (!greeted) {
      greeted = true;
      const g = add("bot", "Hi, I'm Duc-Anh's twin, an AI assistant that answers from this site. For example:");
      const row = document.createElement("div");
      row.className = "cites chips";
      for (const q of ["What is he working on now?", "Which agent projects has he built?", "How do I schedule a meeting with him?", "Leave Duc-Anh a message"]) {
        const b = document.createElement("button");
        b.className = "cite";
        b.type = "button";
        b.textContent = q;
        b.onclick = () => ask(q === "Leave Duc-Anh a message" ? "I'd like to leave Duc-Anh a message" : q);
        row.appendChild(b);
      }
      g.appendChild(row);
    }
    // On phones, show the conversation before the visitor chooses to open the keyboard.
    if (!mobile.matches) input.focus({ preventScroll: true });
  };
  fab.onclick = (e) => {
    if (suppressClick && (e.detail !== 0 || (e as PointerEvent).pointerType === "touch")) {
      suppressClick = false;
      return;
    }
    setOpen(!panel.classList.contains("open"));
  };
  (root.querySelector(".close") as HTMLButtonElement).onclick = () => setOpen(false);
  root.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Escape" && panel.classList.contains("open")) setOpen(false);
  });

  form.onsubmit = (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = "";
    ask(text);
  };
}

// Preview gate: visible only after visiting with ?twin=1 (remembered), until launch.
function enabled(): boolean {
  if (script?.dataset.live === "1") return true;
  try {
    const q = new URLSearchParams(location.search).get("twin");
    if (q === "1") localStorage.setItem("twin-preview", "1");
    if (q === "0") localStorage.removeItem("twin-preview");
    return localStorage.getItem("twin-preview") === "1";
  } catch {
    return false;
  }
}

if (enabled()) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
}
