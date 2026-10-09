"""decision.py — 一次识别的完整结果（也是决策日志的记录模型）。

设计原则：**决策必须可解释、可回溯**。每条记录都要能回答「哪一层、什么证据、
多少分、差多少分、为什么没表态、有没有动工具」。调参和归因全靠它，所以宁可字段
多一点，也不要事后靠猜。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TIER_NONE = 0
TIER_REGEX = 1
TIER_ALGO = 2
TIER_LLM = 3

TIER_NAMES = {TIER_NONE: "none", TIER_REGEX: "regex", TIER_ALGO: "algo", TIER_LLM: "llm"}

# 动作档位
ACTION_NONE = "none"
ACTION_HINT = "hint"
ACTION_ACT = "act"


@dataclass
class Decision:
    """一次识别的结果。``skipped`` 非空表示整条跳过（此时不应有任何注入或工具调用）。"""

    text_hash: str = ""
    preview: str = ""
    tier: int = TIER_NONE
    intent: Optional[str] = None
    score: float = 0.0
    margin: float = 0.0
    sources: int = 0
    evidence: List[str] = field(default_factory=list)
    candidates: List[Tuple[str, float]] = field(default_factory=list)
    slots: Dict[str, str] = field(default_factory=dict)
    entities: Dict[str, str] = field(default_factory=dict)
    rule_id: str = ""
    vetoed: bool = False
    skipped: str = ""
    gate_reason: str = ""
    elapsed_ms: float = 0.0

    tier3_used: bool = False
    tier3_ms: float = 0.0
    tier3_error: str = ""

    action: str = ACTION_NONE
    action_reason: str = ""
    tool: str = ""
    tool_ms: float = 0.0
    injected_chars: int = 0

    def tier_name(self) -> str:
        return TIER_NAMES.get(self.tier, "none")

    @property
    def classified(self) -> bool:
        """是否真的给出了意图（哪怕最终不动手）。"""
        return bool(self.intent) and not self.skipped

    def to_record(
        self,
        *,
        session_id: str = "",
        turn_id: str = "",
        platform: str = "",
        mode: str = "",
        settings: Any = None,
    ) -> Dict[str, Any]:
        """转成日志行（字段名稳定，外部消费方可依赖）。"""
        limit = getattr(settings, "telemetry_preview_chars", 120)
        preview = self.preview if len(self.preview) <= limit else self.preview[:limit] + "…"
        return {
            "ts": round(time.time(), 3),
            "session": session_id,
            "turn": turn_id,
            "platform": platform,
            "mode": mode,
            "hash": self.text_hash,
            "preview": preview,
            "tier": self.tier_name(),
            "intent": self.intent or "",
            "score": self.score,
            "margin": self.margin,
            "sources": self.sources,
            "evidence": self.evidence,
            "candidates": [[name, score] for name, score in self.candidates[:4]],
            "entities": self.entities,
            "slots": self.slots,
            "rule": self.rule_id,
            "vetoed": self.vetoed,
            "skipped": self.skipped,
            "gate_reason": self.gate_reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "tier3_used": self.tier3_used,
            "tier3_ms": round(self.tier3_ms, 3),
            "tier3_error": self.tier3_error,
            "action": self.action,
            "action_reason": self.action_reason,
            "tool": self.tool,
            "tool_ms": round(self.tool_ms, 3),
            "injected_chars": self.injected_chars,
        }
