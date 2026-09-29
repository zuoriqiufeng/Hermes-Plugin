"""compiler.py — node → kanban 卡（body 七段契约渲染、parents 翻译、幂等键、记账）

核心不变式：**Kanban 永远看到完整 DAG 拓扑**。
  - exec 卡：正常建卡，worker 执行后 kanban_complete
  - verdict 卡：judge 后 create + 立即 complete（metadata=outputs）→ done
  - foreach 空集：占位卡 create + 立即 complete（outputs={"foreach_empty": true}）
  - 被剪枝节点：占位卡 create + 立即 archive（+comment reason）→ archived
  → archived/done 都满足 Kanban 扇入（M0 实验 1/2/5），下游提升永远交给 Kanban 原生语义。
"""

from __future__ import annotations

import re

from . import render as render_mod
from .kanban_client import KanbanClient
from .runstore import RunStore

_SHARD_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def card_key(run_id: str, node_id: str, shard: str = "") -> str:
    return f"dag:{run_id}:{node_id}:{shard}" if shard else f"dag:{run_id}:{node_id}"


def shard_slug(raw) -> str:
    s = _SHARD_SAFE.sub("-", str(raw)).strip("-")
    return s[:48] or "item"


CONTRACT_APPENDIX = """

---
[DAG] run={run_id} node={node_id}{shard_part} board={board}
[输出契约] 完成时必须调用 kanban_complete 工具交卡：
  summary: 一句话结论
  metadata.outputs: {{"verdict": "hit|miss|inconclusive", "evidence": ["..."], "artifacts": [...], "direction_rank": 0}}
  （evidence 必须含原始日志行/命令输出摘录与 Qdrant enrichment 细节；≥2 个独立证据源才可下结论）
[纪律] 只读命令白名单：grep/cat/ps/ss/ls/df/stat/etcdctl get/iadebug 只读子命令，优先 i2Stream 原生工具；
  具体命令与参数以 skills 列出的 skill 为唯一来源，禁止任何写操作；先 BKN 工具（思路）后 search_qdrant（enrichment）。
  verdict=inconclusive 是合法结论（拿不到数据时如实交 inconclusive，不算失败）。
[预算] 取证 ≤20 次工具调用；预计输出 >200 行的命令先重定向到文件再摘录关键行——
  禁止把全量 keyspace/全盘 find dump 进对话；证据足够（≥2 独立源）立即交卡，不追求穷尽。
[完成] 契约字段齐全 = 完成；文字汇报不算完成。"""


def build_body(template: str, run_id: str, node, params: dict, upstream_view: dict,
               shard=None, board: str = "") -> str:
    """渲染节点 body：模板取值 + DAG 契约附录盖章。"""
    ctx = {
        "run": {"id": run_id},
        "params": params or {},
        "nodes": {nid: {"outputs": (v or {}).get("outputs"),
                        "status": (v or {}).get("status")}
                  for nid, v in (upstream_view or {}).items()},
    }
    body = render_mod.render(template, ctx)
    shard_part = f" shard={shard}" if shard else ""
    return body + CONTRACT_APPENDIX.format(
        run_id=run_id, node_id=node["id"], shard_part=shard_part, board=board or "-")


def build_title(node, run_id: str, shard=None) -> str:
    base = f"[{run_id}] {node['id']}"
    if shard:
        base += f" · {shard}"
    kind = node.get("kind", "exec")
    return base if kind == "exec" else f"{base} ({kind})"


class Compiler:
    def __init__(self, client: KanbanClient, store: RunStore, board: str):
        self.client = client
        self.store = store
        self.board = board

    def parent_card_ids(self, run_id: str, node_id: str) -> list:
        """父节点全部卡的 id（foreach 节点 = 全部 shard 卡；含 pruned 占位卡）。"""
        return [r["card_id"] for r in self.store.cards_for_node(run_id, node_id)]

    def create_exec_card(self, run_id: str, node, params: dict, upstream_view: dict,
                         shard=None) -> dict:
        """建一张 exec 卡并记账。parents 必须已有卡（advancer 保证时序）。"""
        parents = []
        for p in node.get("parents") or []:
            parents.extend(self.parent_card_ids(run_id, p))
        title = build_title(node, run_id, shard)
        body = build_body(node.get("body_template") or "", run_id, node, params,
                          upstream_view, shard=shard, board=self.board)
        key = card_key(run_id, node["id"], shard or "")
        created = self.client.create(
            title, body=body, parents=parents,
            skills=node.get("skills") or [],
            idempotency_key=key, assignee=node.get("assignee"))
        card_id = created["id"]
        self.store.record_node_card(run_id, node["id"], card_id,
                                    shard=shard or "", status="creating")
        self.store.add_event(run_id, "card_created",
                             {"node": node["id"], "shard": shard, "card_id": card_id,
                              "title": title})
        return created

    def create_instant_card(self, run_id: str, node, params: dict, upstream_view: dict,
                            outputs: dict, summary: str) -> dict:
        """verdict / foreach 空集占位：create + 立即 complete → done。"""
        created = self.create_exec_card(run_id, node, params, upstream_view)
        card_id = created["id"]
        self.client.complete(card_id, summary=summary, metadata={"outputs": outputs})
        self.store.record_node_card(run_id, node["id"], card_id, status="done")
        self.store.add_event(run_id, "card_completed_instantly",
                             {"node": node["id"], "card_id": card_id,
                              "outputs": outputs})
        return created

    def create_pruned_placeholder(self, run_id: str, node, params: dict,
                                  upstream_view: dict, reason: str = "condition_false") -> dict:
        """剪枝占位：create + archive（archived 满足扇入）。父卡可能尚未全部存在
        （深层子树先标记后补卡）→ 由调用方保证父卡就绪或稍后重试。"""
        created = self.create_exec_card(run_id, node, params, upstream_view)
        card_id = created["id"]
        self.client.comment(card_id, f"[dag] pruned: {reason}")
        self.client.archive(card_id)
        self.store.record_node_card(run_id, node["id"], card_id, status="pruned")
        self.store.add_event(run_id, "node_pruned",
                             {"node": node["id"], "card_id": card_id, "reason": reason})
        return created

    def card_status_view(self, run_id: str) -> dict:
        """{card_id: kanban status}（一次 list 全板，本地过滤）。"""
        smap = self.client.status_map()
        out = {}
        for row in self.store.node_rows(run_id):
            if row["card_id"]:
                out[row["card_id"]] = smap.get(row["card_id"])
        return out
