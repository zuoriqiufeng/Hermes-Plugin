"""advancer.py — 推进器：条件边求值 → 动态建卡 / 剪枝 / verdict / foreach

推进时机（三层，全部收敛到 advance_run 幂等入口）：
  1. hook 入队（kanban_task_completed/blocked，可能在 worker/CLI 子进程）→ daemon 线程消费
  2. on_kanban_dispatch_tick（gateway 60s 一拍）→ 节流对账
  3. dag_run/dag_status 重入 → 主动对账（gateway 重启恢复）

核心算法 advance_run（幂等，可任意重入）：
  0. sync：以 kanban 实况校正记账（done 卡 → 节点 done + dataflow 补行）——
     覆盖 hook 丢失、进程崩溃、跨进程双消费者三种不一致
  1. 循环扫描节点直到无进展：节点未终态且父全终态（done/pruned）→
     when=false → DFS 剪枝子树；verdict → judge → create+立即 complete；
     foreach → 空集占位 done 卡 / 逐 shard 建 exec 卡；普通 exec → 建卡
  2. 全节点终态 → run completed

剪枝的扇入修正不需要"移除父边"：archived 占位卡原生满足 Kanban 扇入（M0 实验 2）。
幂等：卡级幂等键 dag:{run}:{node}[:shard]（Kanban 同键返回既有卡）+ run_nodes 记账；
跨进程并发由幂等键 + 记账 ON CONFLICT 兜底，进程内由 per-run 锁串行化。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import evaluator
from . import verdict as verdict_mod
from .compiler import Compiler, shard_slug
from .dsl import DagSpec
from .kanban_client import KanbanClient
from .runstore import RunStore

log = logging.getLogger("hermes-dag.advancer")

NODE_PENDING = "pending"
NODE_CREATING = "creating"
NODE_DONE = "done"
NODE_PRUNED = "pruned"
NODE_PRUNED_PENDING = "pruned-pending"

_TERMINAL_NODE = (NODE_DONE, NODE_PRUNED)


class Advancer:
    def __init__(self, store: RunStore, dataflow, client: KanbanClient,
                 compiler: Compiler, judge_fn=None, llm_fn=None, sender_fn=None,
                 poll_interval: float = 2.0, reconcile_interval: float = 60.0):
        self.store = store
        self.dataflow = dataflow
        self.client = client
        self.compiler = compiler
        self.judge_fn = judge_fn
        self.llm_fn = llm_fn
        self.sender_fn = sender_fn
        self.poll_interval = poll_interval
        self.reconcile_interval = reconcile_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_locks: dict = {}
        self._locks_guard = threading.Lock()
        self._last_reconcile = 0.0

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="hermes-dag-advancer",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                events = self.store.dequeue(batch=20)
                for evt in events:
                    try:
                        self.handle_event(evt)
                    except Exception:
                        log.exception("event handling failed: %s", evt)
                now = time.time()
                if now - self._last_reconcile >= self.reconcile_interval:
                    self._last_reconcile = now
                    self.reconcile()
            except Exception:
                log.exception("advancer loop error")
            self._stop.wait(self.poll_interval)

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            if run_id not in self._run_locks:
                self._run_locks[run_id] = threading.Lock()
            return self._run_locks[run_id]

    # ------------------------------------------------------------ entry points
    def handle_event(self, evt: dict):
        kind = evt.get("kind")
        task_id = evt.get("task_id")
        if not task_id:
            return
        ref = self.store.node_for_task(task_id)
        if not ref:
            return  # 非 DAG 卡（手工卡/其他来源）——忽略
        run_id = ref["run_id"]
        if kind == "kanban_task_completed":
            self.on_task_completed(run_id, task_id)
        elif kind == "kanban_task_blocked":
            self.store.add_event(run_id, "task_blocked",
                                 {"node": ref["node_id"], "card_id": task_id})

    def on_task_completed(self, run_id: str, task_id: str):
        ref = self.store.node_for_task(task_id)
        if not ref:
            return
        node_id, shard = ref["node_id"], ref.get("shard") or ""
        latest = self.client.latest_run(task_id) or {}
        meta = latest.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("outputs"), dict):
            outputs = meta["outputs"]
        elif isinstance(meta, dict):
            outputs = meta
        else:
            outputs = {}
        self.dataflow.put_outputs(run_id, node_id, outputs,
                                  summary=latest.get("summary"), card_id=task_id,
                                  shard=shard)
        self.store.set_node_status(run_id, node_id, NODE_DONE, shard=shard or None)
        self.store.add_event(run_id, "node_done",
                             {"node": node_id, "shard": shard, "card_id": task_id,
                              "verdict": outputs.get("verdict")})
        self.advance_run(run_id)

    def reconcile(self):
        for run_id in self.store.active_runs():
            try:
                self.advance_run(run_id)
            except Exception:
                log.exception("reconcile advance failed: %s", run_id)

    def run_once(self) -> int:
        """测试/手动驱动：消费一批队列事件 + 一次 reconcile；返回处理事件数。"""
        events = self.store.dequeue(batch=50)
        for evt in events:
            try:
                self.handle_event(evt)
            except Exception:
                log.exception("event handling failed: %s", evt)
        self.reconcile()
        return len(events)

    # ------------------------------------------------------------ core
    def advance_run(self, run_id: str):
        with self._run_lock(run_id):
            self._advance_locked(run_id)

    def _advance_locked(self, run_id: str):
        run = self.store.get_run(run_id)
        if not run or run["status"] in ("completed", "failed", "aborted"):
            return
        spec = DagSpec(json.loads(run["spec_json"]))
        params = json.loads(run["params_json"]) if run["params_json"] else {}
        node_status = dict(self.store.node_status_map(run_id))
        self._sync_from_kanban(run_id, node_status)
        upstream_view = self._upstream_view(run_id)
        progressed = True

        while progressed:
            progressed = False
            for node in spec.nodes:
                nid = node.id
                st = node_status.get(nid)
                if st in _TERMINAL_NODE or st == NODE_CREATING:
                    continue
                if st == NODE_PRUNED_PENDING:
                    # 延迟补占位卡：父卡此刻可能已全部就绪
                    if self._try_pruned_placeholder(run_id, spec, node, params):
                        node_status[nid] = NODE_PRUNED
                        progressed = True
                    continue
                parents = node.parents
                if not all(node_status.get(p) in _TERMINAL_NODE for p in parents):
                    continue
                if node.when:
                    try:
                        env = self._eval_env(run_id, upstream_view)
                        cond = evaluator.evaluate(node.when, env)
                    except evaluator.ExpressionError as exc:
                        self.store.set_run_status(run_id, "failed")
                        self.store.add_event(run_id, "run_failed",
                                             {"node": nid, "error": str(exc)})
                        return
                    if not cond:
                        self._prune_subtree(run_id, spec, nid, node_status, params,
                                            upstream_view, reason="condition_false")
                        progressed = True
                        continue
                try:
                    node_status[nid] = self._materialize_node(run_id, spec, node, params,
                                                              upstream_view)
                except Exception:
                    log.exception("materialize failed: %s/%s", run_id, nid)
                    self.store.add_event(run_id, "materialize_error",
                                         {"node": nid})
                    continue
                progressed = True

            if all(node_status.get(n.id) in _TERMINAL_NODE for n in spec.nodes):
                self.store.set_run_status(run_id, "completed")
                self.store.add_event(run_id, "run_completed", {})
                self._maybe_sink(run_id)
                return

    # ------------------------------------------------------------ steps
    def _sync_from_kanban(self, run_id: str, node_status: dict):
        """kanban 实况 → 记账校正（done/archived 卡提升节点态 + dataflow 补行）。"""
        try:
            card_view = self.compiler.card_status_view(run_id)
        except Exception:
            log.exception("card status view failed: %s", run_id)
            return
        for row in self.store.node_rows(run_id):
            card_id = row["card_id"]
            if not card_id:
                continue
            kst = card_view.get(card_id)
            if kst not in ("done", "archived"):
                continue
            target = NODE_DONE if kst == "done" else NODE_PRUNED
            if target == NODE_DONE and row["status"] != NODE_DONE:
                latest = self.client.latest_run(card_id) or {}
                meta = latest.get("metadata")
                outputs = meta.get("outputs") if isinstance(meta, dict) else {}
                self.dataflow.put_outputs(run_id, row["node_id"],
                                          outputs if isinstance(outputs, dict) else {},
                                          summary=latest.get("summary"),
                                          card_id=card_id, shard=row["shard"] or "")
            if row["status"] != target:
                self.store.set_node_status(run_id, row["node_id"], target,
                                           shard=row["shard"] or None)
        fresh = dict(self.store.node_status_map(run_id))
        node_status.clear()
        node_status.update(fresh)

    def _materialize_node(self, run_id: str, spec: DagSpec, node, params: dict,
                          upstream_view: dict) -> str:
        """物化节点（建卡）。返回该节点推进后的本地状态：done / creating。"""
        nid = node.id

        # report 合成节点：插件侧执行（汇总→单次 LLM→回调→落盘），不开 worker 会话
        if node.get("synthesis") == "report":
            from . import report as report_mod

            outs = report_mod.compile_report(run_id, node, params, upstream_view,
                                             llm_fn=self.llm_fn, sender_fn=self.sender_fn)
            created = self.compiler.create_instant_card(
                run_id, node, params, upstream_view, outputs=outs,
                summary=(outs.get("summary") or "report synthesized")[:200])
            self.dataflow.put_outputs(run_id, nid, outs, summary=outs.get("summary"),
                                      card_id=created["id"])
            upstream_view[nid] = {"outputs": outs, "summary": str(outs.get("summary") or ""),
                                  "status": NODE_DONE}
            self.store.add_event(run_id, "report_done",
                                 {"node": nid, "card_id": created["id"],
                                  "report_sent": outs.get("report_sent"),
                                  "llm_synthesized": outs.get("llm_synthesized")})
            return NODE_DONE

        if node.kind == "verdict":
            outs = verdict_mod.run_judge(node, upstream_view, judge_fn=self.judge_fn)
            created = self.compiler.create_instant_card(
                run_id, node, params, upstream_view, outputs=outs,
                summary=f"verdict={outs['verdict']}: {outs['reason']}"[:200])
            self.dataflow.put_outputs(run_id, nid, outs, summary=outs["reason"],
                                      card_id=created["id"])
            upstream_view[nid] = {"outputs": outs, "summary": outs["reason"],
                                  "status": NODE_DONE}
            self.store.add_event(run_id, "verdict_done",
                                 {"node": nid, "card_id": created["id"], **outs})
            return NODE_DONE

        if node.foreach:
            try:
                hits = evaluator.evaluate_value(node.foreach,
                                                self._eval_env(run_id, upstream_view))
            except evaluator.ExpressionError:
                hits = None
            if not isinstance(hits, list):
                hits = []
            hits = [h for h in hits if str(h).strip()]
            if not hits:
                self.compiler.create_instant_card(
                    run_id, node, params, upstream_view,
                    outputs={"foreach_empty": True},
                    summary="foreach 空集，占位完成（无该方向下游证据）")
                self.dataflow.put_outputs(run_id, nid, {"foreach_empty": True},
                                          summary="foreach empty")
                return NODE_DONE
            for h in hits:
                shard = shard_slug(h)
                created = self.compiler.create_exec_card(run_id, node, params,
                                                         upstream_view, shard=shard)
                self.store.record_node_card(run_id, nid, created["id"],
                                            shard=shard, status=NODE_CREATING)
            return NODE_CREATING

        created = self.compiler.create_exec_card(run_id, node, params, upstream_view)
        self.store.record_node_card(run_id, nid, created["id"], status=NODE_CREATING)
        return NODE_CREATING

    def _prune_subtree(self, run_id: str, spec: DagSpec, root_id: str,
                       node_status: dict, params: dict, upstream_view: dict,
                       reason: str):
        """从 root 起剪枝。汇合语义：下游节点只有当其**全部**父路径都在剪枝闭包内
        才递归剪掉；存在任何活父路径的汇合点保持 pending——被剪父的 archived 占位卡
        原生满足扇入（M0 实验 2），其余父完成后汇合点正常建卡运行
        （例：rootcause 被剪 ≠ report 被剪，任务触发图必须出报告）。"""
        pruned_ids: set = set()
        stack = [root_id]
        while stack:
            nid = stack.pop()
            node = spec.node(nid)
            if node is None or nid in pruned_ids:
                continue
            st = node_status.get(nid)
            if st in _TERMINAL_NODE or st in (NODE_CREATING, NODE_PRUNED_PENDING):
                continue
            self._mark_pruned(run_id, node, params, upstream_view, node_status, reason)
            pruned_ids.add(nid)
            self.store.add_event(run_id, "node_pruned", {"node": nid, "reason": reason})
            for child in self._children(spec, nid):
                cnode = spec.node(child)
                if cnode is None or child in pruned_ids:
                    continue
                if all(p in pruned_ids or node_status.get(p) == NODE_PRUNED
                       for p in cnode.parents):
                    stack.append(child)  # 整条支路已死 → 递归剪
                # 否则：汇合点有活父路径 → 不剪，留给正常推进
        return pruned_ids

    def _mark_pruned(self, run_id: str, node, params: dict, upstream_view: dict,
                     node_status: dict, reason: str):
        """标记单节点 pruned：父卡全就绪 → 立即建 archived 占位卡；否则 pruned-pending 延迟补。"""
        nid = node.id
        if all(self.store.cards_for_node(run_id, p) for p in node.parents):
            try:
                self.compiler.create_pruned_placeholder(run_id, node, params,
                                                        upstream_view, reason=reason)
                node_status[nid] = NODE_PRUNED
                return
            except Exception:
                log.exception("pruned placeholder failed: %s/%s", run_id, nid)
        self.store.record_node_card(run_id, nid, "", status=NODE_PRUNED_PENDING)
        node_status[nid] = NODE_PRUNED_PENDING

    def _try_pruned_placeholder(self, run_id: str, spec: DagSpec, node, params: dict) -> bool:
        if self.store.cards_for_node(run_id, node.id):
            return True
        if not all(self.store.cards_for_node(run_id, p) for p in node.parents):
            return False
        try:
            self.compiler.create_pruned_placeholder(
                run_id, node, params, self._upstream_view(run_id),
                reason="condition_false")
        except Exception:
            log.exception("delayed pruned placeholder failed: %s/%s", run_id, node.id)
            return False
        return True

    def _children(self, spec: DagSpec, node_id: str) -> list:
        return spec.children_map().get(node_id, [])

    def _maybe_sink(self, run_id: str):
        """知识回流候选（不自动回写 BKN）：verdict=hit 且 rootcause 有产出才生成。"""
        try:
            from . import sink

            path = sink.maybe_sink(run_id, self.store, self.dataflow)
            if path:
                self.store.add_event(run_id, "lp_candidate", {"path": path})
        except Exception:
            log.exception("maybe_sink error: %s", run_id)

    # ------------------------------------------------------------ helpers
    def _upstream_view(self, run_id: str) -> dict:
        view = self.dataflow.run_view(run_id)
        for entry in view.values():
            entry.setdefault("status", NODE_DONE)
        return view

    def _eval_env(self, run_id: str, upstream_view: dict) -> dict:
        run = self.store.get_run(run_id)
        params = json.loads(run["params_json"]) if run and run["params_json"] else {}
        spec = DagSpec(json.loads(run["spec_json"])) if run else None
        nodes_env = {}
        for nid, entry in (upstream_view or {}).items():
            nodes_env[nid] = {"outputs": entry.get("outputs") or {},
                              "status": entry.get("status") or NODE_DONE}
        if spec:
            for n in spec.nodes:
                nodes_env.setdefault(n.id, {"outputs": {}, "status": "pending"})
        return {"nodes": nodes_env, "params": params or {}}
