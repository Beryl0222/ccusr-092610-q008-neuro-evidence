"""SQLite 事件存储、业务幂等表、影响评估任务表与名额占用表。

存储只负责持久化与并发原语，不做领域判定：
- 事件只追加，不提供更新/删除；
- 业务幂等键带版本指纹，相同键不同指纹由上层判定为冲突；
- 名额占用以 (地区, 项目, 患者) 唯一约束 + 计数闸门保证并发不超额；
- 影响评估任务独立成表，进程崩溃后仍可捞出未完成任务续跑。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping


class EventIdCollision(RuntimeError):
    """相同 event_id 已存在（事件级去重，与业务键幂等相互独立）。"""


class EventStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.row_factory = sqlite3.Row
        # 关闭隐式事务，统一由 transaction() 显式控制。
        conn.isolation_level = None

    @classmethod
    def connect(cls, database: str | Path = ":memory:", *, timeout: float = 30.0) -> "EventStore":
        conn = sqlite3.connect(str(database), check_same_thread=False, timeout=timeout)
        store = cls(conn)
        store.init_schema()
        if database != ":memory:":
            # 文件库开启 WAL，让并发 BEGIN IMMEDIATE 在忙等待下串行化而非立刻报锁。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
        return store

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id       TEXT NOT NULL UNIQUE,
                event_type     TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id   TEXT NOT NULL,
                occurred_at    TEXT NOT NULL,
                version        INTEGER NOT NULL,
                payload        TEXT NOT NULL,
                actor          TEXT
            );

            CREATE TABLE IF NOT EXISTS idempotency (
                business_key TEXT PRIMARY KEY,
                outcome_kind TEXT NOT NULL,
                claim_id     TEXT,
                fingerprint  TEXT NOT NULL,
                result_json  TEXT NOT NULL,
                created_seq  INTEGER NOT NULL,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quota_bookings (
                region             TEXT NOT NULL,
                service_item_code  TEXT NOT NULL,
                patient_ref        TEXT NOT NULL,
                granted_event_id   TEXT NOT NULL,
                PRIMARY KEY (region, service_item_code, patient_ref)
            );

            CREATE TABLE IF NOT EXISTS impact_tasks (
                task_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_event_id TEXT NOT NULL,
                trigger_kind     TEXT NOT NULL,
                product_id       TEXT NOT NULL,
                status           TEXT NOT NULL CHECK (status IN ('open', 'done')),
                opened_at        TEXT NOT NULL,
                finished_at      TEXT,
                report_json      TEXT
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- 事件 ----------------------------------------------------------

    def append(self, event: Mapping[str, Any], *, actor: str | None = None) -> int:
        """追加事件，返回自增序号；event_id 重复时抛出 EventIdCollision。"""
        try:
            cur = self.conn.execute(
                """
                INSERT INTO events
                    (event_id, event_type, aggregate_type, aggregate_id,
                     occurred_at, version, payload, actor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["event_type"],
                    event["aggregate_type"],
                    event["aggregate_id"],
                    event["occurred_at"],
                    int(event["version"]),
                    json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                    actor,
                ),
            )
        except sqlite3.IntegrityError as exc:  # event_id 唯一冲突
            raise EventIdCollision(event["event_id"]) from exc
        return int(cur.lastrowid)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def list_events(
        self, *, aggregate_type: str | None = None, aggregate_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if aggregate_type is not None:
            sql += " AND aggregate_type = ?"
            params.append(aggregate_type)
        if aggregate_id is not None:
            sql += " AND aggregate_id = ?"
            params.append(aggregate_id)
        sql += " ORDER BY seq"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def count_events(self, *, aggregate_type: str, aggregate_id: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM events
            WHERE aggregate_type = ? AND aggregate_id = ?
            """,
            (aggregate_type, aggregate_id),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        event = {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "occurred_at": row["occurred_at"],
            "version": row["version"],
            "payload": json.loads(row["payload"]),
        }
        if row["actor"] is not None:
            event["actor"] = row["actor"]
        return event

    # -- 业务幂等 ------------------------------------------------------

    def save_idempotent(
        self,
        *,
        business_key: str,
        outcome_kind: str,
        fingerprint: str,
        result: Mapping[str, Any],
        created_seq: int,
        claim_id: str | None = None,
        created_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO idempotency
                (business_key, outcome_kind, claim_id, fingerprint,
                 result_json, created_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_key,
                outcome_kind,
                claim_id,
                fingerprint,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                created_seq,
                created_at,
            ),
        )

    def get_idempotent(self, business_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM idempotency WHERE business_key = ?", (business_key,)
        ).fetchone()
        if row is None:
            return None
        return {
            "business_key": row["business_key"],
            "outcome_kind": row["outcome_kind"],
            "claim_id": row["claim_id"],
            "fingerprint": row["fingerprint"],
            "result": json.loads(row["result_json"]),
            "created_seq": row["created_seq"],
            "created_at": row["created_at"],
        }

    # -- 试点名额 ------------------------------------------------------

    def book_quota(
        self, *, region: str, service_item_code: str, patient_ref: str, granted_event_id: str
    ) -> bool:
        """占位成功返回 True；同一 (地区,项目,患者) 重复占位返回 False。"""
        try:
            self.conn.execute(
                """
                INSERT INTO quota_bookings
                    (region, service_item_code, patient_ref, granted_event_id)
                VALUES (?, ?, ?, ?)
                """,
                (region, service_item_code, patient_ref, granted_event_id),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def quota_used(self, *, region: str, service_item_code: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM quota_bookings
            WHERE region = ? AND service_item_code = ?
            """,
            (region, service_item_code),
        ).fetchone()
        return int(row["n"])

    # -- 影响评估任务 --------------------------------------------------

    def create_impact_task(
        self, *, trigger_event_id: str, trigger_kind: str, product_id: str, opened_at: str
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO impact_tasks
                (trigger_event_id, trigger_kind, product_id, status, opened_at)
            VALUES (?, ?, ?, 'open', ?)
            """,
            (trigger_event_id, trigger_kind, product_id, opened_at),
        )
        return int(cur.lastrowid)

    def list_open_impact_tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM impact_tasks WHERE status = 'open' ORDER BY task_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_impact_tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM impact_tasks ORDER BY task_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def finish_impact_task(
        self, *, task_id: int, finished_at: str, report: Mapping[str, Any]
    ) -> None:
        self.conn.execute(
            """
            UPDATE impact_tasks
               SET status = 'done', finished_at = ?, report_json = ?
             WHERE task_id = ?
            """,
            (finished_at, json.dumps(report, ensure_ascii=False, sort_keys=True), task_id),
        )


def utcnow_iso() -> str:
    """统一时间戳格式（带时区），供服务与任务表使用。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
