"""第一层（正则）与意图表加载的测试：编译校验、优先级、槽位、护栏、合并与覆盖。"""
from __future__ import annotations

import textwrap

import pytest

from conftest import submodule


def _normalized(text, max_chars=2000):
    return submodule("normalize").normalize(text, max_chars=max_chars)


# ── 规则编译与匹配 ──────────────────────────────────────────────────────


def test_compile_rules_validates_and_skips_bad_entries():
    regex_tier = submodule("regex_tier")

    raw = [
        {"id": "ok", "intent": "a", "pattern": "^x$", "priority": 10},
        {"id": "BAD ID", "intent": "a", "pattern": "^y$"},          # id 非法
        {"id": "no_intent", "pattern": "^z$"},                       # 缺 intent
        {"id": "broken", "intent": "a", "pattern": "^(unclosed"},    # 编译失败
        "not-a-mapping",                                             # 类型不对
        {"id": "ok", "intent": "b", "pattern": "^w$"},               # id 重复：后者生效
    ]
    rules, warnings = regex_tier.compile_rules(raw)
    ids = [rule.id for rule in rules]
    assert ids.count("ok") == 1
    assert rules[0].intent == "b"          # 重复 id 的后一条覆盖前一条
    assert "broken" not in ids and "BAD ID" not in ids
    assert any("编译失败" in warning for warning in warnings)
    assert any("非法" in warning for warning in warnings)
    assert len(warnings) >= 4


def test_compile_rules_priority_order_and_max_rules():
    regex_tier = submodule("regex_tier")

    raw = [
        {"id": "low", "intent": "a", "pattern": "^x$", "priority": 1},
        {"id": "high", "intent": "b", "pattern": "^x$", "priority": 99},
        {"id": "mid", "intent": "c", "pattern": "^x$", "priority": 50},
    ]
    rules, _ = regex_tier.compile_rules(raw)
    assert [rule.id for rule in rules] == ["high", "mid", "low"]

    capped, warnings = regex_tier.compile_rules(raw, max_rules=2)
    assert len(capped) == 2
    assert any("上限" in warning for warning in warnings)


def test_rule_matching_fullmatch_search_slots_and_target():
    regex_tier = submodule("regex_tier")

    raw = [
        {"id": "anchored", "intent": "code", "pattern": "^(?P<code>-?[0-9]{3,5})$",
         "target": "compact", "slots": {"code": "error_code"}},
        {"id": "loose", "intent": "grep", "pattern": "查一下", "match": "search"},
    ]
    rules, warnings = regex_tier.compile_rules(raw)
    assert not warnings

    hit = regex_tier.match_rules(rules, _normalized("-4073。"))
    # compact 会抹掉非字母数字字符（含符号），所以槽位里拿到的是 "4073"——
    # 这也是内置表刻意**不靠规则槽位传错误码**、而用实体抽取（会补负号）的原因。
    assert hit is not None and hit.intent == "code" and hit.slots == {"error_code": "4073"}

    assert regex_tier.match_rules(rules, _normalized("帮我查一下 -4073")).intent == "grep"
    # fullmatch 不放过更长的串（试例规则限定 3-5 位数字）
    assert regex_tier.match_rules(rules, _normalized("-407350")) is None
    assert regex_tier.match_rules(rules, _normalized("-40735")).intent == "code"


def test_rule_slots_reference_unknown_group_is_warned():
    regex_tier = submodule("regex_tier")

    rules, warnings = regex_tier.compile_rules([
        {"id": "r", "intent": "a", "pattern": "^(?P<code>x)$", "slots": {"oops": "error_code"}},
    ])
    assert any("不存在的命名组" in warning for warning in warnings)
    assert regex_tier.match_rules(rules, _normalized("x")).slots == {}


def test_redos_lint_flags_dangerous_shapes():
    regex_tier = submodule("regex_tier")

    assert regex_tier.lint_pattern("(a+)+$")
    assert regex_tier.lint_pattern(r"(x)\1")
    assert regex_tier.lint_pattern("^[-0-9]{3,5}$") == []

    _rules, warnings = regex_tier.compile_rules([
        {"id": "risky", "intent": "a", "pattern": "^(a+)+$"},
    ])
    assert any("回溯风险" in warning for warning in warnings)


# ── 追问 / 空输入护栏 ───────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "继续", "详细点", "然后呢", "为什么", "好的", "嗯", "谢谢", "再详细点",
    "还有呢", "接着说一下", "OK", "continue",
])
def test_follow_up_guard(text):
    regex_tier = submodule("regex_tier")

    assert regex_tier.match_guard(_normalized(text)) == "follow_up"


