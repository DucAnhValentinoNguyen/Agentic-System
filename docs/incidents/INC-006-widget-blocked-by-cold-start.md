# INC-006: chat button waited for the Python backend to start

**Detection (3 Oct 2026).** Duc-Anh reported roughly 12 seconds before the chat button appeared on a
new website tab. Cloud Run request logs confirmed two `/widget.js` requests on revision
`twin-agent-api-00025-5vb` took 15.10 s and 15.08 s; nearby warm requests took about 3 ms.
Each slow request coincided with an autoscaling instance start. The app startup-complete message
arrived roughly 13 s after the instance-start message, followed by a successful startup probe.

**Cause.** The portfolio loaded its JavaScript directly from the agent API. That service has
`min_instance_count = 0`, so the script request could trigger a cold start. Container startup,
Python dependency imports, agent initialization and readiness all preceded serving the small
static widget file. No answer-model call was needed to display the button, but the UI shared the
backend's startup dependency.

**Fix.** Serve a content-hashed widget bundle through the portfolio's GitHub Pages assets. A
`data-api` attribute keeps WebSocket and feedback calls pointed at Cloud Run. The widget starts a
non-blocking health request when it mounts, giving the backend a head start while a visitor reads.
The dialog displays connection status. It opens a WebSocket only when the visitor opens the chat.
`scripts/sync_widget.py` prepares future static bundle and HTML updates.

**Confirmation.** A simulated unavailable-backend test mounted the widget in 83 ms and opened the
greeting immediately. After publishing portfolio commit `c9963a8`, a fresh 390 px touch-browser
test measured the button mounting 308 ms after navigation (static script download: 128 ms).
The backend health request was deliberately left stalled; the button and greeting still worked.

**Limits.** These are individual browser measurements, not a mobile population latency benchmark.
The agent API still scales to zero. Opening the UI is independent of startup, but an immediate
first question can still wait for the backend to become ready. Keeping one instance warm is a
separate cost/latency decision. Backend image deployment alone does not publish widget changes
to the portfolio's static assets.
