"""tools.py — dag_* 四工具的处理器（命令与工具共用同一引擎逻辑，不分叉）

DagEngine 组装 runstore + dataflow + kanban_client + compiler + advancer，
是插件的唯一门面。设计要点：
- dag_run 幂等：idempotency_key 命中 → 返回既有 run（不重建）
- dag_run 结束前主动 advance（立刻编译首层 + 尽量推进），不等 60s tick
- spec 解析：显式 dag_define 落库 > 插件内置模板 data/templates/<name>.yaml
- 全部入口 fail-open：引擎异常返回 {"ok": False, "error": ...}，不向上抛
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from . import dsl
from .advancer import Advancer
from .compiler import Compiler
from .dataflow import DataflowStore
from .dsl import DagSpec, DagSpecError
from .kanban_client import KanbanClient, KanbanError
from .runstore import RunStore, default_db_path

log = logging.getLogger("hermes-dag.tools")

TEMPLATES_DIR = Path(__file__).parent / "data" / "templates"


class DagEngine:
    def __init__(self, board: str = "i2stream-ops", db_path: str | None = None,
                 client: KanbanClient | None = None, judge_fn=None, llm_fn=None,
                 sender_fn=None, advancer_enabled: bool = True):
        store_path = db_path or default_db_path()
        flow_path = str(Path(store_path).with_name("dataflow.db"))
        self.store = RunStore(store_path)
        self.dataflow = DataflowStore(flow_path)
        self.client = client or KanbanClient(board=board)
        self.board = self.client.board
        self.compiler = Compiler(self.client, self.store, self.board)
        self.advancer = Advancer(self.store, self.dataflow, self.client, self.compiler,
                                 judge_fn=judge_fn, llm_fn=llm_fn, sender_fn=sender_fn)
        if advancer_enabled:
            self.advancer.start()

    def shutdown(self):
        self.advancer.stop()
        self.store.close()
        self.dataflow.close()

    # ------------------------------------------------------------ spec
    def load_spec(self, dag: str) -> DagSpec:
        spec = self.store.get_spec(dag)
        if spec:
            return DagSpec(spec)
        template = TEMPLATES_DIR / f"{dag}.yaml"
        if template.exists():
            return dsl.parse_spec(template.read_text(encoding="utf-8"))
        raise DagSpecError(f"unknown dag {dag!r}: not defined and no bundled template")

    def dag_define(self, spec_yaml: str, validate_only: bool = False) -> dict:
        try:
            spec = dsl.parse_spec(spec_yaml)
        except DagSpecError as exc:
            return {"ok": False, "errors": str(exc).splitlines()}
        if not validate_only:
            self.store.save_spec(spec)
        return {"ok": True, "dag": spec.name, "nodes": len(spec.node_ids()),
                "edges": sum(len(n.parents) for n in spec.nodes),
                "validated_only": bool(validate_only)}

    def dag_explain(self, dag: str) -> dict:
        try:
            spec = self.load_spec(dag)
        except DagSpecError as exc:
            return {"ok": False, "error": str(exc)}
        depths = spec.depth_map()
        nodes = []
        for n in spec.nodes:
            nodes.append({
                "id": n.id, "kind": n.kind, "parents": n.parents,
                "when": n.when, "foreach": n.foreach,
                "skills": n.get("skills") or [], "assignee": n.get("assignee"),
                "depth": depths.get(n.id, 0),
            })
        return {"ok": True, "dag": spec.name, "version": spec.get("version"),
                "params": spec.get("params") or {}, "nodes": nodes}

    # ------------------------------------------------------------ run
    def dag_run(self, dag: str, params: dict | None = None,
                idempotency_key: str | None = None, entry_text: str | None = None) -> dict:
        params = params or {}
        try:
            spec = self.load_spec(dag)
        except DagSpecError as exc:
            return {"ok": False, "error": str(exc)}

        # 参数校验
        missing = []
        for pname, pdef in (spec.get("params") or {}).items():
            if isinstance(pdef, dict) and pdef.get("required") and not params.get(pname):
                missing.append(pname)
        if missing:
            return {"ok": False, "error": f"missing required params: {missing}"}

        if idempotency_key:
            existing = self.store.get_run_by_idempotency(idempotency_key)
            if existing:
                return {"ok": True, "run_id": existing["run_id"], "dag": existing["dag"],
                        "created": False, "status": existing["status"]}

        run_id = f"{spec.name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if not self.store.create_run(run_id, dict(spec), params, entry_text, idempotency_key):
            existing = self.store.get_run_by_idempotency(idempotency_key) if idempotency_key else None
            if existing:
                return {"ok": True, "run_id": existing["run_id"], "dag": existing["dag"],
                        "created": False, "status": existing["status"]}
            return {"ok": False, "error": "run create failed"}

        self.store.add_event(run_id, "run_created", {"dag": spec.name, "params": params})
        try:
            self.advancer.advance_run(run_id)
        except Exception as exc:
            log.exception("initial advance failed: %s", run_id)
            return {"ok": True, "run_id": run_id, "dag": spec.name, "created": True,
                    "warning": f"run created but initial advance errored: {exc}"}
        return {"ok": True, "run_id": run_id, "dag": spec.name, "created": True,
                "status": (self.store.get_run(run_id) or {}).get("status", "running")}

    def dag_status(self, run_id: str | None = None) -> dict:
        if not run_id:
            return {"ok": True, "runs": self.store.all_runs()}
        run = self.store.get_run(run_id)
        if not run:
            return {"ok": False, "error": f"unknown run {run_id!r}"}
        spec = DagSpec(json.loads(run["spec_json"]))
        node_status = self.store.node_status_map(run_id)
        try:
            card_view = self.compiler.card_status_view(run_id)
        except KanbanError as exc:
            card_view = {}
            kanban_error = str(exc)
        else:
            kanban_error = None

        view = self.dataflow.run_view(run_id)
        nodes_out = []
        for n in spec.nodes:
            cards = [{"card_id": r["card_id"], "shard": r["shard"],
                      "ledger": r["status"],
                      "kanban": card_view.get(r["card_id"])}
                     for r in self.store.cards_for_node(run_id, n.id)]
            entry = view.get(n.id) or {}
            nodes_out.append({
                "id": n.id, "kind": n.kind, "when": n.when, "foreach": n.foreach,
                "ledger": node_status.get(n.id, "pending"),
                "cards": cards,
                "summary": entry.get("summary"),
                "outputs": entry.get("outputs"),
            })
        return {
            "ok": True,
            "run_id": run_id,
            "dag": run["dag"],
            "status": run["status"],
            "created_at": run["created_at"],
            "graph": self._ascii_graph(spec, node_status, card_view),
            "nodes": nodes_out,
            "events": self.store.events_for_run(run_id, limit=30),
            "kanban_error": kanban_error,
        }

    # ------------------------------------------------------------ helpers
    def _ascii_graph(self, spec: DagSpec, node_status: dict, card_view: dict) -> str:
        depths = spec.depth_map()
        order = sorted(spec.node_ids(), key=lambda nid: (depths.get(nid, 0), nid))
        lines = []
        by_depth: dict = {}
        for nid in order:
            by_depth.setdefault(depths.get(nid, 0), []).append(nid)
        for depth in sorted(by_depth):
            for nid in by_depth[depth]:
                st = node_status.get(nid, "pending")
                marker = {"done": "✔", "pruned": "✂", "creating": "…",
                          "failed": "✗", "pruned-pending": "⏳"}.get(st, "·")
                lines.append(f"{'  ' * depth}{marker} {nid} [{st}]")
        return "\n".join(lines)


def make_tool_handlers(engine: DagEngine) -> dict:
    """四工具 handler（闭包注入 engine；签名为 Hermes 工具 handler 约定）。

    Hermes tools/registry._normalize_handler_result 只接受 str 或多模态 envelope，
    handler 直接返回 dict 会被判 tool_result_contract 错误，故统一 json.dumps。
    """

    def dag_define(args, **kwargs):
        return json.dumps(engine.dag_define(args.get("spec_yaml") or "",
                                            validate_only=bool(args.get("validate_only"))),
                          ensure_ascii=False)

    def dag_explain(args, **kwargs):
        return json.dumps(engine.dag_explain(args.get("dag") or ""), ensure_ascii=False)

    def dag_run(args, **kwargs):
        return json.dumps(
            engine.dag_run(args.get("dag") or "", params=args.get("params") or {},
                           idempotency_key=args.get("idempotency_key"),
                           entry_text=kwargs.get("entry_text")),
            ensure_ascii=False)

    def dag_status(args, **kwargs):
        return json.dumps(engine.dag_status(args.get("run_id")), ensure_ascii=False)

    return {"dag_define": dag_define, "dag_explain": dag_explain,
            "dag_run": dag_run, "dag_status": dag_status}
