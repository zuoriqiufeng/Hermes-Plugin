"""语料回归：本插件的底线是「不许错判」，所以断言写得比覆盖率更硬。

- 本地两层表态了就必须正确（precision 100%）；
- 标注为「不该表态」的样本，本地两层必须闭嘴；
- 覆盖率有下限（保证收益真实存在），但不追求 100%——剩下的残差本来就该交给第三层。
"""
from __future__ import annotations

from conftest import submodule

# 覆盖率下限：语料里的正样本至少这么大比例要在本地两层解决（当前实测 ≈0.98）
MIN_COVERAGE = 0.90


def test_corpus_loads_and_is_wellformed():
    corpus = submodule("corpus")
    cases, warnings = corpus.load_corpus()
    assert len(cases) >= 50, "语料太少，起不到回归作用"
    assert warnings == []
    assert any(case.expect is None for case in cases), "必须包含反例"
    assert any(case.max_tier == 1 for case in cases), "必须标注第一层样本"


def test_no_wrong_local_classification(router):
    """最硬的一条：本地两层一旦表态，必须与标注一致。"""
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    result = corpus.evaluate(router, cases)
    wrong = [(item.case.text, item.case.expect, item.intent, item.tier) for item in result.wrong]
    assert wrong == [], "本地两层出现错判（这是必须修的 bug）：%s" % wrong


def test_negative_cases_stay_silent(router):
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    result = corpus.evaluate(router, cases)
    false_positives = [(item.case.text, item.intent) for item in result.false_positives]
    assert false_positives == [], "反例被本地两层误判：%s" % false_positives


def test_tier1_precision_is_perfect(router):
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    result = corpus.evaluate(router, cases)
    assert result.tier1, "语料应覆盖第一层样本"
    assert result.tier1_precision == 1.0


def test_coverage_meets_floor(router):
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    result = corpus.evaluate(router, cases)
    assert result.coverage >= MIN_COVERAGE, (
        "本地覆盖率 %.4f 低于下限 %.2f；未解决的：%s" % (
            result.coverage, MIN_COVERAGE,
            [(item.case.text, item.gate_reason) for item in result.unresolved]),
    )


def test_declared_max_tier_is_respected(router):
    """标了 max_tier 的样本，若被解决就必须在那一层（或更浅）解决；未解决（tier 0）不算违规。"""
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    result = corpus.evaluate(router, cases)
    offenders = []
    for item in result.results:
        if item.case.max_tier is None or item.tier == 0:
            continue
        if item.tier > item.case.max_tier:
            offenders.append((item.case.text, item.case.max_tier, item.tier, item.gate_reason))
    assert offenders == [], offenders


def test_classification_is_deterministic(router):
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    first = corpus.evaluate(router, cases).to_dict()
    second = corpus.evaluate(router, cases).to_dict()
    assert first == second


def test_sweep_prefers_no_wrong_results(taxonomy, settings):
    """扫参排序：零错判/零误报的行必须排在前面，且当前阈值应落在最优档附近。"""
    corpus = submodule("corpus")
    cases, _ = corpus.load_corpus()
    rows = corpus.sweep(taxonomy, settings, cases)
    assert rows and rows[0]["wrong"] == 0 and rows[0]["false_positives"] == 0
    current = next(row for row in rows
                   if (row["min_score"], row["margin"], row["min_evidence"])
                   == (settings.min_score, settings.margin, settings.min_evidence))
    assert current["wrong"] == 0 and current["false_positives"] == 0
    assert current["coverage"] >= MIN_COVERAGE


def test_lexicon_snapshot_is_not_drifted():
    """词表快照与源 constants.py 的一致性（源不在本机时跳过，不阻塞测试）。"""
    import subprocess
    from pathlib import Path

    plugin_dir = Path(__file__).resolve().parents[1]
    script = plugin_dir / "tools" / "gen_lexicon.py"
    result = subprocess.run(["python3", str(script), "--check"],
                            capture_output=True, text=True)
    if result.returncode == 2 and "源文件不存在" in (result.stderr or ""):
        import pytest
        pytest.skip("本机没有 i2stream-bkn 源，跳过漂移检查")
    assert result.returncode == 0, (
        "词表快照与源不一致，请重跑 tools/gen_lexicon.py：%s%s" % (result.stdout, result.stderr)
    )
