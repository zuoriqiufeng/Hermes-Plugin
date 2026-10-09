"""router.py — 三层阶梯的编排（纯识别，不做任何动作）。

    normalize → 护栏 → 第一层正则 → 第二层算法（证据→打分→三重门控）→ 第三层大模型

层间关系是**短路**而非叠加：第一层命中就不跑后两层；第二层门控通过就不问模型；
只有前两层都「不敢下结论」时，第三层才有机会——这正是把大模型从「每条消息都过」
改成「只吃残差」的落点。

本模块只产出 Decision，不注入、不调工具、不写日志（那些在 actions.py /
telemetry.py / __init__.py 里）。这样 testing 与 `/intent` 命令可以安全地
「只识别不动作」，也让「影子模式」天然成立。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import decision as decision_mod
from .config import Settings
from .decision import Decision
from .entities import Entities, extract
from .normalize import Normalized, normalize
from .regex_tier import match_guard, match_rules
from .safety import safe_preview
from .scorer import (
    REASON_NO_EVIDENCE,
    GateOutcome,
    IntentEvidence,
    gate,
    score_all,
)
from .taxonomy import Taxonomy

log = logging.getLogger("intent-router")

TIER3_POLICY_OFF = "off"
TIER3_POLICY_AMBIGUOUS = "on_ambiguous"
TIER3_POLICY_ALWAYS = "always"

# 第一层命中的固定置信度：规则是人工写的确定性判据，给 1.0 便于日志与阈值对齐。
TIER1_SCORE = 1.0


@dataclass
class RouteOutcome:
    """识别结果 + 门控细节（`/intent` 命令与调参需要看这些）。"""

    decision: Decision
    normalized: Normalized
    entities: Entities
    gate_outcome: Optional[GateOutcome] = None


class Router:
    """三层阶梯。``llm_fn(text, candidates) -> Optional[dict]`` 由注册方注入；None 表示不启用第三层。"""

    def __init__(
        self,
        settings: Settings,
        taxonomy: Taxonomy,
        llm_fn: Optional[Callable[[str, Sequence[str]], Optional[dict]]] = None,
    ) -> None:
        self.settings = settings
        self.taxonomy = taxonomy
        self.llm_fn = llm_fn

    # ── 对外入口 ────────────────────────────────────────────────────────
    def classify(self, text: Optional[str]) -> RouteOutcome:
        """跑完三层（能短路就短路）。任何异常都退化为「不表态」，绝不抛出。"""
        started = time.perf_counter()
        settings = self.settings
        try:
            normalized = normalize(text, max_chars=settings.max_input_chars)
            decision = Decision(
                text_hash=normalized.digest,
                # 预览在**这里**就脱敏（落盘前的第一道）：先脱敏后截断，顺序不能反——
                # 反了的话，截断点上的密钥会被截成半截而绕过脱敏模式。
                preview=safe_preview(normalized.raw, settings.telemetry_preview_chars,
                                     redact=bool(getattr(settings, "log_redact", True))),
            )

            skip = match_guard(
                normalized, continuation_guard=bool(settings.continuation_guard)
            )
            if skip:
                decision.skipped = skip
                decision.gate_reason = skip
                return self._finish(decision, normalized, Entities(), None, started)

            entities = extract(normalized.text, normalized.compact)
            decision.entities = dict(entities.values)

            hit = match_rules(self.taxonomy.rules, normalized)
            if hit is not None:
                decision.tier = decision_mod.TIER_REGEX
                decision.intent = hit.intent
                decision.score = TIER1_SCORE
                decision.margin = TIER1_SCORE
                decision.sources = 3  # 规则即确定性判据，视为证据充分
                decision.rule_id = hit.rule_id
                decision.slots = dict(hit.slots)
                decision.evidence = ["rule=%s" % hit.rule_id]
                decision.gate_reason = "rule_hit"
                return self._finish(decision, normalized, entities, None, started)

            outcome = self._tier2(normalized, entities, decision)
            if outcome.accepted:
                return self._finish(decision, normalized, entities, outcome, started)

            if self._should_try_llm(decision, outcome):
                self._tier3(normalized, entities, decision)

            # 注意：这里不再回写 gate_reason——_tier2 已经写了本地门控原因，_tier3 可能
            # 用自己的结论覆盖（tier3_accept / tier3_unclear）。回写会把第三层的信息抹掉。
            return self._finish(decision, normalized, entities, outcome, started)
        except Exception:
            log.warning("intent-router: 识别流程异常，按「不表态」处理", exc_info=True)
            fallback = Decision(preview=safe_preview(text or "", 120))
            fallback.gate_reason = "exception"
            fallback.elapsed_ms = (time.perf_counter() - started) * 1000.0
            return RouteOutcome(decision=fallback, normalized=Normalized("", "", ""), entities=Entities())

    # ── 第二层 ──────────────────────────────────────────────────────────
    def _tier2(self, normalized: Normalized, entities: Entities, decision: Decision) -> GateOutcome:
        evidences = self._collect_evidence(normalized, entities)
        breakdowns = score_all(evidences, self.settings)
        outcome = gate(breakdowns, self.settings)
        decision.candidates = [(item.intent_id, item.score) for item in breakdowns[:4]]
        decision.margin = outcome.margin
        decision.sources = outcome.sources
        if outcome.accepted and outcome.intent_id:
            decision.tier = decision_mod.TIER_ALGO
            decision.intent = outcome.intent_id
            decision.score = outcome.score
            top = breakdowns[0] if breakdowns else None
            decision.evidence = list(top.evidence) if top else []
        else:
            decision.vetoed = bool(outcome.vetoed_ids)
        # 无论接受与否都记下本地门控原因：拒绝时它是「为什么没表态」的唯一线索
        decision.gate_reason = outcome.reason
        return outcome

    def _collect_evidence(self, normalized: Normalized, entities: Entities) -> List[IntentEvidence]:
        taxonomy = self.taxonomy
        store: Dict[str, IntentEvidence] = {}

        def bucket(intent_id: str) -> IntentEvidence:
            entry = store.get(intent_id)
            if entry is None:
                entry = IntentEvidence(intent_id=intent_id)
                store[intent_id] = entry
            return entry

        if taxonomy.keyword_automaton is not None and normalized.text:
            for index, _start, _end in taxonomy.keyword_automaton.iter_matches(normalized.text):
                if index >= len(taxonomy.keyword_terms):
                    continue
                term = taxonomy.keyword_terms[index]
                for intent_id, weight in taxonomy.keyword_index.get(term, ()):
                    bucket(intent_id).keyword_hits.append((term, weight))

        if taxonomy.veto_automaton is not None and normalized.text:
            seen_patterns: set = set()
            for index, start, _end in taxonomy.veto_automaton.iter_matches(normalized.text):
                key = (index, start)
                if key in seen_patterns or index >= len(taxonomy.veto_terms):
                    continue
                seen_patterns.add(key)
                pattern = taxonomy.veto_terms[index]
                for intent_id, spec in taxonomy.intents.items():
                    if pattern in spec.veto_patterns:
                        bucket(intent_id).veto_hits.append(pattern)

        if taxonomy.bm25_index is not None and normalized.text:
            from .bm25 import normalize_scores

            for intent_id, score in normalize_scores(
                taxonomy.bm25_index.score_by_id(normalized.text)
            ).items():
                if score > 0.0:
                    bucket(intent_id).bm25 = score

        if entities:
            for intent_id, spec in taxonomy.intents.items():
                hits = [name for name in spec.expects_entities if entities.has(name)]
                if hits:
                    bucket(intent_id).entity_hits = hits
                # veto_entities：消息里出现这些实体时该意图不成立（例：寒暄意图遇到
                # 错误码/库名就该让位——「你好，帮我看看 -4073」不是寒暄）。
                blocked = [name for name in spec.veto_entities if entities.has(name)]
                if blocked:
                    bucket(intent_id).veto_hits.append("<entity:%s>" % ",".join(sorted(blocked)))

        return list(store.values())

    # ── 第三层 ──────────────────────────────────────────────────────────
    def _should_try_llm(self, decision: Decision, outcome: Optional[GateOutcome]) -> bool:
        """第三层触发策略：off / on_ambiguous / always。

        ``on_ambiguous`` 的判据是「**有本地证据**但没敢下结论」——分数不够、证据不足、
        第二名太近，或第一名被否定模式否决。**毫无证据**（``no_evidence``）时不问模型：
        那多半是开放式问题/闲聊，本地词表本就不该覆盖，交给主模型正常处理最省。
        """
        settings = self.settings
        if self.llm_fn is None or not settings.tier3_enabled:
            return False
        policy = settings.tier3_policy
        if policy == TIER3_POLICY_OFF:
            return False
        if policy == TIER3_POLICY_ALWAYS:
            return True
        if outcome is None:
            return False
        return outcome.reason != REASON_NO_EVIDENCE

    def _tier3(self, normalized: Normalized, entities: Entities, decision: Decision) -> None:
        started = time.perf_counter()
        decision.tier3_used = True
        try:
            payload = self.llm_fn(normalized.text, self.taxonomy.intent_ids) or {}
        except Exception as exc:
            decision.tier3_error = str(exc)[:200]
            decision.tier3_ms = (time.perf_counter() - started) * 1000.0
            log.warning("intent-router: 第三层调用失败，按「无意见」处理：%s", exc)
            return
        decision.tier3_ms = (time.perf_counter() - started) * 1000.0
        intent_id = str(payload.get("intent") or "").strip()
        if intent_id in ("", "unclear", "unknown", "none"):
            decision.gate_reason = "tier3_unclear"
            return
        if intent_id not in self.taxonomy.intents:
            # 模型不得自造意图 id（schema 已限定，这里再兜一层）
            decision.tier3_error = "unknown_intent:%s" % intent_id[:40]
            return
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        decision.tier = decision_mod.TIER_LLM
        decision.intent = intent_id
        decision.score = max(0.0, min(1.0, confidence))
        decision.margin = 0.0
        decision.sources = 1
        decision.evidence = ["llm"]
        decision.gate_reason = "tier3_accept"

    # ── 收尾 ────────────────────────────────────────────────────────────
    def _finish(
        self,
        decision: Decision,
        normalized: Normalized,
        entities: Entities,
        outcome: Optional[GateOutcome],
        started: float,
    ) -> RouteOutcome:
        decision.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return RouteOutcome(
            decision=decision,
            normalized=normalized,
            entities=entities,
            gate_outcome=outcome,
        )


def explain(outcome: RouteOutcome, settings: Settings) -> str:
    """把一次识别渲染成人类可读的多行说明（``/intent <文本>`` 用）。"""
    decision = outcome.decision
    lines: List[str] = []
    lines.append("输入: %s" % (decision.preview or "(空)"))
    lines.append("哈希: %s  文本长度: %d%s" % (
        decision.text_hash or "-",
        outcome.normalized.length,
        "（已截断）" if outcome.normalized.truncated else "",
    ))
    if decision.skipped:
        lines.append("结果: 跳过（%s）——追问/空输入不做识别" % decision.skipped)
        lines.append("耗时: %.2f ms" % decision.elapsed_ms)
        return "\n".join(lines)

    if outcome.entities:
        lines.append("实体: " + ", ".join(
            "%s=%s" % (key, value) for key, value in sorted(outcome.entities.values.items())
        ))
    lines.append("结果: 层级=%s 意图=%s 分数=%.2f 证据=%d %s" % (
        decision.tier_name(),
        decision.intent or "(不表态)",
        decision.score,
        decision.sources,
        ("规则=" + decision.rule_id) if decision.rule_id else "",
    ))
    if decision.candidates:
        lines.append("候选: " + " | ".join(
            "%s=%.2f" % (name, score) for name, score in decision.candidates
        ))
    if decision.evidence:
        lines.append("证据明细: " + "; ".join(decision.evidence))
    if decision.gate_reason:
        lines.append("门控: %s%s" % (decision.gate_reason, "（被否定模式否决）" if decision.vetoed else ""))
    if decision.tier3_used:
        lines.append("第三层: 用时 %.0f ms%s" % (
            decision.tier3_ms, "（失败：%s）" % decision.tier3_error if decision.tier3_error else "",
        ))
    lines.append("阈值: min_score=%.2f margin=%.2f min_evidence=%d" % (
        settings.min_score, settings.margin, settings.min_evidence,
    ))
    lines.append("耗时: %.2f ms" % decision.elapsed_ms)
    return "\n".join(lines)
