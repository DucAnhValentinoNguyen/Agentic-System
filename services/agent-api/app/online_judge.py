"""Online scoring job: judge a sample of recent production answers against the sources they used.

Runs as a Cloud Run Job on a schedule (`python -m app.online_judge`). Scores are written back to the
turn document in Firestore and attached to the Langfuse trace, so live quality is visible next to
the offline numbers.
"""

import asyncio
import os

import structlog
from langfuse import get_client

from . import judge as judge_mod
from .store import Store

log = structlog.get_logger()
structlog.configure(processors=[structlog.processors.add_log_level,
                                structlog.processors.EventRenamer("message"),
                                structlog.processors.JSONRenderer()])
MAX_PER_RUN = int(os.environ.get("JUDGE_MAX_PER_RUN", "8"))  # free-tier judge quota is small


async def main() -> None:
    store = Store()
    if not store.enabled:
        log.warning("online_judge_disabled")
        return
    todo = await store.pending_judgments(MAX_PER_RUN)
    client, lf = judge_mod.client(), get_client()
    done = failed = 0
    for trace_id, d in todo:
        j = await judge_mod.judge(client, d["question"], d["answer"], d.get("sources") or [])
        if j.get("judge_failed"):
            failed += 1
            continue
        await store.set_judge(trace_id, {k: j[k] for k in ("claims", "unsupported", "unsupported_text")})
        lf_id = lf.create_trace_id(seed=trace_id)
        lf.create_score(trace_id=lf_id, name="unsupported_claims", value=float(j["unsupported"]),
                        data_type="NUMERIC", comment="; ".join(j["unsupported_text"])[:500] or None)
        lf.create_score(trace_id=lf_id, name="has_unsupported", value=float(j["unsupported"] > 0),
                        data_type="NUMERIC")
        log.info("online_judge_result", trace_id=trace_id, claims=j["claims"],
                 unsupported=j["unsupported"], variant=d.get("variant"))
        done += 1
    lf.flush()
    log.info("online_judge_run", judged=done, failed=failed, pending_seen=len(todo))


if __name__ == "__main__":
    asyncio.run(main())
