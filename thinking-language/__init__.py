"""thinking-language — 每轮向模型注入「深度思考用中文」的语言指令。

指令式插件（directive）：通过官方 ``pre_llm_call`` hook 在每次 LLM 调用
前返回 ``{"context": ...}``，该上下文在 **API 调用时** 注入本轮 user
message（不落会话历史、不污染持久化，契约见 ``agent/turn_context.py``
的 ``_collect_pre_llm_call_context``；每轮注入，因为注入不进历史）。

背景：模型的 reasoning 语言受 prompt 环境影响——hermes 主系统提示词为
英文，深度思考因此大多吐英文。一条显式的中文规则可把思考语言拉回中文
（指令是强影响，不是硬保证；术语/代码保留英文属正常）。

失败策略：hook 内任何异常只记 warning 并返回 None（本轮等同未装插件），
绝不打断对话。纯 stdlib，无自定义配置；措辞直接改 ``LANGUAGE_RULE``。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LANGUAGE_RULE = (
    "深度思考（reasoning/思维链）过程请全程使用中文输出；"
    "代码、命令、专有名词与引用的英文原文保留。"
)

# 首次触发记一条含 platform/session 的诊断日志，之后不再刷屏。
_announced = False


def on_pre_llm_call(**payload: Any) -> Optional[Dict[str, str]]:
    global _announced
    try:
        if not _announced:
            _announced = True
            logger.info(
                "thinking-language: pre_llm_call 已生效（platform=%s session=%s），"
                "每轮注入中文思考指令",
                payload.get("platform"), payload.get("session_id"),
            )
        return {"context": LANGUAGE_RULE}
    except Exception:
        logger.warning(
            "thinking-language: pre_llm_call 处理失败（本轮等同未装插件）",
            exc_info=True,
        )
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
