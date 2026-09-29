"""smoke_integration.py — hermes-dag 引擎 × 真实 kanban CLI 集成冒烟

用法（沙箱库，gateway 调度器不可见，不会 spawn worker）：
    mkdir -p /tmp/hermes-dag-smoke && rm -f /tmp/hermes-dag-smoke/*
    HERMES_KANBAN_DB=/tmp/hermes-dag-smoke/kanban.db hermes kanban init
    /hdd/demo/public/venv/bin/python hermes-dag/tests/smoke_integration.py

覆盖：dag_run 首层建卡（真 CLI）→ 模拟 worker 交卡（CLI complete --metadata）→
推进器（verdict 即时卡 / foreach shard / report）→ run completed → 幂等重入 →
hook 入队探测（CLI 进程是否加载插件 hook）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

SANDBOX = "/tmp/hermes-dag-smoke"
KANBAN_DB = os.path.join(SANDBOX, "kanban.db")
RUNS_DB = os.path.join(SANDBOX, "runs.db")
os.environ["HERMES_KANBAN_DB"] = KANBAN_DB
os.environ["HERMES_DAG_REPORTS_DIR"] = os.path.join(SANDBOX, "reports")  # 沙箱报告不进实盘目录

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "dag", os.path.join(PLUGIN_DIR, "__init__.py"),
    submodule_search_locations=[PLUGIN_DIR])
dag = importlib.util.module_from_spec(_spec)
sys.modules["dag"] = dag
_spec.loader.exec_module(dag)

from dag.kanban_client import KanbanClient  # noqa: E402
from dag.tools import DagEngine  # noqa: E402


def fake_judge(instructions: str, input_text: str) -> dict:
    return {"verdict": "hit", "hits": ["dir-log"], "reason": "smoke: 日志方向命中"}


def main() -> int:
    client = KanbanClient(board=None, db_path=KANBAN_DB)  # 沙箱：钉库，默认板
    engine = DagEngine(board=None, db_path=RUNS_DB, client=client,
                       judge_fn=fake_judge, advancer_enabled=False)

    print("=== 1. dag_explain（内置模板） ===")
    explain = engine.dag_explain("diag-rule-error")
    assert explain["ok"], explain
    print(f"nodes: {[n['id'] for n in explain['nodes']]}")

    print("=== 2. dag_run（首层四方向卡，真 CLI 建卡） ===")
    params = {"rule_id": "CDB6157A-SMOKE", "error_code": "CDB6157A",
              "symptom": "规则报错冒烟", "log_excerpt": "ERROR ... checksum"}
    r1 = engine.dag_run("diag-rule-error", params=params,
                        idempotency_key="smoke:run:1")
    assert r1["ok"] and r1["created"], r1
    run_id = r1["run_id"]
    print(f"run_id={run_id}")

    dir_cards = {}
    for node_id in ("dir-log", "dir-conn", "dir-cap", "dir-rule"):
        rows = engine.store.cards_for_node(run_id, node_id)
        assert len(rows) == 1, f"{node_id} expected 1 card, got {rows}"
        dir_cards[node_id] = rows[0]["card_id"]
    print(f"方向卡: {dir_cards}")

    print("=== 3. dag_status（建图后） ===")
    st = engine.dag_status(run_id)
    print(st["graph"])

    print("=== 4. 幂等重入 ===")
    r2 = engine.dag_run("diag-rule-error", params=params, idempotency_key="smoke:run:1")
    assert r2["ok"] and not r2["created"] and r2["run_id"] == run_id, r2
    print("dup dag_run → 既有 run ✓")

    print("=== 5. 模拟四方向 worker 交卡（CLI complete --metadata） ===")
    outputs = {
        "dir-log": {"verdict": "hit", "evidence": ["grep 命中 CDB6157A ×3", "qdrant: LP-0042"],
                    "direction_rank": 1},
        "dir-conn": {"verdict": "miss", "evidence": [], "direction_rank": 9},
        "dir-cap": {"verdict": "miss", "evidence": [], "direction_rank": 9},
        "dir-rule": {"verdict": "inconclusive", "evidence": [], "direction_rank": 5},
    }
    for nid, card_id in dir_cards.items():
        client.complete(card_id, summary=f"{nid} done",
                        metadata={"outputs": outputs[nid]})
    engine.advancer.run_once()
    print("verdict 裁决 + report 合成后：")

    vrows = engine.store.cards_for_node(run_id, "verdict")
    assert len(vrows) == 1 and client.show(vrows[0]["card_id"])["status"] == "done", \
        "verdict 虚拟卡应已建并完成"
    print(f"verdict 卡 done ✓（{vrows[0]['card_id']}）")

    print("=== 6. report 合成 → run completed（v2：rootcause 内联进方向卡） ===")
    rep_rows = engine.store.cards_for_node(run_id, "report")
    assert len(rep_rows) == 1, f"report 卡应已建: {rep_rows}"
    rep_show = client.show(rep_rows[0]["card_id"])
    assert rep_show["status"] == "done", "synthesis report 卡应由推进器即时完成"
    rep_out = (client.latest_run(rep_rows[0]["card_id"]).get("metadata") or {}).get("outputs") or {}
    assert rep_out.get("llm_synthesized") is False and rep_out.get("summary"), rep_out
    print("report 合成卡即时完成 ✓（冒烟无 llm_fn → 模板兜底；无 callback_address → 回调跳过）")
    run = engine.store.get_run(run_id)
    assert run["status"] == "completed", f"run 应 completed: {run['status']}"
    print("run completed ✓")

    print("=== 7. 最终 dag_status ===")
    st = engine.dag_status(run_id)
    print(st["graph"])
    print("事件时间线（尾部 6 条）:")
    for e in st["events"][:6]:
        print(f"  {e['kind']}: {json.dumps(e['payload'], ensure_ascii=False)[:100]}")

    print("=== 8. hook 入队探测（CLI 进程是否加载插件 hook） ===")
    probe_card = client.create("smoke-hook-probe")["id"]
    before = engine.store.pending_queue_size()
    rc = os.system(
        f"HERMES_KANBAN_DB={KANBAN_DB} hermes kanban complete {probe_card} "
        f"--result probe >/dev/null 2>&1")
    after = engine.store.pending_queue_size()
    fired = after > before
    print(f"CLI complete exit={rc} queue {before}→{after} → "
          + ("hook 在 CLI 进程生效 ✓" if fired else
             "hook 未在 CLI 进程触发（预期内：60s dispatch_tick 对账兜底）"))

    print("=== 9. webhook 冒烟（本机临时端口 + token，重放幂等键） ===")
    import urllib.error
    import urllib.request

    from dag.webhook import WebhookServer
    wh = WebhookServer(engine, host="127.0.0.1", port=0, token="smoke-token")
    wh.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{wh.port}/dag/run",
            data=json.dumps({"dag": "diag-rule-error", "params": params,
                             "idempotency_key": "smoke:run:1"}).encode(),
            headers={"Content-Type": "application/json", "X-Dag-Token": "smoke-token"})
        resp = json.load(urllib.request.urlopen(req, timeout=15))
        assert resp["ok"] and resp["created"] is False and resp["run_id"] == run_id, resp
        print(f"webhook 重放幂等键 → 既有 run {resp['run_id']} ✓（端口 {wh.port}）")
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{wh.port}/dag/run",
                                       data=b'{"dag":"diag-rule-error"}',
                                       headers={"Content-Type": "application/json"}),
                timeout=10)
            raise AssertionError("无 token 应 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
            print("无 token 请求 → 401 ✓")
    finally:
        wh.stop()

    engine.shutdown()
    print("\nSMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
