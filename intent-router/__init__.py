"""intent-router — 分层意图识别：正则 → 算法 → 大模型，只把残差交给模型。

register(ctx) 做四件事（全部包在 try/except 里，任何一步失败只降级本插件功能）：

1. ``pre_llm_call`` hook：每轮识别一次，按档位输出「注入块」——
   shadow 只记录；hint 注入意图提示；act 额外**直连一个只读工具**并把结果注入，
   主模型只需一次请求即可作答（省掉工具循环与路由推理）。
2. ``llm_request`` 中间件（可选）：按意图裁剪本轮工具面，省掉无关工具 schema 的 token。
3. ``/intent`` 命令族：调参与诊断工作台（explain/why/stats/tune/doctor/wrong）。
4. ``intent_classify`` 工具（可选）与 ``intent-router:classified`` 事件（供外部消费）。

铁律：
- **不确定就闭嘴**。三层门控（分数/差距/证据条数）任一不过就不表态，交回主模型自行判断；
- **不认识的输入不动手**。act 需要意图显式 ``safe_auto`` + 工具白名单 + 总开关，
  三者缺一即降级为 hint；
- **fail-open**。任何异常只记日志，绝不阻断对话（与仓库其他插件一致）。

平台事实（决定了上面的设计）：Hermes 对普通文本消息没有「受支持的」零模型调用回复
通道（只有斜杠命令能零 LLM；伪造 provider 响应要走未公开的 llm_execution 中间件）。
因此本插件不做伪响应，收益落在「注入结果 + 少一轮工具循环」和「工具面裁剪」上。

部署：软链接到 ~/.hermes/plugins/ 后 ``hermes plugins enable intent-router``；
涉及 gateway 的档位/开关改动需重启 gateway。详见 README.md。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("intent-router")

PLUGIN_DIR = Path(__file__).resolve().parent
AUX_TASK_KEY = "intent_router_classify"


def register(ctx: Any) -> None:
    """Hermes 启动时调用。任何失败都只禁用本插件，绝不影响其他插件与主流程。"""
    try:
        from .config import Settings
        from .taxonomy import load_taxonomy
        from .telemetry import Telemetry, default_data_dir
        from .state import TurnDecisions
        from .router import Router
        from .llm_tier import make_llm_fn
        from .actions import ACTION_ACT, ACTION_HINT, build_hint, execute, resolve_action
        from .commands import make_command
        from .tools import TOOLSET, make_handlers, schema as tool_schema
        from .prune import make_middleware
        from .decision import ACTION_NONE
        from .logbook import install as install_logbook
    except Exception:
        log.exception("intent-router: 模块导入失败，插件停用（fail-open）")
        return

    try:
        settings = Settings.from_ctx(ctx)
    except Exception:
        log.exception("intent-router: 读取配置失败，插件停用（fail-open）")
        return

    data_dir = default_data_dir()
    try:
        taxonomy = load_taxonomy(
            extra_paths=[settings.taxonomy] if settings.taxonomy else [],
            override_dir=str(data_dir / "taxonomy.d"),
        )
    except Exception:
        log.exception("intent-router: 意图表加载失败，插件停用（fail-open）")
        return

    telemetry = Telemetry(
        path=data_dir / "decisions.jsonl",
        enabled=settings.telemetry_enabled,
        max_bytes=settings.telemetry_max_bytes,
    )
    # 人读日志（与上面的 JSONL 双轨）：装配失败只降级为「没有这个文件」，不影响识别
    try:
        logbook = install_logbook(settings, data_dir)
    except Exception:
        log.warning("intent-router: 可读日志装配失败（该功能停用）", exc_info=True)
        logbook = None
    decisions = TurnDecisions()

    llm_channel = getattr(ctx, "llm", None) if settings.tier3_enabled else None
    try:
        llm_fn = make_llm_fn(llm_channel, settings, taxonomy)
    except Exception:
        log.warning("intent-router: 第三层初始化失败，前两层照常工作", exc_info=True)
        llm_fn = None

    router = Router(settings, taxonomy, llm_fn=llm_fn)
    telemetry.record({
        "kind": "boot",
        "plugin": "intent-router",
        "version": _version(),
        "intents": len(taxonomy.intents),
        "rules": len(taxonomy.rules),
        "keyword_terms": len(taxonomy.keyword_terms),
        "bm25_docs": taxonomy.bm25_index.doc_count if taxonomy.bm25_index else 0,
        "warnings": taxonomy.warnings[:20],
        "lexicon_drift": taxonomy.lexicon_drift,
        "mode": settings.mode,
        "tier3": bool(llm_fn is not None),
    })
    for warning in taxonomy.warnings[:20]:
        log.warning("intent-router: 意图表告警：%s", warning)

    # 启动时在可读日志里留一条「本进程装配了什么」——排查任何问题时先看它
    if logbook is not None:
        logbook.log_event(
            logging.INFO,
            "启动 版本=%s 意图=%d 规则=%d 词条=%d 模式=%s 第三层=%s 裁剪=%s 级=%s 目录=%s",
            _version(), len(taxonomy.intents), len(taxonomy.rules), len(taxonomy.keyword_terms),
            settings.mode, "on" if llm_fn is not None else "off",
            "on" if settings.prune_enabled else "off",
            settings.log_level, logbook.directory,
        )
        for warning in taxonomy.warnings[:20]:
            logbook.log_event(logging.WARNING, "意图表告警：%s", warning)

    # 第三层用的 auxiliary 槽：运维可在 auxiliary.intent_router_classify 指定廉价模型
    try:
        ctx.register_auxiliary_task(
            AUX_TASK_KEY,
            display_name="意图识别分类器",
            description="intent-router 第三层意图分类（建议指向一个廉价小模型）",
            defaults={"timeout": settings.tier3_timeout},
        )
    except Exception:
        log.warning("intent-router: auxiliary 任务注册失败（第三层将走主模型通道）", exc_info=True)

    # ── 钩子：每轮识别 + 按档位注入 ────────────────────────────────────────
    def on_pre_llm_call(**payload: Any) -> Optional[Dict[str, str]]:
        try:
            platform = str(payload.get("platform") or "")
            if not settings.enabled_for(platform):
                return None
            text = _extract_text(payload.get("user_message"))
            if not text:
                return None
            turn_id = str(payload.get("turn_id") or "")
            session_id = str(payload.get("session_id") or "")
            mode = settings.mode_for(platform)
            history = payload.get("conversation_history") or []

            outcome = router.classify(text)
            decision = outcome.decision
            # 影子模式也要算出**假如放开会做什么**（counterfactual），否则没法据日志决策。
            policy_mode = "act" if mode == "act" else "hint"
            action, reason = resolve_action(
                decision, taxonomy, settings, mode=policy_mode, history=history
            )

            context = ""
            if action == ACTION_ACT and mode != "shadow":
                try:
                    dispatch = getattr(ctx, "dispatch_tool", None)
                    if callable(dispatch):
                        act = execute(decision, taxonomy, settings,
                                      lambda name, args: dispatch(name, args))
                        decision.tool = act.tool
                        decision.tool_ms = act.tool_ms
                        if act.action == ACTION_ACT:
                            context = act.context
                        else:
                            action, reason = ACTION_HINT, act.reason
                            context = build_hint(decision)
                    else:
                        action, reason = ACTION_HINT, "no_dispatch_tool"
                        context = build_hint(decision)
                except Exception:
                    log.warning("intent-router: 直连工具失败，降级为提示", exc_info=True)
                    action, reason = ACTION_HINT, "tool_exception"
                    context = build_hint(decision)
            elif action == ACTION_HINT:
                context = build_hint(decision)

            if mode == "shadow" and action != ACTION_NONE:
                reason = "shadow:" + (reason or action)
                context = ""

            decision.action = action
            decision.action_reason = reason or ""
            decision.injected_chars = len(context or "")
            record = decision.to_record(
                session_id=session_id, turn_id=turn_id, platform=platform,
                mode=mode, settings=settings,
            )
            telemetry.record(record)
            if logbook is not None:
                # 决策行直接吃同一份记录字典：两条轨道口径永远一致（不会「JSONL 有、日志没有」）
                logbook.log_decision(record)
                if decision.tool and decision.action == ACTION_HINT and reason:
                    # 本来要直连工具却降级了——排查时最需要知道原因
                    logbook.log_event(logging.WARNING,
                                      "直连工具 %s 降级为提示：%s", decision.tool, reason)
            decisions.put(turn_id, decision)

            if settings.emit_events:
                emitter = getattr(ctx, "emit", None)
                if callable(emitter):
                    try:
                        emitter("classified", {
                            "session_id": session_id, "turn_id": turn_id, "platform": platform,
                            "intent": decision.intent or "", "tier": decision.tier_name(),
                            "score": decision.score, "action": action,
                            "entities": decision.entities,
                        })
                    except Exception:
                        log.debug("intent-router: 事件发布失败", exc_info=True)

            if context:
                return {"context": context}
            return None
        except Exception:
            log.warning("intent-router: pre_llm_call 处理失败（本轮等同未装插件）", exc_info=True)
            return None

    try:
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
    except Exception:
        log.exception("intent-router: 注册 pre_llm_call 失败")
        return

    if settings.prune_enabled:
        try:
            ctx.register_middleware("llm_request",
                                    make_middleware(settings, taxonomy, decisions, telemetry))
        except Exception:
            log.warning("intent-router: 注册工具面裁剪中间件失败（该功能禁用）", exc_info=True)

    try:
        ctx.register_command(
            "intent",
            make_command(router, taxonomy, settings, telemetry, decisions,
                         plugin_dir=PLUGIN_DIR, data_dir=data_dir, logbook=logbook),
            description="分层意图识别：识别/统计/调参/自检",
            args_hint="<文本> | why | stats [n] | tune | doctor | wrong <意图id> | help",
        )
    except Exception:
        log.warning("intent-router: 注册 /intent 命令失败", exc_info=True)

    if settings.expose_tool:
        try:
            handlers = make_handlers(router, taxonomy, settings)
            for name, handler in handlers.items():
                ctx.register_tool(toolset=TOOLSET, name=name, schema=tool_schema(), handler=handler)
        except Exception:
            log.warning("intent-router: 注册 intent_classify 工具失败", exc_info=True)

    try:
        ctx.on_unload(telemetry.close)
        if logbook is not None:
            ctx.on_unload(logbook.close)
    except Exception:
        pass

    log.info("intent-router: 已注册（mode=%s 意图=%d 规则=%d 词条=%d 第三层=%s 裁剪=%s）",
             settings.mode, len(taxonomy.intents), len(taxonomy.rules),
             len(taxonomy.keyword_terms), bool(llm_fn is not None), settings.prune_enabled)


def _extract_text(user_message: Any) -> str:
    """从 hook payload 的 user_message 取文本；多模态（list）时拼接其中的文本段。"""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, (list, tuple)):
        parts: List[str] = []
        for item in user_message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _version() -> str:
    """版本号（与 plugin.yaml 手工同步；只用于 boot 记录，不做校验）。"""
    return "0.1.0"
