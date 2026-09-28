"""kanban_client.py — `hermes kanban` CLI 子进程封装（引擎对 Kanban 的唯一写入通道）

选型依据（M0/源码核实）：
- ctx.dispatch_tool("kanban_create") 受 check_fn 门控（仅 HERMES_KANBAN_TASK worker
  或 kanban-toolset profile 可见），gateway 进程里不可靠 → 不用
- 直接 import hermes_cli.kanban_db 是内部模块（灰色地带）→ 留作日后优化项
- CLI 是公开稳定面：create/show/complete/archive/comment/list 全部 --json 可解析

所有方法线程安全（subprocess 无共享态）；测试可注入 db_path（HERMES_KANBAN_DB）
与 board，把整台引擎钉进沙箱库。
"""

from __future__ import annotations

import json
import os
import subprocess


class KanbanError(RuntimeError):
    """CLI 调用失败（非零退出/输出不可解析）。"""


class KanbanClient:
    def __init__(self, board: str = "i2stream-ops", db_path: str | None = None, timeout: float = 60.0):
        self.board = board
        self.db_path = db_path  # 测试沙箱：注入后经 HERMES_KANBAN_DB 钉定
        self.timeout = timeout

    # ------------------------------------------------------------- internals
    def _argv(self, *args: str, json_out: bool = True) -> list:
        argv = ["hermes", "kanban"]
        if self.board:
            argv += ["--board", self.board]
        argv += list(args)
        if json_out:
            argv.append("--json")
        return argv

    def _run(self, argv: list, check: bool = True) -> str:
        env = dict(os.environ)
        if self.db_path:
            env["HERMES_KANBAN_DB"] = self.db_path
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise KanbanError(f"kanban CLI timeout: {' '.join(argv)}") from exc
        if check and proc.returncode != 0:
            raise KanbanError(
                f"kanban CLI failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr.strip()[:800]}"
            )
        return proc.stdout

    def _run_json(self, argv: list):
        out = self._run(argv)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise KanbanError(f"kanban CLI bad JSON: {' '.join(argv)}\n{out[:400]}") from exc

    # ------------------------------------------------------------- mutations
    def create(
        self,
        title: str,
        body: str | None = None,
        parents: list | None = None,
        skills: list | None = None,
        idempotency_key: str | None = None,
        assignee: str | None = None,
        priority: int = 0,
    ) -> dict:
        """建卡。幂等键已存在时 Kanban 返回既有卡（M0 实验 4）。返回至少含 id/status。"""
        argv = self._argv("create", title)
        if body:
            argv += ["--body", body]
        for p in parents or []:
            argv += ["--parent", p]
        for s in skills or []:
            argv += ["--skill", s]
        if idempotency_key:
            argv += ["--idempotency-key", idempotency_key]
        if assignee:
            argv += ["--assignee", assignee]
        if priority:
            argv += ["--priority", str(priority)]
        data = self._run_json(argv)
        return data if isinstance(data, dict) else {"id": str(data)}

    def complete(self, task_id: str, summary: str | None = None, metadata: dict | None = None) -> bool:
        argv = self._argv("complete", task_id, json_out=False)
        if summary is not None:
            argv += ["--summary", summary]
        if metadata is not None:
            argv += ["--metadata", json.dumps(metadata, ensure_ascii=False)]
        self._run(argv)
        return True

    def archive(self, task_id: str) -> bool:
        self._run(self._argv("archive", task_id, json_out=False))
        return True

    def comment(self, task_id: str, text: str) -> bool:
        self._run(self._argv("comment", task_id, text, json_out=False))
        return True

    # ------------------------------------------------------------- queries
    def show(self, task_id: str) -> dict | None:
        """标准化后的单卡视图：{**task, latest_summary, parents, children, runs, comments}。"""
        data = self._run_json(self._argv("show", task_id))
        if not isinstance(data, dict) or "task" not in data:
            return None
        merged = dict(data["task"])
        for key in ("latest_summary", "parents", "children", "comments", "events", "runs"):
            if key in data:
                merged[key] = data[key]
        return merged

    def list_tasks(self) -> list:
        """全板任务扁平列表（含 status，不含 parents）。"""
        data = self._run_json(self._argv("list"))
        return data if isinstance(data, list) else []

    def status_map(self) -> dict:
        return {t["id"]: t.get("status") for t in self.list_tasks() if t.get("id")}

    def latest_run(self, task_id: str) -> dict | None:
        shown = self.show(task_id)
        if not shown:
            return None
        runs = shown.get("runs") or []
        return runs[-1] if runs else None
