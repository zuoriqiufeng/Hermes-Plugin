"""runstore.py — run 状态、幂等键、node→card 记账、跨进程推进队列、事件日志

存储：单 SQLite（WAL）。默认路径 <hermes_home>/plugin-data/hermes-dag/runs.db
（官方 plugin-data 目录惯例；plugin_storage 在部分解释器不可导入时手动拼路径）。

角色边界（铁律）：kanban.db 是任务状态唯一权威；本库只记"账"——
run/spec/事件/记账/队列。崩溃恢复不依赖内存态：advancer 用 kanban 实况 + 本库记账对账。

queue 表是跨进程推进通道：hook 回调（可能在 worker/CLI 子进程）只写队列秒回；
gateway 内 advancer daemon 线程原子消费（UPDATE ... WHERE consumed=0 保证单消费者语义）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    run_id          TEXT PRIMARY KEY,
    dag             TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    entry_text      TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    idempotency_key TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem ON runs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS run_nodes(
    run_id   TEXT NOT NULL,
    node_id  TEXT NOT NULL,
    shard    TEXT NOT NULL DEFAULT '',
    card_id  TEXT,
    status   TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY(run_id, node_id, shard)
);
CREATE TABLE IF NOT EXISTS events(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS queue(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    task_id      TEXT,
    board        TEXT,
    payload_json TEXT,
    enqueued_at  REAL NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS specs(
    dag        TEXT PRIMARY KEY,
    spec_json  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""

_TERMINAL_RUN = ("completed", "failed", "aborted")


def default_db_path() -> str:
    """plugin-data 目录优先；导入不可用则按官方路径约定手拼。"""
    override = os.environ.get("HERMES_DAG_DB")
    if override:
        return override
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return os.path.join(str(plugin_data_dir("hermes-dag")), "runs.db")
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return os.path.join(home, "plugin-data", "hermes-dag", "runs.db")


class RunStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- runs
    def create_run(self, run_id: str, spec: dict, params: dict, entry_text: str | None,
                   idempotency_key: str | None) -> bool:
        """返回 False = 幂等键已存在（不新建）。"""
        now = time.time()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO runs(run_id, dag, spec_json, params_json, entry_text,"
                    " status, idempotency_key, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, spec.get("dag"), json.dumps(spec, ensure_ascii=False),
                     json.dumps(params, ensure_ascii=False), entry_text,
                     "running", idempotency_key, now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_run_by_idempotency(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def run_spec(self, run_id: str) -> dict | None:
        run = self.get_run(run_id)
        return json.loads(run["spec_json"]) if run else None

    def active_runs(self) -> list:
        rows = self._conn.execute(
            "SELECT run_id FROM runs WHERE status NOT IN (?,?,?)",
            _TERMINAL_RUN).fetchall()
        return [r["run_id"] for r in rows]

    def all_runs(self, limit: int = 50) -> list:
        rows = self._conn.execute(
            "SELECT run_id, dag, status, created_at, updated_at FROM runs"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_run_status(self, run_id: str, status: str):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (status, time.time(), run_id))

    # ---------------------------------------------------------- node ledger
    def record_node_card(self, run_id: str, node_id: str, card_id: str,
                         shard: str = "", status: str = "pending"):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_nodes(run_id, node_id, shard, card_id, status)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(run_id, node_id, shard) DO UPDATE SET card_id=excluded.card_id,"
                " status=excluded.status",
                (run_id, node_id, shard, card_id, status))

    def set_node_status(self, run_id: str, node_id: str, status: str, shard: str | None = None):
        with self._lock, self._conn:
            if shard is None:
                self._conn.execute(
                    "UPDATE run_nodes SET status=? WHERE run_id=? AND node_id=?",
                    (status, run_id, node_id))
            else:
                self._conn.execute(
                    "UPDATE run_nodes SET status=? WHERE run_id=? AND node_id=? AND shard=?",
                    (status, run_id, node_id, shard))

    def node_rows(self, run_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM run_nodes WHERE run_id=?", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def node_status_map(self, run_id: str) -> dict:
        """node_id -> 聚合状态：任一 pruned → pruned；全部终态 → 该终态；否则最“活动”态。"""
        agg: dict = {}
        for r in self.node_rows(run_id):
            nid, st = r["node_id"], r["status"]
            cur = agg.get(nid)
            agg[nid] = _merge_status(cur, st)
        return agg

    def cards_for_node(self, run_id: str, node_id: str) -> list:
        rows = self._conn.execute(
            "SELECT shard, card_id, status FROM run_nodes WHERE run_id=? AND node_id=?"
            " AND card_id IS NOT NULL AND card_id != ''",
            (run_id, node_id)).fetchall()
        return [dict(r) for r in rows]

    def node_for_task(self, card_id: str):
        """card_id 反查 (run_id, node_id, shard)；非 DAG 卡返回 None。"""
        row = self._conn.execute(
            "SELECT run_id, node_id, shard, status FROM run_nodes WHERE card_id=?",
            (card_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- queue
    def enqueue(self, kind: str, task_id: str | None, board: str | None = None,
                payload: dict | None = None):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO queue(kind, task_id, board, payload_json, enqueued_at)"
                " VALUES(?,?,?,?,?)",
                (kind, task_id, board,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 time.time()))

    def dequeue(self, batch: int = 20) -> list:
        """原子取走一批未消费事件（单消费者语义；多进程并发也只会有一个赢）。"""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, kind, task_id, board, payload_json, enqueued_at FROM queue"
                " WHERE consumed=0 ORDER BY id LIMIT ?", (batch,)).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            marks = []
            for qid in ids:
                cur = self._conn.execute(
                    "UPDATE queue SET consumed=1 WHERE id=? AND consumed=0", (qid,))
                if cur.rowcount:
                    marks.append(qid)
            out = [dict(r) for r in rows if r["id"] in set(marks)]
            self._conn.execute(
                "DELETE FROM queue WHERE consumed=1 AND id IN (%s)"
                % ",".join("?" * len(ids)), ids)
            return out

    def pending_queue_size(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE consumed=0").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------- events
    def add_event(self, run_id: str | None, kind: str, payload: dict | None = None):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(run_id, ts, kind, payload_json) VALUES(?,?,?,?)",
                (run_id, time.time(), kind,
                 json.dumps(payload, ensure_ascii=False) if payload else None))

    def events_for_run(self, run_id: str, limit: int = 200) -> list:
        rows = self._conn.execute(
            "SELECT ts, kind, payload_json FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit)).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"],
                 "payload": json.loads(r["payload_json"]) if r["payload_json"] else None}
                for r in rows]

    # ------------------------------------------------------------- specs
    def save_spec(self, spec: dict):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO specs(dag, spec_json, version, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(dag) DO UPDATE SET spec_json=excluded.spec_json,"
                " version=excluded.version, updated_at=excluded.updated_at",
                (spec.get("dag"), json.dumps(spec, ensure_ascii=False),
                 int(spec.get("version") or 1), time.time()))

    def get_spec(self, dag: str) -> dict | None:
        row = self._conn.execute("SELECT spec_json FROM specs WHERE dag=?", (dag,)).fetchone()
        return json.loads(row["spec_json"]) if row else None

    def list_specs(self) -> list:
        rows = self._conn.execute(
            "SELECT dag, version, updated_at FROM specs ORDER BY dag").fetchall()
        return [dict(r) for r in rows]


_ORDER = {"running": 0, "creating": 1, "pending": 1, "pruned": 3, "done": 3, "failed": 2, "blocked": 2}


def _merge_status(a: str | None, b: str) -> str:
    if a is None:
        return b
    if a == b:
        return a
    rank = max(_ORDER.get(a, 1), _ORDER.get(b, 1))
    if "pruned" in (a, b):
        return "pruned"
    for cand in ("running", "creating", "blocked", "failed", "done"):
        if _ORDER.get(cand) == rank and cand in (a, b):
            return cand
    return a
