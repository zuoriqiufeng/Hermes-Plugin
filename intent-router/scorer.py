"""scorer.py — 第 2 层的打分与三重门控（决定「敢不敢下结论」）。

分数：

    score = w_kw·关键词证据 + w_bm25·BM25 + 实体加成 − 否定惩罚

- **关键词证据**：命中的词条权重和除以饱和值（``kw_saturation``），上限 1.0。
  同名迹只计一次，避免长词表堆分。
- **BM25**：已按最大值归一到 [0,1]（见 bm25.normalize_scores）。
- **实体加成**：意图声明了 ``expects_entities`` 且消息里真有时，每类 +0.15（上限 +0.3）。
- **否定惩罚**：命中 ``veto_patterns`` 直接扣 ``veto_penalty``，并在门控里一票否决。

三重门控（缺一不可，全部通过才表态）：

1. ``score >= min_score``          —— 分数够高
2. ``top1 - top2 >= margin``       —— 没有势均力敌的第二名（歧义就闭嘴）
3. ``sources >= min_evidence``     —— 至少两条**独立**证据（关键词/BM25/实体）

第 3 条是这套设计的关键：单靠一个关键词命中就下结论，正是「意图识别乱指挥」的
主要来源；要求两路独立证据互相印证，才能把「高置信」这三个字落到实处。
门控失败会带回具体原因（``score_below_min`` / ``margin_too_small`` /
``insufficient_evidence`` / ``vetoed``），这是调参时唯一有价值的线索。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Settings

ENTITY_BONUS_PER = 0.15
ENTITY_BONUS_CAP = 0.3

REASON_NO_EVIDENCE = "no_evidence"
REASON_VETOED = "vetoed"
REASON_SCORE = "score_below_min"
REASON_EVIDENCE = "insufficient_evidence"
REASON_MARGIN = "margin_too_small"
REASON_ACCEPTED = "accepted"


@dataclass
class IntentEvidence:
    """某个意图在当前输入上积累到的原始证据（由 router 采集）。"""

    intent_id: str
    keyword_hits: List[Tuple[str, float]] = field(default_factory=list)
    bm25: float = 0.0
    entity_hits: List[str] = field(default_factory=list)
    veto_hits: List[str] = field(default_factory=list)


@dataclass
class ScoreBreakdown:
    intent_id: str
    score: float
    keyword_score: float
    bm25_score: float
    entity_bonus: float
    penalty: float
    sources: int
    evidence: List[str] = field(default_factory=list)
    vetoed: bool = False


@dataclass
class GateOutcome:
    accepted: bool
    reason: str
    intent_id: Optional[str] = None
    score: float = 0.0
    margin: float = 0.0
    sources: int = 0
    breakdowns: List[ScoreBreakdown] = field(default_factory=list)
    vetoed_ids: List[str] = field(default_factory=list)

    @property
    def runner_up(self) -> Optional[str]:
        return self.breakdowns[1].intent_id if len(self.breakdowns) > 1 else None


def score_one(evidence: IntentEvidence, settings: Settings) -> ScoreBreakdown:
    """把单个意图的证据折算成分数（纯函数，便于单测与调参）。"""
    seen: Dict[str, float] = {}
    for term, weight in evidence.keyword_hits:
        if term and weight > seen.get(term, 0.0):
            seen[term] = weight
    keyword_raw = sum(seen.values())
    saturation = settings.kw_saturation if settings.kw_saturation > 0 else 1.0
    keyword_score = min(1.0, keyword_raw / saturation)

    bm25_score = max(0.0, min(1.0, evidence.bm25))
    entity_bonus = min(ENTITY_BONUS_CAP, ENTITY_BONUS_PER * len(evidence.entity_hits))
    penalty = settings.veto_penalty if evidence.veto_hits else 0.0

    score = (
        settings.weight_keyword * keyword_score
        + settings.weight_bm25 * bm25_score
        + entity_bonus
        - penalty
    )
    score = max(0.0, min(1.0, score))

    sources = 0
    if keyword_score > 0.0:
        sources += 1
    if bm25_score >= settings.bm25_evidence_min:
        sources += 1
    if evidence.entity_hits:
        sources += 1

    details: List[str] = []
    if seen:
        details.append("kw=" + ",".join(sorted(seen.keys())))
    if bm25_score > 0.0:
        details.append("bm25=%.2f" % bm25_score)
    if evidence.entity_hits:
        details.append("ent=" + ",".join(sorted(evidence.entity_hits)))
    if evidence.veto_hits:
        details.append("veto=" + ",".join(sorted(evidence.veto_hits)))

    return ScoreBreakdown(
        intent_id=evidence.intent_id,
        score=round(score, 4),
        keyword_score=round(keyword_score, 4),
        bm25_score=round(bm25_score, 4),
        entity_bonus=round(entity_bonus, 4),
        penalty=round(penalty, 4),
        sources=sources,
        evidence=details,
        vetoed=bool(evidence.veto_hits),
    )


def score_all(evidences: Sequence[IntentEvidence], settings: Settings) -> List[ScoreBreakdown]:
    """全部意图打分，按分数降序（同分按意图 id 稳定排序）。"""
    breakdowns = [score_one(evidence, settings) for evidence in evidences]
    breakdowns.sort(key=lambda item: (-item.score, item.intent_id))
    return breakdowns


def gate(breakdowns: Sequence[ScoreBreakdown], settings: Settings) -> GateOutcome:
    """三重门控。任何一项不过就不表态，并带回原因。

    ``veto_patterns`` 的语义是「**这个意图不成立**」，不是「整条消息不作数」：被否决的
    候选直接退出竞争，其余候选照常参与门控（第二名有机会上位）。当所有候选都被否决、
    或否决后已无有效证据时，原因记为 ``vetoed``——它与 ``no_evidence`` 区别对待，
    因此 ``tier3_policy: on_ambiguous`` 仍会问一次模型（否决只说明「不是 A」，不说明「不是 B」）。
    """
    all_items = list(breakdowns)
    live = [item for item in all_items if item.score > 0.0 or item.vetoed]
    if not live:
        return GateOutcome(accepted=False, reason=REASON_NO_EVIDENCE, breakdowns=all_items)

    vetoed_ids = [item.intent_id for item in live if item.vetoed]
    survivors = [item for item in live if not item.vetoed]
    if not survivors:
        top = live[0]
        return GateOutcome(False, REASON_VETOED, top.intent_id, top.score, 0.0, top.sources,
                           all_items, vetoed_ids)

    top = survivors[0]
    runner_score = survivors[1].score if len(survivors) > 1 else 0.0
    margin = round(top.score - runner_score, 4)

    if top.score < settings.min_score:
        return GateOutcome(False, REASON_SCORE, top.intent_id, top.score, margin,
                           top.sources, all_items, vetoed_ids)
    if top.sources < settings.min_evidence:
        return GateOutcome(False, REASON_EVIDENCE, top.intent_id, top.score, margin,
                           top.sources, all_items, vetoed_ids)
    if margin < settings.margin:
        return GateOutcome(False, REASON_MARGIN, top.intent_id, top.score, margin,
                           top.sources, all_items, vetoed_ids)
    return GateOutcome(True, REASON_ACCEPTED, top.intent_id, top.score, margin,
                       top.sources, all_items, vetoed_ids)
