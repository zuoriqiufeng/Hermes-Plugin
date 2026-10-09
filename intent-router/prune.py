"""prune.py — 按意图裁剪本轮工具面（``llm_request`` 中间件，默认关闭）。

动机：工具 schema 通常是提示里最固定的那块开销——一次会话十几个工具，每个几百
token，每轮都要付。若意图已经明确（例如「查 -4073」只会用到诊断类工具），把无关
工具从本轮请求里摘掉，是比「少调一次模型」更直接的省法。

为什么默认关闭、且只保留到「保留清单 + 意图允许集」：这是本插件**唯一可能削弱
agent 能力**的动作。裁错了不会报错，只会让 agent 做不到本该能做的事——所以必须
先用 shadow 数据证明「该意图下确实不会用到被裁的工具」，再开。

安全边界：
- 只在**本轮首个请求**（``api_call_count == 1``，即刚分类的那次）裁剪；
  后续工具循环请求保持原样，避免长尾任务中途失去工具；
- 判定必须是第 1/2 层的确定性结论（第三层的模型判断不参与裁剪）；
- 工具名解析不出来、或裁剪后剩下的工具少于 2 个 → 整体放弃裁剪；
- ``prune_keep_tools`` 里的工具永远保留（建议放核心工具）；
- 任何异常直接放行原请求（fail-open），并在决策日志记一条 prune 记录便于度量。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Settings
from .taxonomy import Taxonomy

log = logging.getLogger("intent-router")

MIN_KEPT_TOOLS = 2


def _tool_name(entry: Any) -> Optional[str]:
    """从工具 schema 条目里取名字（兼容 OpenAI / Anthropic 两种形状）。"""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    function = entry.get("function")
    if isinstance(function, dict):
        inner = function.get("name")
        if isinstance(inner, str) and inner:
            return inner
    return None


def _locate_tool_list(request: Dict[str, Any]) -> Optional[List[Any]]:
    """找到请求里的工具列表；找不到或形状不认识就返回 None（不裁剪）。"""
    tools = request.get("tools")
    if isinstance(tools, list):
        return tools
    tool_config = request.get("toolConfig")
    if isinstance(tool_config, dict) and isinstance(tool_config.get("tools"), list):
        return tool_config["tools"]
    return None


@dataclass
class PruneStats:
    """裁剪计数（只用于日志与 `/intent stats`）。"""

    pruned_requests: int = 0
    skipped_requests: int = 0
    tools_removed: int = 0
    chars_saved: int = 0
    last_reason: str = ""
    samples: List[str] = field(default_factory=list)


def plan_prune(
    decision: Any,
    taxonomy: Taxonomy,
    settings: Settings,
    keep: List[str],
) -> Tuple[Optional[List[str]], str]:
    """算出本轮应该保留的工具名集合；返回 (保留集或 None, 原因)。

    返回 None 表示「不裁剪」，原因字符串进日志。
    """
    if not settings.prune_enabled:
        return None, "disabled"
    if decision is None or not decision.intent:
        return None, "no_intent"
    if decision.tier not in (1, 2):
        return None, "tier_not_local"
    spec = taxonomy.spec(decision.intent)
    if spec is None:
        return None, "unknown_intent"
    allowed = set(spec.allowed_tools or ())
    if not allowed:
        return None, "intent_has_no_allowed_tools"
    kept = sorted(set(keep) | allowed)
    if len(kept) < MIN_KEPT_TOOLS:
        return None, "too_few_kept"
    return kept, "ok"


def make_middleware(
    settings: Settings,
    taxonomy: Taxonomy,
    decisions: Any,
    telemetry: Any = None,
) -> Callable[..., Optional[Dict[str, Any]]]:
    """构造 ``llm_request`` 中间件回调。

    ``decisions`` 需提供 ``get(turn_id) -> Decision|None``（见 state.py）。
    """
    stats = PruneStats()

    def middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            request = kwargs.get("request")
            if not isinstance(request, dict):
                return None
            if kwargs.get("api_call_count") not in (1, 1.0):
                # 只在首个请求裁剪；工具循环的后续请求保持完整工具面
                return None
            turn_id = str(kwargs.get("turn_id") or "")
            decision = decisions.get(turn_id) if turn_id else None
            keep, reason = plan_prune(decision, taxonomy, settings, list(settings.prune_keep_tools or ()))
            stats.last_reason = reason
            if keep is None:
                stats.skipped_requests += 1
                return None
            tools = _locate_tool_list(request)
            if tools is None:
                stats.last_reason = "no_tool_list"
                stats.skipped_requests += 1
                return None
            named = [(entry, _tool_name(entry)) for entry in tools]
            if sum(1 for _entry, name in named if name) < len(named):
                # 有解析不出名字的条目——形状不认识，宁可不裁
                stats.last_reason = "unnamed_entries"
                stats.skipped_requests += 1
                return None
            keep_set = set(keep)
            pruned = [entry for entry, name in named if name in keep_set]
            if len(pruned) < MIN_KEPT_TOOLS or len(pruned) >= len(tools):
                stats.last_reason = "no_reduction"
                stats.skipped_requests += 1
                return None

            saved = _approx_chars(tools) - _approx_chars(pruned)
            new_request = dict(request)
            if isinstance(request.get("tools"), list):
                new_request["tools"] = pruned
            else:
                tool_config = dict(request.get("toolConfig") or {})
                tool_config["tools"] = pruned
                new_request["toolConfig"] = tool_config
            stats.pruned_requests += 1
            stats.tools_removed += len(tools) - len(pruned)
            stats.chars_saved += max(0, saved)
            stats.last_reason = "ok"
            if len(stats.samples) < 20:
                stats.samples.append("%s: %d→%d tools" % (decision.intent, len(tools), len(pruned)))
            if telemetry is not None:
                telemetry.record({
                    "kind": "prune",
                    "turn": turn_id,
                    "session": str(kwargs.get("session_id") or ""),
                    "intent": decision.intent,
                    "tools_before": len(tools),
                    "tools_after": len(pruned),
                    "removed": len(tools) - len(pruned),
                    "chars_saved": max(0, saved),
                })
            return {"request": new_request}
        except Exception:
            # 裁剪失败绝不影响请求本身
            log.warning("intent-router: 工具面裁剪异常，按原请求放行", exc_info=True)
            return None

    middleware.prune_stats = stats
    return middleware


def _approx_chars(tools: List[Any]) -> int:
    try:
        return len(json.dumps(tools, ensure_ascii=False, default=str))
    except Exception:
        return 0
