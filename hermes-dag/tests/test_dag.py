"""hermes-dag 单元测试（不依赖 hermes CLI：Kanban 用 FakeKanbanClient 模拟）

覆盖：
- evaluator：合法/非法表达式、evaluate/evaluate_value
- dsl：解析/环检测/引用校验/edges 并集/verdict 约束
- render：点路径/缺省/JSON 值
- runstore：run 幂等、队列原子消费、节点记账
- verdict：正常/非法返回/fallback
- advancer：hit 路径（方向→verdict→rootcause×2→report）、miss 路径（剪枝占位）、
            foreach 空集占位、pruned-pending 延迟补卡、sync 恢复
运行：python -m pytest hermes-dag/tests -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

import pytest

# 目录名 hermes-dag 带连字符，无法常规 import——用 importlib 以 "dag" 名装载
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec_pkg = importlib.util.spec_from_file_location(
    "dag", os.path.join(_PLUGIN_DIR, "__init__.py"),
    submodule_search_locations=[_PLUGIN_DIR])
dag = importlib.util.module_from_spec(_spec_pkg)
sys.modules["dag"] = dag
_spec_pkg.loader.exec_module(dag)

from dag import evaluator, dsl, render  # noqa: E402
from dag.advancer import Advancer  # noqa: E402
from dag.compiler import Compiler, build_body  # noqa: E402
from dag.dataflow import DataflowStore  # noqa: E402
from dag.runstore import RunStore  # noqa: E402
from dag import verdict as verdict_mod  # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeKanbanError(RuntimeError):
    pass


class FakeKanbanClient:
    """模拟 Kanban 核心语义：幂等键去重、parents 必须存在、done/archived 满足扇入。"""

    KanbanError = FakeKanbanError

    def __init__(self):
        self.tasks = {}
        self.links = {}
        self.runs = {}
        self.comments = {}
        self._seq = 0

    def _next_id(self):
        self._seq += 1
        return f"t_fake{self._seq:04d}"

    def create(self, title, body=None, parents=None, skills=None,
               idempotency_key=None, assignee=None, priority=0):
        if idempotency_key:
            for tid, t in self.tasks.items():
                if t.get("idempotency_key") == idempotency_key and t["status"] != "archived":
                    return {"id": tid, "status": t["status"], "deduped": True}
        tid = self._next_id()
        for p in parents or []:
            if p not in self.tasks:
                raise FakeKanbanError(f"unknown parent {p}")
        self.tasks[tid] = {"id": tid, "title": title, "body": body, "status": "ready",
                           "skills": skills or [], "idempotency_key": idempotency_key,
                           "assignee": assignee}
        self.links[tid] = list(parents or [])
        return {"id": tid, "status": self.tasks[tid]["status"]}

    def complete(self, task_id, summary=None, metadata=None):
        t = self.tasks[task_id]
        assert t["status"] in ("ready", "running", "blocked", "review"), \
            f"complete on {t['status']}"
        t["status"] = "done"
        self.runs[task_id] = {"summary": summary, "metadata": metadata}
        return True

    def archive(self, task_id):
        self.tasks[task_id]["status"] = "archived"
        return True

    def comment(self, task_id, text):
        self.comments.setdefault(task_id, []).append(text)
        return True

    def show(self, task_id):
        t = self.tasks.get(task_id)
        if not t:
            return None
        run = self.runs.get(task_id) or {}
        return {**t, "latest_summary": run.get("summary"),
                "runs": [run] if run else []}

    def list_tasks(self):
        return [dict(t, id=tid) for tid, t in self.tasks.items()]

    def status_map(self):
        return {tid: t["status"] for tid, t in self.tasks.items()}

    def latest_run(self, task_id):
        run = self.runs.get(task_id)
        return run if run else None

    # 测试辅助：模拟 worker 完成方向卡
    def worker_complete(self, task_id, outputs, summary="done"):
        self.complete(task_id, summary=summary, metadata={"outputs": outputs})


class Rig:
    """完整引擎装配（Fake 客户端 + 临时库）。"""

    def __init__(self, judge_fn=None, llm_fn=None, sender_fn=None):
        self.tmp = tempfile.mkdtemp(prefix="dag-test-")
        self.store = RunStore(os.path.join(self.tmp, "runs.db"))
        self.dataflow = DataflowStore(os.path.join(self.tmp, "dataflow.db"))
        self.client = FakeKanbanClient()
        self.compiler = Compiler(self.client, self.store, "test-board")
        self.advancer = Advancer(self.store, self.dataflow, self.client,
                                 self.compiler, judge_fn=judge_fn, llm_fn=llm_fn,
                                 sender_fn=sender_fn, poll_interval=999)

    def run_spec(self, spec: dict, params=None, idem=None):
        self.store.save_spec(spec)
        eng_run_id = f"{spec['dag']}-test-0001"
        assert self.store.create_run(eng_run_id, spec, params or {}, None, idem)
        return eng_run_id

    def advance(self, run_id):
        self.advancer.advance_run(run_id)


def mini_spec():
    return {
        "dag": "mini",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "exec", "body_template": "A {{ params.x }}"},
            {"id": "b", "kind": "exec", "body_template": "B"},
            {"id": "verdict", "kind": "verdict", "parents": ["a", "b"],
             "judge": {"instructions": "sum"}},
            {"id": "rc", "kind": "exec", "parents": ["verdict"],
             "when": "nodes.verdict.outputs.verdict == 'hit'",
             "foreach": "nodes.verdict.outputs.hits",
             "body_template": "RC {{ params.x }}"},
            {"id": "report", "kind": "exec", "parents": ["verdict", "rc"],
             "body_template": "R verdict={{ nodes.verdict.outputs.verdict }}"},
        ],
    }


# ------------------------------------------------------------------ evaluator
def test_evaluator_basic():
    env = {"nodes": {"v": {"outputs": {"verdict": "hit"}, "status": "done"}},
           "params": {"x": 1}}
    assert evaluator.evaluate("nodes.v.outputs.verdict == 'hit'", env)
    assert not evaluator.evaluate("nodes.v.outputs.verdict == 'miss'", env)
    assert evaluator.evaluate("nodes.v.outputs.verdict == 'hit' and params.x == 1", env)
    assert evaluator.evaluate("nodes.v.outputs.verdict in ['hit', 'miss']", env)
    assert not evaluator.evaluate("nodes.v.outputs.verdict != 'hit'", env)
    assert evaluator.evaluate("nodes.v.outputs.missing == None", env)


def test_evaluator_rejects_injection():
    for bad in ["__import__('os')", "nodes.v.outputs.x if True else 1",
                "open('/etc/passwd')", "[x for x in range(3)]",
                "nodes.v.outputs.x + 1 == 2"]:
        assert evaluator.validate_expression(bad), bad


def test_evaluator_value():
    env = {"nodes": {"v": {"outputs": {"hits": ["log", "conn"]}}},
           "params": {}}
    assert evaluator.evaluate_value("nodes.v.outputs.hits", env) == ["log", "conn"]


# ------------------------------------------------------------------ dsl
def test_dsl_parse_and_edges_union():
    spec = dsl.parse_spec("""
