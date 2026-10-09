"""第 2 层算法测试：Aho-Corasick 正确性（对暴力匹配逐例对齐）、BM25 单调性与归一化、分词。"""
from __future__ import annotations

import random

from conftest import submodule


# ── Aho-Corasick ────────────────────────────────────────────────────────


def brute_force(patterns, text):
    """暴力基准：每个模式的所有出现位置。"""
    found = set()
    for index, pattern in enumerate(patterns):
        if not pattern:
            continue
        start = text.find(pattern)
        while start != -1:
            found.add((index, start, start + len(pattern)))
            start = text.find(pattern, start + 1)
    return found


def test_ac_matches_brute_force_random():
    """固定种子随机用例：AC 必须与暴力匹配**完全一致**（不多不少）。"""
    aho = submodule("aho")
    rng = random.Random(20261008)
    alphabet = "abc中文字符"

    for _ in range(120):
        patterns = set()
        for _ in range(rng.randint(1, 12)):
            length = rng.randint(1, 5)
            patterns.add("".join(rng.choice(alphabet) for _ in range(length)))
        patterns = sorted(patterns)
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        automaton = aho.Automaton(patterns)
        assert set(automaton.find(text)) == brute_force(patterns, text), (patterns, text)


def test_ac_skips_empty_patterns_and_keeps_index_alignment():
    """空模式不占索引：索引 i 对应「过滤空串后」的第 i 个模式。"""
    aho = submodule("aho")

    automaton = aho.Automaton(["", "ab", "", "cd"])
    assert automaton.pattern_count == 2
    assert set(automaton.find("abcd")) == {(0, 0, 2), (1, 2, 4)}


def test_ac_overlapping_and_output_inheritance():
    """输出继承：'he'/'she'/'hers' 这类经典用例，重叠命中都要在。"""
    aho = submodule("aho")

    patterns = ["he", "she", "hers", "his"]
    automaton = aho.Automaton(patterns)
    assert set(automaton.find("ushers")) == brute_force(patterns, "ushers")
    assert automaton.has_match("ushers")
    assert not automaton.has_match("xyz")


def test_ac_handles_large_dictionary_quickly():
    """上千词条也不该退化（这是选 AC 而不是 N 条正则的理由）。"""
    aho = submodule("aho")

    terms = sorted({"词条%04d" % index for index in range(2000)} | {"增量卡住", "连接超时"})
    automaton = aho.Automaton(terms)
    assert automaton.pattern_count == len(terms)
    text = "现场日志里出现增量卡住和连接超时，还有词条0007。"
    hits = automaton.find(text)
    matched = {terms[index] for index, _s, _e in hits}
    assert {"增量卡住", "连接超时", "词条0007"} <= matched


def test_distinct_patterns_counts_unique_positions():
    aho = submodule("aho")

    matches = [(0, 0, 2), (0, 0, 2), (0, 3, 5), (1, 0, 2)]
    assert aho.distinct_patterns(matches) == {0: 2, 1: 1}


# ── BM25 ────────────────────────────────────────────────────────────────


def test_tokenize_mixed_script():
    bm25 = submodule("bm25")

    assert bm25.tokenize("ora-01555") == ["ora-01555"]
    tokens = bm25.tokenize("增量卡住")
    assert tokens == ["增量", "量卡", "卡住"]
    mixed = bm25.tokenize("ORA-01555 报错")
    assert "ora-01555" in mixed and "报错" in mixed
    assert bm25.tokenize("") == []
    assert bm25.tokenize("的") == ["的"]          # 单字成串不硬凑 bigram


def test_bm25_prefers_the_matching_document():
    bm25 = submodule("bm25")

    docs = [
        ("stuck", "增量同步卡住了"),
        ("mismatch", "两端数据对不上"),
        ("perf", "同步变慢了"),
    ]
    index = bm25.BM25Index.build(docs)
    assert index.doc_count == 3
    top = max(index.score_by_id("数据对不上").items(), key=lambda item: item[1])
    assert top[0] == "mismatch"
    assert index.score_by_id("完全无关的一句话")["stuck"] == 0.0


def test_bm25_scores_are_normalized_and_monotone():
    bm25 = submodule("bm25")

    docs = [("a", "增量同步卡住了"), ("b", "两端数据对不上")]
    index = bm25.BM25Index.build(docs)
    normalized = bm25.normalize_scores(index.score_by_id("增量同步卡住了"))
    assert normalized["a"] == 1.0
    assert 0.0 <= normalized["b"] < 1.0
    # 归一化后上界恒为 1；全零输入不应除零
    assert all(value <= 1.0 for value in normalized.values())
    assert bm25.normalize_scores({"x": 0.0}) == {"x": 0.0}
    assert bm25.normalize_scores({}) == {}


def test_bm25_rare_term_outweighs_common_term():
    """区分性词的 idf 更高：'卡住' 只在 1 篇里出现，'同步' 到处都有。"""
    bm25 = submodule("bm25")

    docs = [
        ("stuck", "同步卡住"),
        ("other1", "同步变慢"),
        ("other2", "同步不一致"),
        ("other3", "同步延迟"),
    ]
    index = bm25.BM25Index.build(docs)
    assert index._idf["卡住"] > index._idf["同步"]
