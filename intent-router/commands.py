"""commands.py — ``/intent`` 命令族：调参与诊断的工作台（全部零 LLM，除显式要求）。

    /intent <文本>        跑一遍完整阶梯，打印层级/意图/分数/证据/门控原因/耗时
    /intent why           回看本进程最近一次识别（含动作决策与原因）
    /intent stats [n]     聚合最近 n 条决策日志：层分布、第三层占比、延迟分位、估算省下的 token
    /intent tune          在标注语料上扫阈值网格，输出「无错判 + 覆盖率」最优组合
    /intent doctor        自检：意图表/词表/规则计数、词表漂移、告警、当前档位与开关
    /intent wrong <id>    记一条误判（写入 corrections.jsonl），供后续补充语料
    /intent help          用法

这些命令是「影子模式 → 放开」这条路上唯一的操作面：没有它们，阈值就只能猜。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import corpus as corpus_mod
from . import telemetry as telemetry_mod
from .router import explain

log = logging.getLogger("intent-router")

SUBCOMMANDS = ("why", "stats", "tune", "doctor", "wrong", "log", "help", "explain")

USAGE = """用法：
  /intent <文本>      识别一段文本（例：/intent -4073）
  /intent why         回看最近一次识别与动作决策
  /intent stats [n]   聚合最近 n 条决策（默认 500）
  /intent log [n]     看插件日志最近 n 行（默认 40，支持配到自定义目录）
  /intent tune        在标注语料上扫阈值（min_score × margin × min_evidence）
  /intent doctor      自检意图表/词表/漂移/日志与配置
  /intent wrong <意图id>  记录一次误判（提示修正语料）
  /intent help        本说明"""


def make_command(
    router: Any,
    taxonomy: Any,
    settings: Any,
    telemetry: Any,
    decisions: Any,
    *,
    plugin_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    logbook: Any = None,
) -> Callable[[str], str]:
    """构造 ``/intent`` 的 handler（签名遵循 Hermes 插件命令契约 ``fn(raw_args) -> str``）。"""
    plugin_dir = plugin_dir or Path(__file__).resolve().parent
    data_dir = data_dir or telemetry_mod.default_data_dir()

    def handler(raw_args: str) -> str:
        try:
            return _dispatch(raw_args or "", router, taxonomy, settings, telemetry,
                             decisions, plugin_dir, data_dir, logbook)
        except Exception as exc:
            log.warning("intent-router: /intent 执行异常", exc_info=True)
            return "intent-router: 命令执行失败（已记录日志）：%s" % exc

    return handler


def _dispatch(raw: str, router: Any, taxonomy: Any, settings: Any, telemetry: Any,
              decisions: Any, plugin_dir: Path, data_dir: Path, logbook: Any = None) -> str:
    text = (raw or "").strip()
    if not text:
        return USAGE
    head, _sep, rest = text.partition(" ")
    head_lower = head.lower()
    if head_lower == "log":
        return _log(rest.strip(), logbook, settings)
    if head_lower == "help":
        return USAGE
    if head_lower == "why":
        return _why(decisions)
    if head_lower == "stats":
        return _stats(rest.strip(), telemetry, settings)
    if head_lower == "tune":
        return _tune(taxonomy, settings, plugin_dir)
    if head_lower == "doctor":
        return _doctor(taxonomy, settings, router, telemetry, decisions, plugin_dir, logbook)
    if head_lower == "wrong":
        return _wrong(rest.strip(), decisions, data_dir)
    if head_lower == "explain":
        text = rest.strip() or text
    return explain(router.classify(text), settings)


def _why(decisions: Any) -> str:
    decision = decisions.last()
    if decision is None:
        return "还没有识别记录（本进程内）。"
    lines = [
        "最近一次识别：",
        "  输入: %s" % (decision.preview or "(空)"),
        "  层级: %s   意图: %s   分数: %.2f   证据: %d" % (
            decision.tier_name(), decision.intent or "(不表态)", decision.score, decision.sources),
        "  门控: %s%s" % (decision.gate_reason or "-", "（含被否决候选）" if decision.vetoed else ""),
        "  动作: %s   原因: %s" % (decision.action, decision.action_reason or "-"),
        "  耗时: %.2f ms%s" % (
            decision.elapsed_ms,
            "   第三层: %.0f ms" % decision.tier3_ms if decision.tier3_used else "",
        ),
    ]
    if decision.tool:
        lines.append("  工具: %s（%.0f ms）" % (decision.tool, decision.tool_ms))
    if decision.injected_chars:
        lines.append("  注入: %d 字符" % decision.injected_chars)
    return "\n".join(lines)


def _stats(rest: str, telemetry: Any, settings: Any) -> str:
    limit = 500
    if rest:
        try:
            limit = max(1, min(int(rest), 20000))
        except ValueError:
            return "stats 参数需为整数（默认 500）"
    records = telemetry.tail(limit)
    if not records:
        return "决策日志为空（%s）。确认 telemetry_enabled 且本机已有会话流量。" % telemetry.path
    summary = telemetry_mod.summarize(records)
    lines = [
        "最近 %d 条记录（%d 条决策 / %d 条裁剪）：" % (
            summary["records"], summary["decisions"], summary["prune_events"]),
        "  层分布: %s" % summary["tiers"],
        "  表态率: %.1f%%   第三层: %d 次（%.1f%%）   触及工具: %d 次（%.1f%%）" % (
            summary["classified_rate"] * 100, summary["tier3_used"],
            summary["tier3_rate"] * 100, summary["acted"], summary["act_rate"] * 100),
        "  延迟 ms: p50=%.2f p95=%.2f max=%.2f" % (
            summary["latency_ms"]["p50"], summary["latency_ms"]["p95"], summary["latency_ms"]["max"]),
        "  注入: %d 字符（粗估 %d tokens）" % (
            summary["injected_chars"], summary["injected_chars_est_tokens"]),
        "  顶部意图: %s" % ", ".join(
            "%s=%d" % (name, count) for name, count in list(summary["intents"].items())[:8]),
        "  门控原因: %s" % summary["gate_reasons"],
    ]
    if summary["skipped"]:
        lines.append("  跳过: %s" % summary["skipped"])
    if summary["prune_events"]:
        lines.append("  工具面裁剪: %d 次，移除 %d 个工具定义，约省 %d 字符" % (
            summary["prune_events"], summary["prune_tools_removed"], summary["prune_chars_saved"]))
    if not settings.telemetry_enabled:
        lines.append("  注意：telemetry_enabled=false，当前看到的是历史数据。")
    return "\n".join(lines)


def _tune(taxonomy: Any, settings: Any, plugin_dir: Path) -> str:
    cases, warnings = corpus_mod.load_corpus()
    if not cases:
        return "语料读不到：%s" % (warnings or plugin_dir / "data" / "corpus" / "regression.yaml")
    rows = corpus_mod.sweep(taxonomy, settings, cases)
    lines = [
        "语料 %d 条（正 %d / 负 %d）——按「错判+误报」升序、覆盖率降序取前 8 行：" % (
            len(cases),
            sum(1 for case in cases if case.expect),
            sum(1 for case in cases if not case.expect),
        ),
        "  min_score margin evidence | 覆盖率  本地精度  第一层精度  错判 误报",
    ]
    for row in rows[:8]:
        lines.append("  %8.2f %6.2f %8d | %6.1f%% %8.1f%% %10.1f%% %5d %4d" % (
            row["min_score"], row["margin"], row["min_evidence"],
            row["coverage"] * 100, row["local_precision"] * 100,
            row["tier1_precision"] * 100, row["wrong"], row["false_positives"],
        ))
    current_key = (settings.min_score, settings.margin, settings.min_evidence)
    lines.append("当前配置：min_score=%.2f margin=%.2f min_evidence=%d" % current_key)
    current_row = next(
        (row for row in rows
         if (row["min_score"], row["margin"], row["min_evidence"]) == current_key),
        None,
    )
    best = rows[0]
    best_key = (best["wrong"] + best["false_positives"], best["coverage"])
    ties = [row for row in rows
            if (row["wrong"] + row["false_positives"], row["coverage"]) == best_key]
    if current_row is None:
        lines.append("（当前配置不在扫描网格内，未做等价性比较）")
    elif (current_row["wrong"] + current_row["false_positives"], current_row["coverage"]) == best_key:
        if len(ties) > 1:
            lines.append(
                "当前配置已在最优档：这 %d 组阈值在语料上**等价**（错判/覆盖率完全相同）——"
                "分数是离散的，调更松不会换来覆盖，建议保持现状或往更保守的一侧取。"
                % len(ties))
        else:
            lines.append("当前配置即最优行，无需调整。")
    else:
        lines.append("提示：最优行为 min_score=%.2f margin=%.2f min_evidence=%d"
                     "（覆盖率 %.1f%%，错判 %d，误报 %d）——调整前先确认取舍可接受。"
                     % (best["min_score"], best["margin"], best["min_evidence"],
                        best["coverage"] * 100, best["wrong"], best["false_positives"]))
    if warnings:
        lines.append("语料告警：%s" % "; ".join(warnings))
    return "\n".join(lines)


def _log(rest: str, logbook: Any, settings: Any) -> str:
    """``/intent log [n]``：在会话里直接看插件日志（无需 shell）。"""
    limit = 40
    if rest:
        try:
            limit = max(1, min(int(rest), 500))
        except ValueError:
            return "用法：/intent log [行数]，行数默认 40"
    if logbook is None or not getattr(logbook, "enabled", False):
        return "插件日志未启用（log_enabled=false，或装配失败——见 /intent doctor）。"
    lines = logbook.tail(limit)
    path = logbook.current_path()
    header = "%s（最近 %d 行）" % (path or logbook.directory, len(lines))
    if not lines:
        return header + "\n（当前分片还没有内容；log_level=%s 时未表态的输入不会记录）" % settings.log_level
    return "\n".join([header] + lines)


def _doctor(taxonomy: Any, settings: Any, router: Any, telemetry: Any, decisions: Any,
            plugin_dir: Path, logbook: Any = None) -> str:
    keyword_total = sum(len(spec.keywords) for spec in taxonomy.intents.values())
    lines = [
        "intent-router 自检：",
        "  意图: %d 个 %s" % (len(taxonomy.intents), sorted(taxonomy.intents.keys())),
        "  规则: %d 条（第一层）" % len(taxonomy.rules),
        "  词条: %d 个（去重后进入自动机）；AC 模式数=%s；BM25 文档=%s" % (
            keyword_total, getattr(taxonomy.keyword_automaton, "pattern_count", 0),
            taxonomy.bm25_index.doc_count if taxonomy.bm25_index else 0),
        "  否定词: %d 条" % len(taxonomy.veto_terms),
        "  词表文件: %s" % (", ".join(Path(p).name for p in taxonomy.lexicon_files) or "(无)"),
        "  档位: mode=%s surfaces=%s auto_enabled=%s 允许工具=%s" % (
            settings.mode, settings.surfaces or "(全部)", settings.auto_enabled,
            settings.auto_allow_tools or "(空)"),
        "  开关: tier3=%s(%s) prune=%s expose_tool=%s telemetry=%s" % (
            settings.tier3_enabled, settings.tier3_policy, settings.prune_enabled,
            settings.expose_tool, settings.telemetry_enabled),
        "  阈值: min_score=%.2f margin=%.2f min_evidence=%d" % (
            settings.min_score, settings.margin, settings.min_evidence),
        "  决策日志(JSONL): %s（%s）" % (
            telemetry.path, "开" if settings.telemetry_enabled else "关"),
        "  " + (logbook.describe() if logbook is not None
               else "插件日志: 未装配（见启动日志）"),
    ]
    lines.append("  日志设置: level=%s 上限=%dB 同日分片≤%d 保留=%d天 决策行=%s 预览=%d字符 脱敏=%s pid=%s" % (
        settings.log_level, settings.log_max_bytes, settings.log_backups_per_day,
        settings.log_retention_days, settings.log_decisions, settings.log_preview_chars,
        settings.log_redact, settings.log_include_pid))
    if logbook is not None and settings.log_enabled:
        lines.append("  日志目录: %s（自检提示：目录不可写只会静默丢日志，首次失败会 warn 一次）"
                     % logbook.directory)
    llm_fn = getattr(router, "llm_fn", None)
    if llm_fn is not None:
        cache = getattr(llm_fn, "cache", None)
        lines.append("  第三层缓存: %d 条（上限 %d）" % (
            len(cache) if cache is not None else 0, settings.tier3_cache_size))
    else:
        lines.append("  第三层: 未接（llm 通道缺失或 tier3_enabled=false）")
    if taxonomy.lexicon_drift:
        lines.append("  ⚠ 词表漂移: %s" % "; ".join(taxonomy.lexicon_drift))
    if taxonomy.warnings:
        lines.append("  意图表告警 %d 条：" % len(taxonomy.warnings))
        for warning in taxonomy.warnings[:10]:
            lines.append("    - %s" % warning)
        if len(taxonomy.warnings) > 10:
            lines.append("    …（其余 %d 条见日志）" % (len(taxonomy.warnings) - 10))
    else:
        lines.append("  意图表告警: 无")
    recent = decisions.recent(5)
    if recent:
        lines.append("  最近识别: %s" % "; ".join(
            "%s→%s(%s)" % (item.text_hash or "-", item.intent or "-", item.tier_name())
            for item in recent))
    return "\n".join(lines)


def _wrong(rest: str, decisions: Any, data_dir: Path) -> str:
    intent_id = (rest or "").strip()
    if not intent_id:
        return "用法：/intent wrong <正确的意图 id>——会把最近一次识别的文本与你的修正写进 corrections.jsonl"
    decision = decisions.last()
    if decision is None:
        return ("还没有**真实会话**的识别记录，无法关联修正。\n"
                "（/intent <文本> 是试算、不计入「最近一次」——先像平常那样发一条消息，再回来标记。）")
    record = {
        "ts": round(time.time(), 3),
        "kind": "correction",
        "hash": decision.text_hash,
        "preview": decision.preview,
        "predicted": decision.intent or "",
        "corrected": intent_id,
        "tier": decision.tier_name(),
        "score": decision.score,
    }
    path = Path(data_dir) / "corrections.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        return "写入修正记录失败：%s" % exc
    return "已记录修正：%s → %s（%s）\n建议把这条补进 %s，让回归测试覆盖它。" % (
        decision.intent or "(不表态)", intent_id, decision.preview, "data/corpus/regression.yaml")
