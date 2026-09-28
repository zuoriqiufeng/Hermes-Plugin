"""sink.py — pattern-sink 接口（知识回流的"候选"半环）

铁律：**不自动回写 BKN**。run 完成后从 rootcause outputs 提取候选 LP 草稿，
落到插件待审目录（plugin-data/hermes-dag/patterns-pending/<run_id>.json），
由人 / i2stream-bkn-updater 流程评审后搬运进 <bkn>/log-mode/（LP 正式库）。

目标 schema 对齐 skill/i2stream-log-pattern-modeler（LP-XXXX.json）：
    pattern_id / log_fingerprint[] / component / severity / symptom /
    root_cause / remediation[] / error_codes? / db_types? / updated_at
候选允许字段不齐，标 status: candidate + source run 信息。
"""

from __future__ import annotations

import json
import os
import time

from .runstore import default_db_path

PENDING_DIR_NAME = "patterns-pending"


def pending_dir(base_dir: str | None = None) -> str:
    if base_dir is None:
        base_dir = os.path.dirname(default_db_path())
    return os.path.join(base_dir, PENDING_DIR_NAME)


def build_lp_candidate(run_id: str, params: dict, rootcause_outputs: list) -> dict | None:
    """rootcause outputs（多 shard 的 items）→ LP 候选草稿；无可用根因时返回 None。"""
    causes = []
    for out in rootcause_outputs:
        if not isinstance(out, dict):
            continue
        if out.get("foreach_empty"):
            continue
        rc = out.get("root_cause") or out.get("root_cause_hint")
        if rc:
            causes.append({
                "root_cause": rc,
                "symptom": out.get("symptom") or (params or {}).get("symptom") or "",
                "remediation": out.get("remediation") or out.get("suggestion") or [],
                "error_codes": out.get("error_codes")
                or ([params.get("error_code")] if (params or {}).get("error_code") else []),
                "evidence_count": len(out.get("evidence") or []),
            })
    if not causes:
        return None
    return {
        "status": "candidate",
        "source": {"kind": "hermes-dag", "run_id": run_id},
        "proposed_patterns": causes,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def sink_candidate(run_id: str, candidate: dict, base_dir: str | None = None) -> str | None:
    """落待审目录（原子写）。返回文件路径；无候选返回 None。异常只记日志不外抛。"""
    try:
        out_dir = pending_dir(base_dir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{run_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(candidate, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception:
        import logging

        logging.getLogger("hermes-dag.sink").exception("sink candidate failed: %s", run_id)
        return None


def maybe_sink(run_id: str, store, dataflow) -> str | None:
    """advancer 在 run completed 时调用：verdict=hit 且 run 内存在根因产出才生成候选。

    不耦合节点命名：扫描全节点 outputs，凡含 root_cause/root_cause_hint 的都算
    根因产出（verdict/report 的 outputs 天然不含该字段，无需排除表）。
    """
    try:
        run = store.get_run(run_id)
        if not run:
            return None
        view = dataflow.run_view(run_id)
        verdict_entry = view.get("verdict") or view.get("final_verdict") or {}
        if (verdict_entry.get("outputs") or {}).get("verdict") != "hit":
            return None
        producers: list = []
        for nid, entry in view.items():
            outputs = entry.get("outputs") or {}
            items = outputs.get("items") if isinstance(outputs, dict) else None
            for out in (items if isinstance(items, list) else [outputs]):
                if isinstance(out, dict) and (out.get("root_cause") or out.get("root_cause_hint")):
                    producers.append(out)
        if not producers:
            return None
        params = json.loads(run["params_json"]) if run.get("params_json") else {}
        candidate = build_lp_candidate(run_id, params, producers)
        if not candidate:
            return None
        return sink_candidate(run_id, candidate,
                              base_dir=os.path.dirname(store.db_path))
    except Exception:
        import logging

        logging.getLogger("hermes-dag.sink").exception("maybe_sink failed: %s", run_id)
        return None
