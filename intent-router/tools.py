"""tools.py — ``intent_classify`` 工具（默认不注册，config.expose_tool 打开）。

给模型的「按需咨询」入口：模型自己拿不准某句话属于哪类问题时，可以调一次拿到
本插件的分层判定（含证据与门控原因）。默认关闭的理由很实际——工具 schema 本身
就要吃主提示的 token，而这个插件的主线收益恰恰是省 token；只在对账/排查场景才值得开。

handler 返回 **JSON 字符串**（Hermes 工具契约只见 str 或多模态 envelope，
直接返回 dict 会被判 tool_result_contract 错误——同 hermes-dag/tools.py 的约定）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("intent-router")

TOOL_NAME = "intent_classify"
TOOLSET = "intent-router"

_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "对本轮用户消息做分层意图识别（正则 → 词典/BM25 → 大模型），返回意图 id、"
        "置信分数、命中的证据与门控原因。用于你自己拿不准「用户这句话属于哪类问题」时"
        "做一次对照判断。识别在本地完成、不产生副作用；返回的 intent 为空表示插件不敢表态，"
        "此时按你自己的判断处理。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要识别的用户消息原文"},
        },
        "required": ["text"],
    },
}


def schema() -> Dict[str, Any]:
    """返回工具 schema 的副本（调用方可能改动，避免污染模块级常量）。"""
    return json.loads(json.dumps(_SCHEMA, ensure_ascii=False))


def make_handlers(router: Any, taxonomy: Any, settings: Any) -> Dict[str, Callable[[Dict[str, Any]], str]]:
    """构造 ``{工具名: handler}``。handler 收 args dict，返回 JSON 字符串。"""

    def intent_classify(args: Optional[Dict[str, Any]]) -> str:
        try:
            text = str((args or {}).get("text") or "")
            outcome = router.classify(text)
            decision = outcome.decision
            spec = taxonomy.spec(decision.intent) if decision.intent else None
            payload = {
                "intent": decision.intent or "",
                "tier": decision.tier_name(),
                "score": decision.score,
                "sources": decision.sources,
                "gate_reason": decision.gate_reason,
                "skipped": decision.skipped,
                "entities": decision.entities,
                "candidates": [[name, score] for name, score in decision.candidates[:4]],
                "evidence": decision.evidence,
                "action_hint": spec.action if spec else "",
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            log.warning("intent-router: intent_classify 工具异常", exc_info=True)
            return json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)

    return {TOOL_NAME: intent_classify}
