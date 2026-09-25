from __future__ import annotations

import json
import queue
import threading
import time

DEFAULT_REPLAY_SIZE = 512
DEFAULT_CLIENT_QUEUE = 512
DEFAULT_HEARTBEAT_SECONDS = 15.0
STOP_POLL_SECONDS = 0.25
MINIMUM_WAIT_SECONDS = 0.05
COALESCED_KINDS = {"progress", "bandwidth_updated"}


class EventClient:
    def __init__(self, broker: "EventBroker", queue_size: int = DEFAULT_CLIENT_QUEUE):
        self.broker = broker
        self.queue: queue.Queue = queue.Queue(maxsize=max(8, int(queue_size)))
        self.dropped = 0
        self.connected_at = time.time()

    def offer(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                return

    def take(self, timeout: float) -> dict | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.broker.unsubscribe(self)


class EventBroker:
    def __init__(self, state, replay_size: int = DEFAULT_REPLAY_SIZE, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS):
        self.state = state
        self.replay_size = max(16, int(replay_size))
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._lock = threading.RLock()
        self._clients: list[EventClient] = []
        self._replay: list[dict] = []
        self._sequence = 0
        self._unsubscribe = state.subscribe(self._on_state_event)

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _on_state_event(self, event: dict) -> None:
        kind = str(event.get("kind") or "")
        payload = dict(event.get("payload") or {})
        self.publish(kind, payload)

    def publish(self, kind: str, payload: dict | None = None) -> dict:
        with self._lock:
            record = {
                "id": self._next_sequence(),
                "kind": str(kind),
                "payload": dict(payload or {}),
                "at": time.time(),
            }
            self._replay.append(record)
            if len(self._replay) > self.replay_size:
                del self._replay[: len(self._replay) - self.replay_size]
            clients = list(self._clients)
        for client in clients:
            client.offer(record)
        return record

    def subscribe(self, last_event_id: int | None = None, queue_size: int = DEFAULT_CLIENT_QUEUE) -> tuple[EventClient, list[dict], bool]:
        client = EventClient(self, queue_size)
        with self._lock:
            self._clients.append(client)
            backlog: list[dict] = []
            gap = False
            if last_event_id is not None:
                oldest = self._replay[0]["id"] if self._replay else self._sequence + 1
                if int(last_event_id) < oldest - 1:
                    gap = True
                else:
                    backlog = [item for item in self._replay if item["id"] > int(last_event_id)]
            return client, backlog, gap

    def unsubscribe(self, client: EventClient) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def last_event_id(self) -> int:
        with self._lock:
            return self._sequence

    def shutdown(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.offer({"id": self.last_event_id(), "kind": "shutdown", "payload": {}, "at": time.time()})
        try:
            self._unsubscribe()
        except Exception:
            return


def format_sse(record: dict) -> str:
    payload = json.dumps(
        {"kind": record.get("kind"), "payload": record.get("payload") or {}, "at": record.get("at")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {record.get('id')}\nevent: {record.get('kind')}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    return f": {text}\n\n"


def coalesce(records: list[dict]) -> list[dict]:
    result: list[dict] = []
    index: dict[tuple[str, str], int] = {}
    for record in records:
        kind = str(record.get("kind") or "")
        if kind not in COALESCED_KINDS:
            result.append(record)
            continue
        key = (kind, str((record.get("payload") or {}).get("job_id") or ""))
        position = index.get(key)
        if position is None:
            index[key] = len(result)
            result.append(record)
        else:
            result[position] = record
    return result


def event_stream(broker: EventBroker, last_event_id: int | None = None, stop=None, idle_timeout: float | None = None, idle_ticks: bool = False):
    client, backlog, gap = broker.subscribe(last_event_id)
    started = time.monotonic()
    try:
        if gap:
            yield format_sse({"id": broker.last_event_id(), "kind": "resync_required", "payload": {"reason": "event backlog expired"}, "at": time.time()})
        for record in coalesce(backlog):
            yield format_sse(record)
        yield format_comment("connected")
        last_heartbeat = time.monotonic()
        bounded = stop is not None or idle_timeout is not None
        while True:
            if stop is not None and stop():
                return
            now = time.monotonic()
            if idle_timeout is not None and (now - started) > float(idle_timeout):
                return
            wait = broker.heartbeat_seconds - (now - last_heartbeat)
            wait = max(0.0, wait)
            if bounded:
                wait = min(wait, STOP_POLL_SECONDS)
            if idle_timeout is not None:
                wait = min(wait, max(0.0, float(idle_timeout) - (now - started)))
            record = client.take(max(MINIMUM_WAIT_SECONDS, wait))
            if record is None:
                if (time.monotonic() - last_heartbeat) >= broker.heartbeat_seconds:
                    last_heartbeat = time.monotonic()
                    yield format_comment("heartbeat")
                elif idle_ticks:
                    yield ""
                continue
            if str(record.get("kind")) == "shutdown":
                return
            yield format_sse(record)
    finally:
        client.close()
