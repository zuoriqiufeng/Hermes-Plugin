"""打分与三重门控测试：分数公式、阈值/差距/证据条数、以及「否决只取消候选、不毒化整条消息」。"""
from __future__ import annotations

from conftest import submodule


def _evidence(intent_id, *, keywords=(), bm25=0.0, entities=(), vetoes=()):
    scorer = submodule("scorer")
    return scorer.IntentEvidence(
        intent_id=intent_id,
        keyword_hits=list(keywords),
        bm25=bm25,
        entity_hits=list(entities),
        veto_hits=list(vetoes),
    )


def test_score_formula_and_clamping(settings):
    scorer = submodule("scorer")

    # 关键词权重和 2.0 = 饱和值 → 关键词证据满分 1.0；BM25 1.0；无实体无否决
    perfect = scorer.score_one(_evidence("a", keywords=[("x", 1.0), ("y", 1.0)], bm25=1.0), settings)
    assert perfect.keyword_score == 1.0
    assert perfect.score == 1.0                     # clamp 到 1，不溢出
    assert perfect.sources == 2

    # 同一个词条重复命中只计一次
    repeated = scorer.score_one(_evidence("a", keywords=[("x", 1.0), ("x", 1.0)]), settings)
    assert repeated.keyword_score == 0.5

    entity = scorer.score_one(_evidence("a", entities=["error_code"]), settings)
    assert entity.entity_bonus == 0.15
    capped = scorer.score_one(_evidence("a", entities=["error_code", "db_type", "ip"]), settings)
    assert capped.entity_bonus == 0.3               # 上限

    vetoed = scorer.score_one(_evidence("a", keywords=[("x", 1.0), ("y", 1.0)], vetoes=["别诊断"]), settings)
    assert vetoed.penalty == settings.veto_penalty
    assert vetoed.score < 1.0


def test_gate_accepts_only_with_three_conditions(settings):
    scorer = submodule("scorer")

    strong = scorer.score_all([
        _evidence("good", keywords=[("x", 1.0), ("y", 1.0)], bm25=1.0),
        _evidence("other", keywords=[("z", 0.5)]),
    ], settings)
    outcome = scorer.gate(strong, settings)
    assert outcome.accepted and outcome.intent_id == "good"
    assert outcome.reason == scorer.REASON_ACCEPTED
    assert outcome.sources == 2


def test_gate_rejects_below_min_score(settings):
    scorer = submodule("scorer")

    rows = scorer.score_all([_evidence("weak", keywords=[("x", 1.0)]), _evidence("noise")], settings)
    outcome = scorer.gate(rows, settings)
    assert not outcome.accepted
    assert outcome.reason == scorer.REASON_SCORE


def test_gate_rejects_single_evidence_source(settings):
    """只有 BM25（哪怕满分）也不算「敢下结论」——这是防乱指的关键一条。"""
    scorer = submodule("scorer")

    rows = scorer.score_all([_evidence("bm25_only", bm25=1.0)], settings)
    outcome = scorer.gate(rows, settings)
    assert not outcome.accepted
    # 分数 0.4（=0.4×1.0）先不过阈值；把阈值放低后必须由「证据不足」拦住
    loose = _settings_with(settings, min_score=0.1, margin=0.0, min_evidence=2)
    outcome2 = scorer.gate(scorer.score_all([_evidence("bm25_only", bm25=1.0)], loose), loose)
    assert not outcome2.accepted and outcome2.reason == scorer.REASON_EVIDENCE


def test_gate_rejects_ambiguous_margin(settings):
    scorer = submodule("scorer")

    rows = scorer.score_all([
        _evidence("a", keywords=[("x", 1.0), ("y", 1.0)], bm25=1.0),
        _evidence("b", keywords=[("x", 1.0), ("y", 1.0)], bm25=1.0),
    ], settings)
    outcome = scorer.gate(rows, settings)
    assert not outcome.accepted and outcome.reason == scorer.REASON_MARGIN


def test_veto_disqualifies_candidate_but_runner_up_competes(settings):
    """否决语义：被否决者退出竞争，第二名有机会上位（而不是整条消息作废）。"""
    scorer = submodule("scorer")

    rows = scorer.score_all([
        _evidence("top", keywords=[("a", 1.0), ("b", 1.0)], bm25=1.0, vetoes=["别诊断"]),
        _evidence("runner", keywords=[("c", 1.0), ("d", 1.0)], bm25=1.0),
    ], settings)
    outcome = scorer.gate(rows, settings)
    assert outcome.accepted and outcome.intent_id == "runner"
    assert outcome.vetoed_ids == ["top"]


def test_all_candidates_vetoed_reports_vetoed(settings):
    scorer = submodule("scorer")

    rows = scorer.score_all([
        _evidence("only", keywords=[("a", 1.0)], vetoes=["<entity:error_code>"]),
    ], settings)
    outcome = scorer.gate(rows, settings)
    assert not outcome.accepted
    assert outcome.reason == scorer.REASON_VETOED
    assert outcome.vetoed_ids == ["only"]


def test_no_evidence_at_all(settings):
    scorer = submodule("scorer")

    outcome = scorer.gate([], settings)
    assert not outcome.accepted and outcome.reason == scorer.REASON_NO_EVIDENCE
    outcome2 = scorer.gate(scorer.score_all([_evidence("x")], settings), settings)
    assert not outcome2.accepted and outcome2.reason == scorer.REASON_NO_EVIDENCE


def _settings_with(settings, **changes):
    from dataclasses import replace
    return replace(settings, **changes)