dag: t1
version: 1
nodes:
  - {id: a, kind: exec, body_template: A}
  - {id: b, kind: exec, body_template: B}
  - {id: v, kind: verdict, judge: {instructions: i}}
edges:
  - {from: a, to: v}
  - {from: b, to: v}
""")
    assert spec.node("v").parents == ["a", "b"]


def test_dsl_cycle():
    with pytest.raises(dsl.DagSpecError) as ei:
        dsl.parse_spec("""
dag: t2
nodes:
  - {id: a, kind: exec, body_template: A, parents: [b]}
  - {id: b, kind: exec, body_template: B, parents: [a]}
""")
    assert "cycle" in str(ei.value)


def test_dsl_unknown_parent_and_verdict_rules():
    with pytest.raises(dsl.DagSpecError) as ei:
        dsl.parse_spec("""
dag: t3
nodes:
  - {id: a, kind: exec, body_template: A, parents: [ghost]}
""")
    assert "unknown parent" in str(ei.value)
    with pytest.raises(dsl.DagSpecError):
        dsl.parse_spec("""
dag: t4
nodes:
  - {id: v, kind: verdict, parents: [a]}
  - {id: a, kind: exec, body_template: A}
""")
    with pytest.raises(dsl.DagSpecError):
        dsl.parse_spec("""
dag: t5
nodes:
  - {id: a, kind: exec}
