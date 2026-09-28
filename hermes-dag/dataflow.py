"""dataflow.py — run/node outputs（Kanban XCom 等价物的汇聚层）

职责（v0.21.1 下已收窄——Kanban 原生把父卡 summary+metadata 回放进子卡 prompt）：
  1. verdict judge 的 run 级聚合输入
  2. body 模板显式渲染 {{ nodes.<id>.outputs.<key> }} 的取值源
  3. dag_status 的数据流摘要

只存 outputs 与 summary，不存任务状态（状态权威在 kanban.db）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time


class DataflowStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS node_outputs(
                   run_id TEXT NOT NULL, node_id TEXT NOT NULL,
                   shard TEXT NOT NULL DEFAULT '',
                   outputs_json TEXT, summary TEXT, card_id TEXT,
                   created_at REAL NOT NULL,
                   PRIMARY KEY(run_id, node_id, shard))""")
        self._conn.commit()

    def close(self):
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    def put_outputs(self, run_id: str, node_id: str, outputs: dict,
                    summary: str | None = None, card_id: str | None = None,
                    shard: str = ""):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO node_outputs(run_id, node_id, shard, outputs_json, summary,"
                " card_id, created_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id, node_id, shard) DO UPDATE SET"
                " outputs_json=excluded.outputs_json, summary=excluded.summary,"
                " card_id=excluded.card_id",
                (run_id, node_id, shard, json.dumps(outputs, ensure_ascii=False),
                 summary, card_id, time.time()))

    def _rows(self, run_id: str, node_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM node_outputs WHERE run_id=? AND node_id=? ORDER BY shard",
            (run_id, node_id)).fetchall()
        return [dict(r) for r in rows]

    def node_outputs(self, run_id: str, node_id: str):
        """单节点合并视图：无行 → None；单 shard → 该 outputs；多 shard → {"items":[...]}。"""
        rows = self._rows(run_id, node_id)
        if not rows:
            return None
        items = []
        for r in rows:
            try:
                items.append(json.loads(r["outputs_json"]) if r["outputs_json"] else {})
            except json.JSONDecodeError:
                items.append({})
        if len(items) == 1:
            return items[0]
        return {"items": items}

    def run_view(self, run_id: str) -> dict:
        """{node_id: {"outputs": ..., "summary": str}}，供渲染与 judge。"""
        rows = self._conn.execute(
            "SELECT node_id, shard, outputs_json, summary, card_id FROM node_outputs"
            " WHERE run_id=? ORDER BY node_id, shard", (run_id,)).fetchall()
        by_node: dict = {}
        for r in rows:
            try:
                outputs = json.loads(r["outputs_json"]) if r["outputs_json"] else {}
            except json.JSONDecodeError:
                outputs = {}
            by_node.setdefault(r["node_id"], []).append(
                {"shard": r["shard"], "outputs": outputs,
                 "summary": r["summary"], "card_id": r["card_id"]})
        view = {}
        for nid, lst in by_node.items():
            if len(lst) == 1:
                view[nid] = {"outputs": lst[0]["outputs"],
                             "summary": lst[0]["summary"],
                             "card_id": lst[0]["card_id"]}
            else:
                view[nid] = {"outputs": {"items": [e["outputs"] for e in lst]},
                             "summary": " | ".join(str(e["summary"] or "") for e in lst),
                             "card_id": ",".join(str(e["card_id"] or "") for e in lst)}
        return view
