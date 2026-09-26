"""追加式 JSONL 事件存储。

- 事件按聚合内版本号单调递增，版本由存储自动分配；
- 写入走文件锁（POSIX 下为 ``fcntl.flock``），多进程并发不交错、同键不重复；
- :meth:`EventStore.write_lock` 暴露"持锁区间"，服务可在其中先重载磁盘真相、
  做名额判断再追加事件，保证检查与写入的原子性。
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:  # 优先使用 fcntl 跨进程互斥
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - 非 POSIX 平台
    _HAS_FCNTL = False


class DuplicateEventError(Exception):
    """相同 event_id 已存在。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件已存在: {event_id}")
        self.event_id = event_id


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StoredEvent":
        return cls(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            aggregate_type=raw["aggregate_type"],
            aggregate_id=raw["aggregate_id"],
            occurred_at=raw["occurred_at"],
            version=raw["version"],
            payload=dict(raw["payload"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": dict(self.payload),
        }


def utc_now_iso() -> str:
    """统一以带时区的 UTC 时间戳记事件。"""
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """JSONL 追加日志。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[StoredEvent] = []
        self._ids: set[str] = set()
        self._versions: dict[tuple[str, str], int] = defaultdict(int)
        self._gate = threading.local()
        self._reload()

    # ----- 读取 -----------------------------------------------------------

    def _reload(self) -> None:
        self._events.clear()
        self._ids.clear()
        self._versions = defaultdict(int)
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = StoredEvent.from_dict(json.loads(line))
                self._events.append(event)
                self._ids.add(event.event_id)
                key = (event.aggregate_type, event.aggregate_id)
                if event.version > self._versions[key]:
                    self._versions[key] = event.version

    def load(self) -> list[StoredEvent]:
        with self._lock:
            return list(self._events)

    def stream(
        self, aggregate_type: str | None = None, aggregate_id: str | None = None
    ) -> Iterator[StoredEvent]:
        with self._lock:
            for event in self._events:
                if aggregate_type is not None and event.aggregate_type != aggregate_type:
                    continue
                if aggregate_id is not None and event.aggregate_id != aggregate_id:
                    continue
                yield event

    # ----- 写锁区间 -------------------------------------------------------

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """进入区间即独占文件锁并以磁盘为准重载；区间内的 append 不再重复加锁。"""
        with self._lock:
            handle = self.path.open("a+", encoding="utf-8") if _HAS_FCNTL else None
            if handle is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._gate.held = True
            try:
                self._reload()
                yield
            finally:
                self._gate.held = False
                if handle is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()

    def _in_gate(self) -> bool:
        return bool(getattr(self._gate, "held", False))

    # ----- 写入 -----------------------------------------------------------

    def append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> StoredEvent:
        if self._in_gate():
            return self._append_nolock(
                event_id, event_type, aggregate_type, aggregate_id, payload, occurred_at
            )
        with self.write_lock():
            return self._append_nolock(
                event_id, event_type, aggregate_type, aggregate_id, payload, occurred_at
            )

    def append_many(self, records: Iterable[Mapping[str, Any]]) -> list[StoredEvent]:
        """在同一次文件锁占用内顺序追加多个事件。"""
        if self._in_gate():
            return self._append_many_nolock(records)
        with self.write_lock():
            return self._append_many_nolock(records)

    def _append_nolock(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        occurred_at: str | None,
    ) -> StoredEvent:
        if event_id in self._ids:
            raise DuplicateEventError(event_id)
        version = self._versions[(aggregate_type, aggregate_id)] + 1
        event = StoredEvent(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at or utc_now_iso(),
            version=version,
            payload=dict(payload),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)
        self._ids.add(event_id)
        self._versions[(aggregate_type, aggregate_id)] = version
        return event

    def _append_many_nolock(self, records: Iterable[Mapping[str, Any]]) -> list[StoredEvent]:
        records = list(records)
        for record in records:
            if record["event_id"] in self._ids:
                raise DuplicateEventError(record["event_id"])
        appended: list[StoredEvent] = []
        lines: list[str] = []
        try:
            for record in records:
                key = (record["aggregate_type"], record["aggregate_id"])
                version = self._versions[key] + 1
                event = StoredEvent(
                    event_id=record["event_id"],
                    event_type=record["event_type"],
                    aggregate_type=record["aggregate_type"],
                    aggregate_id=record["aggregate_id"],
                    occurred_at=record.get("occurred_at") or utc_now_iso(),
                    version=version,
                    payload=dict(record["payload"]),
                )
                lines.append(json.dumps(event.to_dict(), ensure_ascii=False))
                appended.append(event)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self._reload()
            raise
        for event in appended:
            self._events.append(event)
            self._ids.add(event.event_id)
            self._versions[(event.aggregate_type, event.aggregate_id)] = event.version
        return appended