""")


# ------------------------------------------------------------------ render
def test_render_paths_and_missing():
    ctx = {"run": {"id": "r1"}, "params": {"x": 3},
           "nodes": {"v": {"outputs": {"hits": ["a", "b"]}}}}
    assert render.render("run={{ run.id }} x={{ params.x }}", ctx) == "run=r1 x=3"
    assert render.render("h={{ nodes.v.outputs.hits }}", ctx) == 'h=["a", "b"]'
    assert render.render("no={{ nodes.ghost.outputs.x }}|", ctx) == "no=|"


def test_build_body_stamps_contract():
    node = {"id": "n1", "kind": "exec"}
    body = build_body("TARGET {{ params.x }}", "r1", node, {"x": "v"}, {}, board="b1")
    assert "TARGET v" in body and "[DAG] run=r1 node=n1" in body
    assert "kanban_complete" in body


# ------------------------------------------------------------------ runstore
def test_runstore_idempotency_and_queue():
    with tempfile.TemporaryDirectory() as tmp:
        store = RunStore(os.path.join(tmp, "r.db"))
        assert store.create_run("run1", {"dag": "d"}, {}, None, "key1")
        assert not store.create_run("run2", {"dag": "d"}, {}, None, "key1")
        assert store.get_run_by_idempotency("key1")["run_id"] == "run1"

        store.enqueue("completed", "t1")
        store.enqueue("blocked", "t2")
        batch = store.dequeue()
        assert [e["task_id"] for e in batch] == ["t1", "t2"]
        assert store.dequeue() == []
        assert store.pending_queue_size() == 0

        store.record_node_card("run1", "a", "t_a", status="creating")
        store.record_node_card("run1", "a", "t_a", status="done")
        assert store.node_for_task("t_a")["node_id"] == "a"
        # 空占位记账（pruned-pending 延迟补卡场景）走独立节点
        store.record_node_card("run1", "b", "", status="pruned-pending")
        assert store.cards_for_node("run1", "a") == [
            {"shard": "", "card_id": "t_a", "status": "done"}]
        assert store.cards_for_node("run1", "b") == []
        assert store.node_status_map("run1")["b"] == "pruned-pending"
        store.close()


# ------------------------------------------------------------------ verdict
def test_verdict_normalization():
    node = {"id": "v", "parents": ["a"], "judge": {"instructions": "extra"}}
    view = {"a": {"outputs": {"verdict": "hit", "evidence": ["e1"]}, "summary": "s"}}

    out = verdict_mod.run_judge(node, view, judge_fn=lambda instructions, input_text:
                                {"verdict": "hit", "hits": ["a"], "reason": "r"})
    assert out["verdict"] == "hit"

    # hit 无 hits → 降级 inconclusive
    out = verdict_mod.run_judge(node, view, judge_fn=lambda **kw:
                                {"verdict": "hit", "hits": [], "reason": "r"})
    assert out["verdict"] == "inconclusive"

    # 非 hit 却给 hits → 清空
    out = verdict_mod.run_judge(node, view, judge_fn=lambda **kw:
                                {"verdict": "clean", "hits": ["a"], "reason": "r"})
    assert out["hits"] == []

    # judge 异常 → inconclusive
    def boom(**kw):
        raise RuntimeError("llm down")
    out = verdict_mod.run_judge(node, view, judge_fn=boom)
    assert out["verdict"] == "inconclusive" and "失败" in out["reason"]


# ------------------------------------------------------------------ advancer
def _complete_and_advance(rig, run_id, card_id, outputs):
    rig.client.worker_complete(card_id, outputs)
    rig.advancer.on_task_completed(run_id, card_id)


def test_full_hit_path_with_foreach():
    rig = Rig(judge_fn=lambda instructions, input_text:
              {"verdict": "hit", "hits": ["a", "b"], "reason": "两方向命中"})

    run_id = rig.run_spec(mini_spec(), params={"x": "P1"})
    rig.advance(run_id)
    # 首层 a/b 已建
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    b = [t for t in rig.client.tasks.values() if t["title"].endswith("b")][0]
    assert "P1" in a["body"]  # 模板渲染 params
    assert rig.store.node_status_map(run_id)["a"] == "creating"

    # 方向卡完成 → verdict 建卡并立即完成
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "hit", "evidence": ["ea"]})
    _complete_and_advance(rig, run_id, b["id"], {"verdict": "miss", "evidence": []})
    vrow = rig.store.cards_for_node(run_id, "verdict")
    assert len(vrow) == 1 and rig.client.tasks[vrow[0]["card_id"]]["status"] == "done"
    vmeta = rig.client.runs[vrow[0]["card_id"]]["metadata"]["outputs"]
    assert vmeta["verdict"] == "hit" and vmeta["hits"] == ["a", "b"]

    # foreach hits=[a, b] → 2 张 rootcause shard 卡；report 尚未建（等 rc）
    rig.advance(run_id)
    rc_rows = rig.store.cards_for_node(run_id, "rc")
    assert len(rc_rows) == 2
    # report 的 parents 含 verdict+rc 占位（rc 未完成）→ 未建
    assert rig.store.cards_for_node(run_id, "report") == []

    # 两张 rc 完成 → report 建
    for row in rc_rows:
        _complete_and_advance(rig, run_id, row["card_id"], {"verdict": "hit",
                                                            "root_cause": "rc"})
    rep_rows = rig.store.cards_for_node(run_id, "report")
    assert len(rep_rows) == 1
    rep_body = rig.client.tasks[rep_rows[0]["card_id"]]["body"]
    assert "verdict=hit" in rep_body
    _complete_and_advance(rig, run_id, rep_rows[0]["card_id"], {"verdict": "hit"})
    assert rig.store.get_run(run_id)["status"] == "completed"


def test_miss_path_prunes_subtree_with_placeholder():
    """miss → rc 剪枝：占位卡 archived，report 仍被建（task 触发必须出报告）。"""
    rig = Rig(judge_fn=lambda **kw: {"verdict": "clean", "hits": [], "reason": "全 miss"})
    run_id = rig.run_spec(mini_spec(), params={"x": "P"})
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    b = [t for t in rig.client.tasks.values() if t["title"].endswith("b")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "miss"})
    _complete_and_advance(rig, run_id, b["id"], {"verdict": "miss"})
    rig.advance(run_id)

    # rc 剪枝：占位卡 archived
    rc_rows = rig.store.cards_for_node(run_id, "rc")
    assert len(rc_rows) == 1
    assert rig.client.tasks[rc_rows[0]["card_id"]]["status"] == "archived"
    assert rig.store.node_status_map(run_id)["rc"] == "pruned"
    # report 建立（parents: verdict done + rc archived 占位 → 扇入满足）
    rep_rows = rig.store.cards_for_node(run_id, "report")
    assert len(rep_rows) == 1
    parents = rig.client.links[rep_rows[0]["card_id"]]
    v_id = rig.store.cards_for_node(run_id, "verdict")[0]["card_id"]
    assert v_id in parents and rc_rows[0]["card_id"] in parents
    _complete_and_advance(rig, run_id, rep_rows[0]["card_id"], {"verdict": "clean"})
    assert rig.store.get_run(run_id)["status"] == "completed"


def test_foreach_empty_placeholder():
    spec = {"dag": "fe", "nodes": [
        {"id": "a", "kind": "exec", "body_template": "A"},
        {"id": "f", "kind": "exec", "parents": ["a"],
         "foreach": "nodes.a.outputs.hits", "body_template": "F"},
        {"id": "z", "kind": "exec", "parents": ["f"], "body_template": "Z"},
    ]}
    rig = Rig()
    run_id = rig.run_spec(spec)
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "miss", "hits": []})
    # foreach 空集 → 占位 done 卡 → z 可建
    f_rows = rig.store.cards_for_node(run_id, "f")
    assert len(f_rows) == 1
    assert rig.client.tasks[f_rows[0]["card_id"]]["status"] == "done"
    assert rig.client.runs[f_rows[0]["card_id"]]["metadata"]["outputs"] == \
        {"foreach_empty": True}
    z_rows = rig.store.cards_for_node(run_id, "z")
    assert len(z_rows) == 1


def test_join_with_live_path_runs():
    """汇合语义：p 被剪但汇合点 d 还有活父路径（c）→ d 不剪，c 完成后正常建卡。"""
    spec = {"dag": "jn", "nodes": [
        {"id": "a", "kind": "exec", "body_template": "A"},
        {"id": "c", "kind": "exec", "body_template": "C"},
        {"id": "p", "kind": "exec", "parents": ["a"], "body_template": "P",
         "when": "nodes.a.outputs.verdict == 'never'"},
        {"id": "d", "kind": "exec", "parents": ["p", "c"], "body_template": "D"},
    ]}
    rig = Rig()
    run_id = rig.run_spec(spec)
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    c = [t for t in rig.client.tasks.values() if t["title"].endswith("c")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "miss"})
    # p 剪枝（archived 占位）；d 不剪（c 活，尚未建卡记账 → 默认 pending）
    assert rig.store.node_status_map(run_id)["p"] == "pruned"
    assert rig.store.node_status_map(run_id).get("d", "pending") == "pending"
    # c 完成 → d 正常建卡（p 的 archived 占位满足扇入）
    _complete_and_advance(rig, run_id, c["id"], {"verdict": "miss"})
    d_rows = rig.store.cards_for_node(run_id, "d")
    assert len(d_rows) == 1
    assert rig.client.tasks[d_rows[0]["card_id"]]["status"] == "ready"
    parents = rig.client.links[d_rows[0]["card_id"]]
    assert rig.store.cards_for_node(run_id, "p")[0]["card_id"] in parents


def test_sync_recovers_lost_hook():
    """hook 事件丢失：reconcile 的 sync 用 kanban 实况补记账/dataflow。"""
    rig = Rig(judge_fn=lambda **kw: {"verdict": "inconclusive", "hits": [],
                                     "reason": "no judge"})
    run_id = rig.run_spec(mini_spec(), params={"x": "P"})
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    b = [t for t in rig.client.tasks.values() if t["title"].endswith("b")][0]
    # 模拟 hook 丢失：只动 kanban，不调 on_task_completed
    rig.client.worker_complete(a["id"], {"verdict": "miss"})
    rig.client.worker_complete(b["id"], {"verdict": "miss"})
    rig.advancer.reconcile()
    assert rig.store.node_status_map(run_id)["a"] == "done"
    assert rig.dataflow.node_outputs(run_id, "a")["verdict"] == "miss"
    assert rig.store.cards_for_node(run_id, "verdict")


def test_idempotent_card_creation_via_rerun():
    """重复 advance 不重复建卡（幂等键 + 记账）。"""
    rig = Rig()
    run_id = rig.run_spec(mini_spec(), params={"x": "P"})
    rig.advance(run_id)
    rig.advance(run_id)
    rig.advance(run_id)
    titles = [t["title"] for t in rig.client.tasks.values()]
    assert len(titles) == len(set(titles)) == 2  # 仅 a、b


def test_sink_candidate_on_hit():
    """verdict=hit 且 rootcause 有产出 → LP 候选落待审目录；miss → 不产。"""
    from dag import sink

    rig = Rig(judge_fn=lambda **kw: {"verdict": "hit", "hits": ["a"], "reason": "r"})
    run_id = rig.run_spec(mini_spec(), params={"x": "P", "error_code": "CDB6157A"})
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    b = [t for t in rig.client.tasks.values() if t["title"].endswith("b")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "hit", "evidence": ["e"]})
    _complete_and_advance(rig, run_id, b["id"], {"verdict": "miss"})
    rig.advance(run_id)
    rc = rig.store.cards_for_node(run_id, "rc")[0]
    _complete_and_advance(rig, run_id, rc["card_id"],
                          {"verdict": "hit", "root_cause": "checksum 不一致",
                           "evidence": ["e1", "e2"]})
    rep = rig.store.cards_for_node(run_id, "report")[0]
    _complete_and_advance(rig, run_id, rep["card_id"], {"verdict": "hit"})
    pending = os.path.join(os.path.dirname(rig.store.db_path), "patterns-pending")
    files = os.listdir(pending) if os.path.isdir(pending) else []
    assert f"{run_id}.json" in files
    data = json.load(open(os.path.join(pending, f"{run_id}.json"), encoding="utf-8"))
    assert data["status"] == "candidate"
    assert data["proposed_patterns"][0]["root_cause"] == "checksum 不一致"

    # miss 路径不产生候选
    rig2 = Rig(judge_fn=lambda **kw: {"verdict": "clean", "hits": [], "reason": "全 miss"})
    rid2 = rig2.run_spec(mini_spec(), params={"x": "P"})
    rig2.advance(rid2)
    a2 = [t for t in rig2.client.tasks.values() if t["title"].endswith("a")][0]
    b2 = [t for t in rig2.client.tasks.values() if t["title"].endswith("b")][0]
    _complete_and_advance(rig2, rid2, a2["id"], {"verdict": "miss"})
    _complete_and_advance(rig2, rid2, b2["id"], {"verdict": "miss"})
    rig2.advance(rid2)
    rep2 = rig2.store.cards_for_node(rid2, "report")[0]
    _complete_and_advance(rig2, rid2, rep2["card_id"], {"verdict": "clean"})
    assert not os.path.isdir(os.path.join(os.path.dirname(rig2.store.db_path),
                                          "patterns-pending"))


def test_webhook_server():
    """webhook：health 免鉴权 / 缺错 token 401 / 正确 token 200 / 坏 JSON 400 / 无 token 拒启。"""
    import urllib.error
    import urllib.request

    from dag.webhook import WebhookServer, start_webhook

    class StubEngine:
        def dag_run(self, dag, params=None, idempotency_key=None, **kw):
            assert dag == "mini"
            return {"ok": True, "run_id": "r1", "created": idempotency_key != "dup",
                    "dag": dag}

        def dag_status(self, run_id):
            return {"ok": run_id == "r1", "run_id": run_id}

    # token 强制：未配置拒绝启动
    assert start_webhook(StubEngine(), host="0.0.0.0", port=0, token=None) is None

    srv = WebhookServer(StubEngine(), host="127.0.0.1", port=0, token="s3cret")
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"

    def call(method, path, body=None, token="s3cret"):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Dag-Token": token} if token else {})})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        code, data = call("GET", "/health", token=None)
        assert code == 200 and data["ok"] is True  # health 免鉴权
        code, _ = call("POST", "/dag/run", {"dag": "mini"}, token=None)
        assert code == 401  # 缺 token
        code, _ = call("POST", "/dag/run", {"dag": "mini"}, token="wrong")
        assert code == 401  # 错 token
        code, data = call("POST", "/dag/run", {"dag": "mini", "params": {"x": 1}})
        assert code == 200 and data["ok"] and data["run_id"] == "r1"
        code, data = call("POST", "/dag/run", {"dag": "mini", "idempotency_key": "dup"})
        assert code == 200 and data["created"] is False  # 幂等键透传
        code, _ = call("POST", "/dag/run", {"params": {}})  # 缺 dag
        assert code == 400
        code, _ = call("POST", "/dag/run", {"dag": "mini", "params": []})  # params 类型错
        assert code == 400
        code, data = call("GET", "/dag/status?run_id=r1")
        assert code == 200 and data["ok"] is True
        code, _ = call("GET", "/dag/status?run_id=ghost")
        assert code == 404
        code, _ = call("GET", "/nope")
        assert code == 404
    finally:
        srv.stop()


def test_is_gateway_process():
    """argv 门控：只有长驻 gateway（gateway run）才启动 webhook/advancer。"""
    from dag.runtime import is_gateway_process

    assert is_gateway_process(["python", "-m", "hermes_cli.main", "gateway", "run"])
    assert is_gateway_process(["hermes", "gateway", "run", "--replace"])
    assert not is_gateway_process(["hermes", "gateway", "restart"])
    assert not is_gateway_process(["hermes", "gateway", "stop"])
    assert not is_gateway_process(["hermes", "chat", "-q", "hi"])
    assert not is_gateway_process(["hermes", "kanban", "list"])
    assert not is_gateway_process([])


def test_webhook_bind_conflict_retries_then_gives_up():
    """端口被占：短重试后放弃并返回 None（fail-open），不抛异常。"""
    from dag.webhook import WebhookServer, start_webhook

    class StubEngine:
        def dag_run(self, *a, **kw):
            return {"ok": True}

        def dag_status(self, run_id):
            return {"ok": True}

    holder = WebhookServer(StubEngine(), host="127.0.0.1", port=0, token="t")
    holder.start()
    try:
        out = start_webhook(StubEngine(), host="127.0.0.1", port=holder.port,
                            token="t", retries=2, retry_delay=0.05)
        assert out is None
    finally:
        holder.stop()


def test_report_synthesis_node():
    """synthesis: report = 插件侧合成：不开 worker 会话，单次 LLM + 回调 + 落盘。"""
    os.environ["HERMES_DAG_REPORTS_DIR"] = os.path.join(tempfile.mkdtemp(prefix="dag-rep-"), "reports")

    spec = {"dag": "syn", "params": {"callback_address": {"type": "str"}},
            "nodes": [
                {"id": "a", "kind": "exec", "body_template": "A"},
                {"id": "v", "kind": "verdict", "parents": ["a"],
                 "judge": {"instructions": "i"}},
                {"id": "report", "kind": "exec", "synthesis": "report", "parents": ["v", "a"]},
            ]}
    sent = []

    def fake_sender(run_id, payload_path, address):
        sent.append((run_id, address))
        return {"sent": True, "returncode": 0}

    def fake_llm(instructions, input_text, json_schema, schema_name):
        assert "六维度" in instructions and "a" in input_text
        return {"summary": "s", "root_cause": "rc", "impact": "im",
                "suggestion": "sg", "severity": "low", "need_human_input": False}

    rig = Rig(judge_fn=lambda **kw: {"verdict": "hit", "hits": ["a"], "reason": "r"},
              llm_fn=fake_llm, sender_fn=fake_sender)
    run_id = rig.run_spec(spec, params={"callback_address": "1.2.3.4:8080"})
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "hit", "evidence": ["e"]})
    rep_rows = rig.store.cards_for_node(run_id, "report")
    assert len(rep_rows) == 1
    assert rig.client.tasks[rep_rows[0]["card_id"]]["status"] == "done"
    outs = rig.client.runs[rep_rows[0]["card_id"]]["metadata"]["outputs"]
    assert outs["llm_synthesized"] is True and outs["report_sent"] is True
    assert outs["root_cause"] == "rc" and sent and sent[0][1] == "1.2.3.4:8080"
    assert outs["artifacts"] and all(os.path.exists(p) for p in outs["artifacts"])
    assert rig.store.get_run(run_id)["status"] == "completed"


def test_report_synthesis_fallback():
    """LLM 失败 → 模板兜底仍完成；无 callback_address → 回调跳过（不静默失败）。"""
    def boom(**kw):
        raise RuntimeError("llm down")

    spec = {"dag": "synf", "nodes": [
        {"id": "a", "kind": "exec", "body_template": "A"},
        {"id": "v", "kind": "verdict", "parents": ["a"], "judge": {"instructions": "i"}},
        {"id": "report", "kind": "exec", "synthesis": "report", "parents": ["v"]},
    ]}
    rig = Rig(judge_fn=lambda **kw: {"verdict": "inconclusive", "hits": [], "reason": "卡点"})
    os.environ["HERMES_DAG_REPORTS_DIR"] = os.path.join(tempfile.mkdtemp(prefix="dag-rep2-"), "reports")
    run_id = rig.run_spec(spec, params={})
    rig.advance(run_id)
    a = [t for t in rig.client.tasks.values() if t["title"].endswith("a")][0]
    _complete_and_advance(rig, run_id, a["id"], {"verdict": "inconclusive"})
    rep_rows = rig.store.cards_for_node(run_id, "report")
    assert len(rep_rows) == 1 and rig.client.tasks[rep_rows[0]["card_id"]]["status"] == "done"
    outs = rig.client.runs[rep_rows[0]["card_id"]]["metadata"]["outputs"]
    assert outs["llm_synthesized"] is False and outs["need_human_input"] is True
    assert outs["callback"]["skipped"] is True
    assert rig.store.get_run(run_id)["status"] == "completed"


def test_dsl_synthesis_validation():
    with pytest.raises(dsl.DagSpecError) as ei:
        dsl.parse_spec("""
dag: t6
nodes:
  - {id: a, kind: exec, body_template: A}
  - {id: r, kind: exec, synthesis: report, parents: [a], foreach: "nodes.a.outputs.x"}
""")
    assert "cannot combine with foreach" in str(ei.value)
    with pytest.raises(dsl.DagSpecError):
        dsl.parse_spec("""
dag: t7
nodes:
  - {id: a, kind: exec, body_template: A}
  - {id: r, kind: exec, synthesis: email, parents: [a]}
""")


def test_real_template_loads():
    """业务模板本身必须通过校验（v2：rootcause 内联进方向卡）。"""
    from dag.tools import TEMPLATES_DIR
    text = (TEMPLATES_DIR / "diag-rule-error.yaml").read_text(encoding="utf-8")
    spec = dsl.parse_spec(text)
    assert set(spec.node_ids()) >= {"dir-log", "dir-conn", "dir-cap", "dir-rule",
                                    "verdict", "report"}
    assert "rootcause" not in spec.node_ids()  # v2：根因闭环内联进方向卡
    depths = spec.depth_map()
    assert depths["verdict"] == 1 and depths["report"] == 2
