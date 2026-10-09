"""corpus.py — 标注语料的加载与评估（单测与 ``/intent tune`` 共用）。

两个用途：

1. **回归断言**：语料里「不许错判」是硬约束——本地两层一旦表态就必须正确，
   错了就是 bug（这正是本插件存在的意义：不确定可以闭嘴，但不能乱指）。
2. **阈值扫参**：``/intent tune`` 在语料上跑网格，输出「覆盖率 vs 精度」，
   用来把 ``min_score`` / ``margin`` / ``min_evidence`` 调到位，而不是凭感觉设。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .taxonomy import _load_mapping

log = logging.getLogger("intent-router")

PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS = PLUGIN_DIR / "data" / "corpus" / "regression.yaml"
NONE_TOKEN = "none"


@dataclass(frozen=True)
class CorpusCase:
    text: str
    expect: Optional[str]  # None = 不该表态
    max_tier: Optional[int] = None
    note: str = ""


@dataclass
class CaseResult:
    case: CorpusCase
    intent: Optional[str]
    tier: int
    score: float
    gate_reason: str
    skipped: str = ""

    @property
    def correct(self) -> bool:
        return self.intent == self.case.expect

    @property
    def wrong(self) -> bool:
        """本地两层给出结论但与标注不符——最严重的一类。"""
        return self.tier in (1, 2) and self.intent is not None and self.intent != self.case.expect

    @property
    def resolved_locally(self) -> bool:
        return self.tier in (1, 2) and self.intent is not None


@dataclass
class EvalResult:
    results: List[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def wrong(self) -> List[CaseResult]:
        return [item for item in self.results if item.wrong]

    @property
    def positive(self) -> List[CaseResult]:
        return [item for item in self.results if item.case.expect is not None]

    @property
    def negative(self) -> List[CaseResult]:
        return [item for item in self.results if item.case.expect is None]

    @property
    def tier1(self) -> List[CaseResult]:
        return [item for item in self.results if item.tier == 1]

    @property
    def local_resolved(self) -> List[CaseResult]:
        return [item for item in self.results if item.resolved_locally and item.case.expect is not None]

    @property
    def coverage(self) -> float:
        """正样本里被本地两层正确解决的比例。"""
        pool = self.positive
        if not pool:
            return 0.0
        return round(sum(1 for item in pool if item.correct and item.resolved_locally) / len(pool), 4)

    @property
    def tier1_precision(self) -> float:
        pool = self.tier1
        if not pool:
            return 1.0
        return round(sum(1 for item in pool if item.correct) / len(pool), 4)

    @property
    def local_precision(self) -> float:
        """本地两层的精度：表态了就必须对（分母是「本地表态过」的样本）。"""
        pool = [item for item in self.results if item.resolved_locally]
        if not pool:
            return 1.0
        return round(sum(1 for item in pool if item.correct) / len(pool), 4)

    @property
    def false_positives(self) -> List[CaseResult]:
        """标注为 none 却被本地表态的样本。"""
        return [item for item in self.negative if item.resolved_locally]

    @property
    def unresolved(self) -> List[CaseResult]:
        return [item for item in self.positive if not item.resolved_locally]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "positive": len(self.positive),
            "negative": len(self.negative),
            "coverage": self.coverage,
            "tier1_precision": self.tier1_precision,
            "local_precision": self.local_precision,
            "wrong": [(item.case.text, item.case.expect, item.intent, item.tier) for item in self.wrong],
            "false_positives": [(item.case.text, item.intent) for item in self.false_positives],
            "unresolved": [(item.case.text, item.case.expect, item.gate_reason) for item in self.unresolved],
        }


def load_corpus(path: Optional[str] = None) -> Tuple[List[CorpusCase], List[str]]:
    """读语料；坏行跳过并告警。返回 (cases, warnings)。"""
    target = Path(path) if path else DEFAULT_CORPUS
    doc = _load_mapping(target)
    if doc is None:
        return [], ["语料 %s 读不到或格式不对" % target]
    cases: List[CorpusCase] = []
    warnings: List[str] = []
    for index, raw in enumerate(doc.get("cases") or ()):
        if not isinstance(raw, Mapping):
            warnings.append("cases[%d] 不是映射，跳过" % index)
            continue
        text = str(raw.get("text") or "")
        if not text:
            warnings.append("cases[%d] 缺 text，跳过" % index)
            continue
        expect_raw = raw.get("expect")
        expect = None if expect_raw in (None, "", NONE_TOKEN) else str(expect_raw)
        max_tier: Optional[int] = None
        if raw.get("max_tier") is not None:
            try:
                max_tier = int(raw["max_tier"])
            except (TypeError, ValueError):
                warnings.append("cases[%d] max_tier 不是整数，忽略" % index)
        cases.append(CorpusCase(text=text, expect=expect, max_tier=max_tier,
                               note=str(raw.get("note") or "")))
    return cases, warnings


def evaluate(router: Any, cases: Sequence[CorpusCase]) -> EvalResult:
    """用给定 router 跑一遍语料。router 只需提供 ``classify(text) -> RouteOutcome``。"""
    result = EvalResult()
    for case in cases:
        try:
            outcome = router.classify(case.text)
            decision = outcome.decision
            result.results.append(CaseResult(
                case=case,
                intent=decision.intent,
                tier=decision.tier,
                score=decision.score,
                gate_reason=decision.gate_reason,
                skipped=decision.skipped,
            ))
        except Exception as exc:  # 评估不该因为单条样本崩掉
            log.warning("intent-router: 语料样本 %r 评估失败：%s", case.text, exc)
            result.results.append(CaseResult(case=case, intent=None, tier=0, score=0.0,
                                             gate_reason="exception"))
    return result


def sweep(
    taxonomy: Any,
    settings: Any,
    cases: Sequence[CorpusCase],
    *,
    min_scores: Iterable[float] = (0.55, 0.6, 0.62, 0.65, 0.7, 0.75),
    margins: Iterable[float] = (0.05, 0.1, 0.15, 0.2, 0.25),
    evidences: Iterable[int] = (1, 2, 3),
) -> List[Dict[str, Any]]:
    """在语料上扫阈值网格，返回按「无错判优先、覆盖率次之」排序的结果。

    第三层不参与（tune 关心的是本地两层的取舍）：构造 router 时不注入 llm_fn。
    """
    from .router import Router

    rows: List[Dict[str, Any]] = []
    for min_score in min_scores:
        for margin in margins:
            for evidence in evidences:
                trial = replace(settings, min_score=min_score, margin=margin,
                                min_evidence=evidence, tier3_enabled=False)
                outcome = evaluate(Router(trial, taxonomy, llm_fn=None), cases)
                rows.append({
                    "min_score": min_score,
                    "margin": margin,
                    "min_evidence": evidence,
                    "coverage": outcome.coverage,
                    "local_precision": outcome.local_precision,
                    "tier1_precision": outcome.tier1_precision,
                    "wrong": len(outcome.wrong),
                    "false_positives": len(outcome.false_positives),
                    "unresolved": len(outcome.unresolved),
                })
    # 排序：先看有没有错判/误报，再看覆盖率，最后**在同覆盖率下偏好更保守的阈值**——
    # 否则界面会把「调松阈值但一点覆盖都没换来」的行排在最前面，诱导运维白调。
    rows.sort(key=lambda row: (
        row["wrong"] + row["false_positives"],
        -row["coverage"],
        -row["min_score"], -row["margin"], -row["min_evidence"],
    ))
    return rows
