from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    """Thread-safe fan-out of events (wake word, transcripts, ...) to the UI.

    Events are dicts: {"id", "type", "time", **data}. Persistent events are kept in a
    short history so a page opened later (or reconnecting) can catch up. Transient
    events (e.g. live scores) are only delivered to currently connected subscribers.
    """

    def __init__(self, history_size: int = 100) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._next_id = 1

    def publish(self, type_: str, persist: bool = True, **data: Any) -> dict[str, Any]:
        with self._lock:
            event = {"id": self._next_id, "type": type_, "time": time.time(), **data}
            self._next_id += 1
            if persist:
                self._history.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass  # a stalled browser must never block the audio loop
        return event

    def subscribe(self, after_id: int = 0) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Return a live queue plus the persisted events newer than `after_id`.

        Both are taken under one lock so no event is missed or duplicated in between.
        """
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            backlog = [event for event in self._history if event["id"] > after_id]
            self._subscribers.add(subscriber)
        return subscriber, backlog

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
