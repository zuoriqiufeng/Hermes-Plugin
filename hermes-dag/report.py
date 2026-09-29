"""report.py — report 合成节点：插件侧执行，不开 worker 会话

背景（效率基线）：report 作为独立 worker 会话实测 23 分钟 / 60 次 API 调用，而其工作本质是
"汇总已交证据 → 生成六维度报告 → 回调"。折叠为插件侧执行：
  1. 汇总 dataflow 中 verdict/rootcause/方向卡 outputs；
  2. 单次 ctx.llm.complete_structured 生成六维度报告；
  3. 子进程调 send_report.py 回调（params.callback_address 提供时）；
  4. 报告与 payload 落 /hdd/demo/public/reports/<run_id>/，建占位卡立即 complete（与 verdict 同模式）。

口径与 verdict 一致：只依据已交证据，不引入输入之外的事实（报告不重新取证）。
失败路径：LLM 失败 → 无 LLM 模板兜底并如实标注；回调失败 → report_sent=false（不静默）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "一句话结论"},
        "root_cause": {"type": "string"},
        "impact": {"type": "string"},
        "suggestion": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "none"]},
        "confidence": {"type": "string"},
        "need_human_input": {"type": "boolean"},
    },
    "required": ["summary", "root_cause", "impact", "suggestion"],
}

BASE_INSTRUCTIONS = """你是诊断 DAG 的报告合成节点。输入是各方向卡与 verdict/rootcause 已交的
outputs（worker 现场取证的结构化事实）。产出六维度诊断报告：现象 / 证据 / 根因 / 影响 / 建议 / 置信度。

规则：
1. 只依据输入证据，不引入输入之外的事实（你没有工具，报告不重新取证）。
2. verdict=hit → 按 rootcause 结论写实根因；verdict=clean → 如实说明各方向均未发现异常；
   verdict=inconclusive → 如实说明卡点（不可达源/缺证清单）与 need_human_input=true。