@pytest.mark.parametrize("text", [
    "", "   ", "增量同步卡住了", "继续处理一下增量同步卡住的问题怎么解决呢",
])
def test_guard_does_not_swallow_real_requests(text):
    regex_tier = submodule("regex_tier")

    reason = regex_tier.match_guard(_normalized(text))
    assert reason in (None, "empty")


def test_guard_can_be_disabled():
    regex_tier = submodule("regex_tier")

    assert regex_tier.match_guard(_normalized("继续"), continuation_guard=False) is None


# ── 意图表加载 ──────────────────────────────────────────────────────────


def test_builtin_taxonomy_is_consistent(taxonomy):
    """内置意图表必须自洽：规则指向存在的意图；act 意图有只读绑定与安全标记；
    绑定里引用的实体必须写进 requires（否则可能出现「参数取不到却仍然动手」）。"""
    import re

    assert taxonomy.intents
    for rule in taxonomy.rules:
        assert rule.intent in taxonomy.intents
    acts = [spec for spec in taxonomy.intents.values() if spec.action == "act"]
    assert acts, "内置表应至少有一个 act 意图（否则直连能力无从演示）"
    for spec in acts:
        binding = spec.tool_binding
        assert binding is not None, spec.id
        assert binding.safe_auto, spec.id
        for value in binding.args.values():
            for entity_name in re.findall(r"\{entities\.([a-z0-9_]+)\}", value):
                assert entity_name in spec.requires, (
                    "%s: 绑定引用了 entities.%s 但未写进 requires" % (spec.id, entity_name))
    # 自动机索引与词表键严格对齐（空串被过滤后仍一一对应）
    assert taxonomy.keyword_terms == sorted(term for term in taxonomy.keyword_index if term)


def test_taxonomy_warnings_empty_for_builtin(taxonomy):
    assert taxonomy.warnings == [], taxonomy.warnings
    assert taxonomy.lexicon_drift == [], taxonomy.lexicon_drift


def test_lexicon_snapshot_has_groups(taxonomy):
    assert taxonomy.lexicon_files, "内置表应引用词表快照"
    assert any(term for term in taxonomy.keyword_terms if term)
    # 生成器产出的组确实被展开进了词表（而不是留下 '@xxx' 字面量）
    assert not [term for term in taxonomy.keyword_terms if term.startswith("@")]


def test_taxonomy_override_merges_by_id(tmp_path):
    taxonomy_mod = submodule("taxonomy")

    override = tmp_path / "override.yaml"
    override.write_text(textwrap.dedent("""
        version: 1
        intents:
          - id: chitchat
            description: 覆盖后的寒暄
            action: none
            keywords: [寒暄]
          - id: brand_new
            description: 新增意图
            action: hint
            keywords: [新意图关键词]
            examples: ["新意图示例"]
    """), encoding="utf-8")

    loaded = taxonomy_mod.load_taxonomy(taxonomy_path="data/taxonomy/ops-diag.yaml",
                                        extra_paths=[str(override)])
    assert loaded.spec("chitchat").description == "覆盖后的寒暄"
    assert "brand_new" in loaded.intents
    assert any("被" in warning and "chitchat" in warning for warning in loaded.warnings)


def test_taxonomy_veto_entities_and_allowed_tools_parsed(taxonomy):
    chitchat = taxonomy.spec("chitchat")
    assert "error_code" in chitchat.veto_entities
    assert taxonomy.spec("ask_knowledge").allowed_tools
    binding = taxonomy.spec("diag_error_code").tool_binding
    assert binding.tool == "diagnose_error"
    assert binding.optional_args == {}
    symptom = taxonomy.spec("diag_symptom_connection").tool_binding
    assert symptom.args == {"symptom": "connection_error"}
    assert "db_type" in symptom.optional_args


def test_taxonomy_missing_lexicon_group_is_warned(tmp_path):
    taxonomy_mod = submodule("taxonomy")

    doc = tmp_path / "t.yaml"
    doc.write_text(textwrap.dedent("""
        version: 1
        intents:
          - id: x
            keywords: ["@nope:missing"]
            examples: ["x"]
    """), encoding="utf-8")
    loaded = taxonomy_mod.load_taxonomy(taxonomy_path=str(doc))
    assert any("词表组" in warning for warning in loaded.warnings)
    assert loaded.spec("x").keywords == ()       # 缺组只丢该条，不炸
