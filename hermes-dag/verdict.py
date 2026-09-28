"""verdict.py — 裁决节点：ctx.llm.complete_structured 结构化汇总裁决

铁律（思路 ≠ 结论）：judge 只读各方向卡 worker 现场取证产生的 outputs.evidence，
不查 BKN——BKN 里只有"怎么查"，没有"查到了什么"。

口径：inconclusive ≠ clean（宁可误报不可漏报）——写死在 instructions，spec 只补领域描述。
judge_fn 注入：生产环境由 __init__ 用 plugin_api.llm 包装；测试注入 fake。
judge 失败 fail-open：返回 inconclusive（run 不静默、report 如实说明）。
"""

from __future__ import annotations

import json

JUDGE_SCHEMA = {
    "name": "dag_verdict",
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["clean", "hit", "inconclusive"]},
            "hits": {"type": "array", "items": {"type": "string"},
                     "description": "命中方向的 node_id 列表"},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "hits", "reason"],
    },
}

BASE_INSTRUCTIONS = """你是诊断 DAG 的裁决节点（verdict judge）。输入是各方向卡的 outputs
（每张方向卡的 worker 已现场只读取证并结构化交卡）。

裁决规则（必须遵守）：
1. 只依据输入中的 evidence 裁决，不引入输入之外的事实（思路≠结论：你没有工具，只有证据）。
2. 任一方向 verdict=hit → overall=hit，hits=[命中的 node_id 列表]（按 evidence 强度排序）。
3. 无 hit 但存在 inconclusive → overall=inconclusive（inconclusive ≠ clean：宁可误报不可漏报）。
4. 全部 miss → overall=clean。
5. hits 非空时 overall 必须为 hit；verdict=hit 时 hits 不得为空。
6. reason 用中文一句话说明裁决依据（引用关键证据，不超过 200 字）。"""


def build_judge_input(upstream_view: dict, verdict_node_id: str, parent_ids: list) -> str:
    """父卡 outputs → judge 输入文本（只含 verdict 的父节点，按 node_id 组织）。"""
    lines = [f"方向卡输出汇总（run 内，共 {len(parent_ids)} 个方向）:"]
    for nid in parent_ids:
        entry = (upstream_view or {}).get(nid) or {}
        outputs = entry.get("outputs")
        summary = entry.get("summary")
        lines.append(f"\n### {nid}")
        if summary:
            lines.append(f"summary: {summary}")
        lines.append("outputs: " + json.dumps(outputs, ensure_ascii=False))
    return "\n".join(lines)


def run_judge(node: dict, upstream_view: dict, judge_fn=None) -> dict:
    """执行裁决。judge_fn(instructions: str, input_text: str) -> dict；失败/缺失 → inconclusive。"""
    node_id = node["id"]
    parent_ids = list(node.get("parents") or [])
    instructions = BASE_INSTRUCTIONS
    extra = ((node.get("judge") or {}).get("instructions") or "").strip()
    if extra:
        instructions += "\n\n领域补充（来自 spec）:\n" + extra
    input_text = build_judge_input(upstream_view, node_id, parent_ids)

    if judge_fn is None:
        return {"verdict": "inconclusive", "hits": [],
                "reason": "judge 通道不可用（judge_fn 未注入），按 inconclusive 处理"}
    try:
        raw = judge_fn(instructions=instructions, input_text=input_text)
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as exc:
        return {"verdict": "inconclusive", "hits": [],
                "reason": f"judge 调用失败（{type(exc).__name__}），按 inconclusive 处理"}

    verdict = data.get("verdict")
    hits = data.get("hits") or []
    if verdict not in ("clean", "hit", "inconclusive"):
        return {"verdict": "inconclusive", "hits": [],
                "reason": f"judge 返回非法 verdict={verdict!r}"}
    if verdict == "hit" and not hits:
        return {"verdict": "inconclusive", "hits": [],
                "reason": "judge 返回 hit 但 hits 为空，降级为 inconclusive"}
    if verdict != "hit" and hits:
        hits = []
    return {"verdict": verdict, "hits": hits,
            "reason": str(data.get("reason") or "")[:500]}
