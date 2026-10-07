"""LangGraph checkpointer on Firestore: a chat's state survives restarts and can be served by any instance.

The default MemorySaver keeps every conversation in the memory of one process, which is why the service had to run as a
single instance. Layout (all documents carry expire_at, a TTL policy deletes them after a few days):

  sessions/{thread}                               a pointer to the latest checkpoint id (no index or query needed)
  sessions/{thread}/checkpoints/{checkpoint_id}   the checkpoint without channel values, its metadata, the parent id
  sessions/{thread}/blobs/{ns}|{channel}|{ver}    one channel value at one version (written only when it changes)
  sessions/{thread}/writes/{checkpoint}|{task}|{i}  writes made by tasks of a step that is still pending (interrupts)

Subgraphs are not used by Twin, so the checkpoint namespace is always "". Every call is a handful of Firestore
round trips: one batched write per step, and one query plus two batched reads to load the latest state.
"""

import datetime as dt
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


class FirestoreSaver(BaseCheckpointSaver[str]):
    def __init__(self, db, ttl: dt.timedelta = dt.timedelta(days=2), root: str = "sessions") -> None:
        super().__init__()
        self.db, self.ttl, self.root = db, ttl, root

    def _thread(self, thread_id: str):
        return self.db.collection(self.root).document(thread_id)

    def _expire(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC) + self.ttl

    def get_next_version(self, current: str | int | None, channel: None) -> str:
        current_v = 0 if current is None else (current if isinstance(current, int) else int(current.split(".")[0]))
        return f"{current_v + 1:032}.{random.random():016}"

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
                   new_versions: ChannelVersions) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        t, expire, batch = self._thread(thread_id), self._expire(), self.db.batch()
        for channel, version in new_versions.items():
            typ, data = self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")
            batch.set(t.collection("blobs").document(f"{ns}|{channel}|{version}"),
                      {"type": typ, "data": data, "expire_at": expire})
        ctype, cdata = self.serde.dumps_typed(c)
        mtype, mdata = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        batch.set(t.collection("checkpoints").document(checkpoint["id"]), {
            "ctype": ctype, "cdata": cdata, "mtype": mtype, "mdata": mdata,
            "parent": config["configurable"].get("checkpoint_id"), "expire_at": expire})
        batch.set(t, {"latest": checkpoint["id"], "expire_at": expire}, merge=True)
        await batch.commit()
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                          task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        t, expire, batch = self._thread(thread_id), self._expire(), self.db.batch()
        for idx, (channel, value) in enumerate(writes):
            i = WRITES_IDX_MAP.get(channel, idx)
            typ, data = self.serde.dumps_typed(value)
            batch.set(t.collection("writes").document(f"{checkpoint_id}|{task_id}|{i}"), {
                "cp": checkpoint_id, "task_id": task_id, "idx": i, "channel": channel, "type": typ, "data": data,
                "task_path": task_path, "expire_at": expire})
        await batch.commit()

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        t = self._thread(thread_id)
        if wanted := get_checkpoint_id(config):
            snap = await t.collection("checkpoints").document(wanted).get()
            if not snap.exists:
                return None
            checkpoint_id, doc = snap.id, snap.to_dict()
        else:
            head = await t.get()
            if not head.exists:
                return None
            snap = await t.collection("checkpoints").document(head.to_dict()["latest"]).get()
            if not snap.exists:
                return None
            checkpoint_id, doc = snap.id, snap.to_dict()
        checkpoint = self.serde.loads_typed((doc["ctype"], doc["cdata"]))
        refs = [t.collection("blobs").document(f"{ns}|{ch}|{v}") for ch, v in checkpoint["channel_versions"].items()]
        channel_values: dict[str, Any] = {}
        if refs:
            async for blob in self.db.get_all(refs):
                if not blob.exists:
                    continue
                b = blob.to_dict()
                if b["type"] != "empty":
                    channel_values[blob.id.split("|", 2)[1]] = self.serde.loads_typed((b["type"], b["data"]))
        pending = [d.to_dict() async for d in t.collection("writes").where("cp", "==", checkpoint_id).stream()]
        pending.sort(key=lambda w: (w["task_id"], w["idx"]))
        parent = doc.get("parent")
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}},
            checkpoint={**checkpoint, "channel_values": channel_values},
            metadata=self.serde.loads_typed((doc["mtype"], doc["mdata"])),
            pending_writes=[(w["task_id"], w["channel"], self.serde.loads_typed((w["type"], w["data"])))
                            for w in pending],
            parent_config=({"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent}}
                           if parent else None))

    async def alist(self, config: RunnableConfig | None, *, filter: dict[str, Any] | None = None,
                    before: RunnableConfig | None = None, limit: int | None = None) -> AsyncIterator[CheckpointTuple]:
        if not config:
            return
        t = self._thread(config["configurable"]["thread_id"])
        ids = sorted([d.id async for d in t.collection("checkpoints").stream()], reverse=True)[:limit]
        stop = get_checkpoint_id(before) if before else None
        for cid in ids:
            if stop and cid >= stop:
                continue
            got = await self.aget_tuple({"configurable": {**config["configurable"], "checkpoint_id": cid}})
            if got:
                yield got

    async def adelete_thread(self, thread_id: str) -> None:
        t = self._thread(thread_id)
        for sub in ("checkpoints", "blobs", "writes"):
            async for d in t.collection(sub).stream():
                await d.reference.delete()
        await t.delete()