3. root_cause/impact/suggestion 若证据不足，写"未定位/待补充"，不得编造。
4. summary ≤200 字；severity 按影响判断；confidence 按证据独立源数量判断。"""

DEFAULT_SEND_REPORT = ("/hdd/demo/public/i2stream-bkn/skill/i2stream_report_sender/"
                       "scripts/send_report.py")


def _reports_dir(run_id: str) -> str:
    base = os.environ.get("HERMES_DAG_REPORTS_DIR", "/hdd/demo/public/reports")
    return os.path.join(base, run_id)


def build_report_input(upstream_view: dict, parent_ids: list) -> str:
    lines = [f"诊断 run 已交证据汇总（{len(parent_ids)} 个上游节点）："]
    for nid in parent_ids:
        entry = (upstream_view or {}).get(nid) or {}
        lines.append(f"\n### {nid}")
        if entry.get("summary"):
            lines.append(f"summary: {entry['summary']}")
        lines.append("outputs: " + json.dumps(entry.get("outputs"), ensure_ascii=False))
    return "\n".join(lines)


def _fallback_report(upstream_view: dict, parent_ids: list) -> dict:
    """无 LLM / LLM 失败时的模板兜底：verdict 原文 + 扫描全部上游 outputs 的根因字段。"""
    v = (upstream_view.get("verdict") or {}).get("outputs") or {}
    verdict = v.get("verdict") or "inconclusive"
    causes = []
    for nid, entry in (upstream_view or {}).items():
        outputs = entry.get("outputs") or {}
        items = outputs.get("items") if isinstance(outputs, dict) else None
        for out in (items if isinstance(items, list) else [outputs]):
            if isinstance(out, dict) and out.get("root_cause") and nid != "verdict":
                causes.append(f"{nid}: {out['root_cause']}")
    reason = str(v.get("reason") or "")
    return {
        "summary": reason[:200] or f"诊断完成：verdict={verdict}",
        "root_cause": "；".join(causes) if causes else ("未定位" if verdict != "clean" else "无异常"),
        "impact": "待补充",
        "suggestion": "待补充",
        "severity": "none" if verdict == "clean" else "unknown",
        "confidence": "low（无 LLM 兜底模板）",
        "need_human_input": verdict != "clean",
    }


def render_report_markdown(run_id: str, report: dict, upstream_view: dict) -> str:
    lines = [f"# 诊断报告 {run_id}", "",
             f"- **summary**: {report.get('summary', '')}",
             f"- **root_cause**: {report.get('root_cause', '')}",
             f"- **impact**: {report.get('impact', '')}",
             f"- **suggestion**: {report.get('suggestion', '')}",
             f"- **severity**: {report.get('severity', '')} | **confidence**: {report.get('confidence', '')}",
             f"- **need_human_input**: {report.get('need_human_input', False)}", "", "## 上游证据"]
    for nid, entry in (upstream_view or {}).items():
        lines.append(f"### {nid}")
        lines.append(json.dumps(entry.get("outputs"), ensure_ascii=False)[:2000])
    return "\n".join(lines)


def default_sender(run_id: str, payload_path: str, address: str) -> dict:
    """默认回调：子进程调 BKN 的 send_report.py（字段契约见 skill）。"""
    script = os.environ.get("HERMES_DAG_SEND_REPORT", DEFAULT_SEND_REPORT)
    if not os.path.exists(script):
        return {"sent": False, "error": f"send_report.py not found: {script}"}
    cmd = [sys.executable, script, "--type", "analysis-result", "--analysis-id", run_id,
           "--address", address, "--session-id", f"dag-{run_id}", "--json-file", payload_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"sent": proc.returncode == 0, "returncode": proc.returncode,
                "stderr": (proc.stderr or "")[-300:]}
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}


def _persist(run_id: str, report: dict, markdown: str, payload: dict) -> list:
    """报告/payload 落盘（best-effort），返回 artifact 路径列表。"""
    artifacts = []
    try:
        out_dir = _reports_dir(run_id)
        os.makedirs(out_dir, exist_ok=True)
        for name, content in (("report.json", json.dumps(report, ensure_ascii=False, indent=2)),
                              ("report.md", markdown),
                              ("payload.json", json.dumps(payload, ensure_ascii=False, indent=2))):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            artifacts.append(path)
    except Exception:
        pass
    return artifacts


def compile_report(run_id: str, node: dict, params: dict, upstream_view: dict,
                   llm_fn=None, sender_fn=None) -> dict:
    """report 合成入口。返回 outputs dict（作为占位卡 metadata.outputs 与 dataflow 行）。"""
    parent_ids = list(node.get("parents") or [])
    input_text = build_report_input(upstream_view, parent_ids)
    instructions = BASE_INSTRUCTIONS
    extra = ((node.get("judge") or {}).get("instructions") or "").strip()
    if extra:
        instructions += "\n\n领域补充（来自 spec）:\n" + extra

    report, llm_used = None, False
    if llm_fn is not None:
        try:
            data = llm_fn(instructions=instructions, input_text=input_text,
                          json_schema=REPORT_SCHEMA, schema_name="dag_report")
            if isinstance(data, dict) and all(data.get(k) for k in ("summary", "root_cause", "impact", "suggestion")):
                report, llm_used = data, True
        except Exception:
            report = None
    if report is None:
        report = _fallback_report(upstream_view, parent_ids)
    report["need_human_input"] = bool(report.get("need_human_input") or
                                      (upstream_view.get("verdict") or {}).get("outputs", {}).get("verdict") == "inconclusive")

    markdown = render_report_markdown(run_id, report, upstream_view)
    payload = {"summary": report.get("summary"), "root_cause": report.get("root_cause"),
               "impact": report.get("impact"), "suggestion": report.get("suggestion"),
               "severity": report.get("severity"), "confidence": report.get("confidence"),
               "source": {"kind": "hermes-dag", "run_id": run_id}}
    artifacts = _persist(run_id, report, markdown, payload)

    address = (params or {}).get("callback_address")
    callback = {"skipped": True, "reason": "no callback_address in params"}
    report_sent = False
    if address:
        payload_path = os.path.join(_reports_dir(run_id), "payload.json")
        sender = sender_fn or default_sender
        try:
            callback = sender(run_id, payload_path, address)
        except Exception as exc:
            callback = {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
        report_sent = bool(callback.get("sent"))
        callback["skipped"] = False

    outs = dict(report)
    outs.update({"report_sent": report_sent, "callback": callback,
                 "artifacts": artifacts, "llm_synthesized": llm_used,
                 "synthesized_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    return outs
