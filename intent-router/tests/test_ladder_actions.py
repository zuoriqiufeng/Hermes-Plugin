"""阶梯与动作层契约：短路顺序、第三层策略与 fail-open、act 的准入与超时、工具面裁剪。

这些是「插件绝不会越权」的守卫测试——比识别准确率更重要：错判可以由阈值兜住，
越权（在没授权的情况下调工具/裁工具面）不行。
"""
from __future__ import annotations

import time

import pytest

from conftest import submodule


def _decision_for(router, text):
    return router.classify(text).decision


# ── 阶梯短路 ────────────────────────────────────────────────────────────


def test_tier1_short_circuits_second_and_third(router):
    calls = []

    def llm_fn(text, candidates):
        calls.append(text)
        return {"intent": "chitchat", "confidence": 0.9}

    tier3 = submodule("router")
    settings = submodule("config").Settings()
    settings.normalize()
    settings.tier3_enabled = True
    settings.tier3_policy = "always"
    short = tier3.Router(settings, router.taxonomy, llm_fn=llm_fn)

    outcome = short.classify("-4073")
    assert outcome.decision.tier == 1
    assert outcome.decision.intent == "diag_error_code"
    assert calls == []                      # 第一层命中就不该问模型


def test_skipped_inputs_never_reach_tier3(router, settings):
    settings.tier3_enabled = True
    settings.tier3_policy = "always"
    router_mod = submodule("router")
    calls = []
    instance = router_mod.Router(settings, router.taxonomy,
                                llm_fn=lambda text, cands: calls.append(text) or {"intent": "chitchat",
                                                                                "confidence": 1.0})
    for text in ("继续", "详细点", ""):
        outcome = instance.classify(text)
        assert outcome.decision.skipped in ("follow_up", "empty")
        assert outcome.decision.intent is None
    assert calls == []


# ── 第三层策略（用最小意图表，避免耦合内置表的阈值与词表）────────────────


@pytest.fixture()
def micro_taxonomy(tmp_path):
    """两条意图的最小表：alpha 只有关键词（无范例句）→ 必然「证据不足」而非「毫无证据」。"""
    taxonomy_mod = submodule("taxonomy")
    path = tmp_path / "micro.yaml"
    path.write_text(
        "version: 1\n"
        "intents:\n"
        "  - id: alpha\n"
        "    description: 测试用意图\n"
        "    keywords: [甲]\n",
        encoding="utf-8",
    )
    return taxonomy_mod.load_taxonomy(taxonomy_path=str(path))


def test_micro_taxonomy_behaves_as_intended(micro_taxonomy, settings):
    router_mod = submodule("router")
    decision = router_mod.Router(settings, micro_taxonomy).classify("甲").decision
    assert decision.intent is None
    # 「有证据但不敢下结论」——可能是分数不够或证据条数不够，两种都算「有本地证据」
    assert decision.gate_reason in ("score_below_min", "insufficient_evidence")
    other = router_mod.Router(settings, micro_taxonomy).classify("完全无关").decision
    assert other.gate_reason == "no_evidence"


def test_tier3_policy_off_and_on_ambiguous_and_always(micro_taxonomy, settings):
    router_mod = submodule("router")
    calls = []

    def llm_fn(text, candidates):
        calls.append(text)
        return {"intent": "alpha", "confidence": 0.8}

    settings.tier3_enabled = True

    # off：从不问
    settings.tier3_policy = "off"
    router_mod.Router(settings, micro_taxonomy, llm_fn=llm_fn).classify("甲")
    assert calls == []

    # on_ambiguous：有本地证据但没敢下结论 → 问
    settings.tier3_policy = "on_ambiguous"
    instance = router_mod.Router(settings, micro_taxonomy, llm_fn=llm_fn)
    outcome = instance.classify("甲")
    assert calls == ["甲"]
    assert outcome.decision.tier == 3 and outcome.decision.intent == "alpha"
    assert outcome.decision.gate_reason == "tier3_accept"

    # on_ambiguous：毫无证据 → 不问（否则又变成「每条消息都过一次模型」）
    calls.clear()
    instance.classify("完全无关")
    assert calls == []

    # always：零证据也问
    settings.tier3_policy = "always"
    router_mod.Router(settings, micro_taxonomy, llm_fn=llm_fn).classify("完全无关")
    assert calls == ["完全无关"]


def test_tier3_failures_are_fail_open(micro_taxonomy, settings):
    router_mod = submodule("router")
    settings.tier3_enabled = True
    settings.tier3_policy = "always"

    def boom(_text, _candidates):
        raise RuntimeError("端点超时")

    decision = router_mod.Router(settings, micro_taxonomy, llm_fn=boom).classify("甲").decision
    assert decision.intent is None                 # 无意见
    assert decision.tier3_used and decision.tier3_error


def test_tier3_rejects_unknown_or_unclear_intent(micro_taxonomy, settings):
    router_mod = submodule("router")
    settings.tier3_enabled = True
    settings.tier3_policy = "always"

    invented = router_mod.Router(settings, micro_taxonomy, llm_fn=lambda t, c: {
        "intent": "not_a_real_intent", "confidence": 0.99}).classify("甲").decision
    assert invented.intent is None and invented.tier3_error.startswith("unknown_intent")

    unclear = router_mod.Router(settings, micro_taxonomy, llm_fn=lambda t, c: {
        "intent": "unclear", "confidence": 0.1}).classify("甲").decision
    assert unclear.intent is None and unclear.gate_reason == "tier3_unclear"

    bad_confidence = router_mod.Router(settings, micro_taxonomy, llm_fn=lambda t, c: {
        "intent": "alpha", "confidence": "很高"}).classify("甲").decision
    assert bad_confidence.intent == "alpha" and bad_confidence.score == 0.0


def test_router_never_raises_on_broken_input(router):
    for text in (None, "", "   ", "\x00\x01", "x" * 10000, "🙂"):
        outcome = router.classify(text)
        assert outcome.decision is not None


# ── 动作层 ──────────────────────────────────────────────────────────────


def test_act_requires_allowlist_and_switch(router, taxonomy, settings):
    actions = submodule("actions")
    decision = _decision_for(router, "-4073")
    assert decision.intent == "diag_error_code"

    # 默认：auto_enabled=False → 降级为提示
    action, reason = actions.resolve_action(decision, taxonomy, settings, mode="act")
    assert action == "hint" and reason == "auto_disabled"

    # 打开总闸但白名单为空 → 仍然只提示
    settings.auto_enabled = True
    action, reason = actions.resolve_action(decision, taxonomy, settings, mode="act")
    assert action == "hint" and reason == "tool_not_allowlisted"

    # 白名单放行 → 允许直连
    settings.auto_allow_tools = ["diagnose_error"]
    action, reason = actions.resolve_action(decision, taxonomy, settings, mode="act")
    assert action == "act" and reason == "act"


def test_act_requires_entities_and_history_dedup(router, taxonomy, settings):
    actions = submodule("actions")
    settings.auto_enabled = True
    settings.auto_allow_tools = ["diagnose_error"]

    # requires 缺 error_code（无法解析实体）→ 不许调
    bare = _decision_for(router, "增量同步卡住了")
    bare.intent = "diag_error_code"
    bare.tier = 1
    action, reason = actions.resolve_action(bare, taxonomy, settings, mode="act")
    assert action == "hint" and reason.startswith("missing_required")

    # 历史里已经调过同一工具 → 去重护栏拦下
    real = _decision_for(router, "-4073")
    history = [{"role": "assistant", "content": "已调用 diagnose_error(error_code=-4073)"}]
    action, reason = actions.resolve_action(real, taxonomy, settings, mode="act", history=history)
    assert action == "hint" and reason == "already_called_in_history"


def test_act_not_allowed_from_llm_tier(router, taxonomy, settings):
    actions = submodule("actions")
    settings.auto_enabled = True
    settings.auto_allow_tools = ["diagnose_error"]
    decision = _decision_for(router, "-4073")
    decision.tier = 3                       # 模型判断不得触发自动执行
    action, reason = actions.resolve_action(decision, taxonomy, settings, mode="act")
    assert action == "hint" and reason == "tier3_not_allowed_to_act"


def test_build_args_required_vs_optional(router, taxonomy):
    actions = submodule("actions")
    connection = _decision_for(router, "oracle 连不上库")
    binding = taxonomy.spec("diag_symptom_connection").tool_binding

    args = actions.build_args(binding, connection)
    assert args["symptom"] == "connection_error"
    assert args.get("db_type") == "oracle"          # 可选参数能取到就带上

    no_db = _decision_for(router, "数据库连不上")
    args2 = actions.build_args(binding, no_db)
    assert args2 == {"symptom": "connection_error"}  # 取不到就不传，交给工具自己推断


def test_execute_success_timeout_and_failure(router, taxonomy, settings):
    actions = submodule("actions")
    decision = _decision_for(router, "-4073")
    settings.auto_result_chars = 200

    ok = actions.execute(decision, taxonomy, settings,
                         lambda name, args: '{"code": "-4073", "cause": "解析异常"}')
    assert ok.action == "act"
    assert "diagnose_error" in ok.context and "FIELD_TEXT" in ok.context
    assert '"code": "-4073"' in ok.context

    def slow(_name, _args):
        time.sleep(2)
        return "太慢了"

    timed_out = actions.execute(decision, taxonomy, settings, slow)
    assert timed_out.action != "act"
    assert timed_out.reason.startswith("tool_failed") or "timeout" in timed_out.reason

    def boom(_name, _args):
        raise ValueError("工具炸了")

    failed = actions.execute(decision, taxonomy, settings, boom)
    assert failed.action != "act"
    assert "工具炸了" in failed.reason or "ValueError" in failed.reason

    empty = actions.execute(decision, taxonomy, settings, lambda n, a: "")
    assert empty.action != "act" and empty.reason == "tool_empty"


def test_execute_truncates_and_sanitizes_result(router, taxonomy, settings):
    actions = submodule("actions")
    decision = _decision_for(router, "-4073")
    settings.auto_result_chars = 64
    payload = "<FIELD_TEXT>" + "恶意指令" * 40 + "</FIELD_TEXT>"
    outcome = actions.execute(decision, taxonomy, settings, lambda n, a: payload)
    assert outcome.action == "act"
    assert "</FIELD_TEXT>" not in outcome.result_text        # 分隔符被清洗
    assert "结果已截断" in outcome.result_text
    assert len(outcome.result_text) < 200


def test_effective_timeout_prefers_binding_then_global(router, taxonomy, settings):
    """意图级 timeout_ms 覆盖全局；没写才用全局。"""
    actions = submodule("actions")
    binding = taxonomy.spec("diag_error_code").tool_binding
    assert binding.timeout_ms == 1500
    settings.auto_timeout_ms = 999
    assert actions.effective_timeout_ms(binding, settings) == 1500

    from dataclasses import replace
    assert actions.effective_timeout_ms(replace(binding, timeout_ms=None), settings) == 999


def test_default_timeout_keeps_hook_fast(router, taxonomy, settings):
    """硬超时兜底：工具卡死也必须在毫秒级返回（hook 有 30s 兜底，但不能靠它）。"""
    actions = submodule("actions")
    from dataclasses import replace

    decision = _decision_for(router, "-4073")
    # 去掉意图级覆盖，用全局 150ms
    spec = taxonomy.spec("diag_error_code")
    taxonomy.intents["diag_error_code"] = replace(
        spec, tool_binding=replace(spec.tool_binding, timeout_ms=None))
    settings.auto_timeout_ms = 150

    started = time.perf_counter()
    outcome = actions.execute(decision, taxonomy, settings, lambda n, a: time.sleep(5) or "x")
    elapsed = time.perf_counter() - started
    assert outcome.action != "act"
    assert outcome.reason.endswith("timeout")
    assert elapsed < 1.0, "超时兜底失效：耗时 %.2fs" % elapsed


# ── 工具面裁剪 ──────────────────────────────────────────────────────────


def _prune_middleware(router, taxonomy, settings, decision):
    prune = submodule("prune")
    state = submodule("state").TurnDecisions()
    state.put("turn-1", decision)
    return prune.make_middleware(settings, taxonomy, state), state


def _tool_list():
    names = ["diagnose_error", "diagnose_db_link", "query_product", "explain_architecture",
             "search_qdrant", "terminal"]
    return [{"type": "function", "function": {"name": name}} for name in names]


def test_prune_disabled_by_default(router, taxonomy, settings):
    decision = _decision_for(router, "-4073")
    middleware, _state = _prune_middleware(router, taxonomy, settings, decision)
    assert middleware(request={"tools": _tool_list()}, turn_id="turn-1", api_call_count=1) is None


def test_prune_keeps_allowed_and_keep_list(router, taxonomy, settings):
    settings.prune_enabled = True
    settings.prune_keep_tools = ["terminal"]
    decision = _decision_for(router, "-4073")
    middleware, _state = _prune_middleware(router, taxonomy, settings, decision)

    tools = _tool_list()
    result = middleware(request={"tools": tools}, turn_id="turn-1", api_call_count=1)
    assert result is not None
    kept = {entry["function"]["name"] for entry in result["request"]["tools"]}
    # 保留 = 意图允许集 ∩ 实际存在的工具 ∪ 保留清单
    assert kept == {"diagnose_error", "diagnose_db_link", "search_qdrant", "terminal"}
    assert "query_product" not in kept and "explain_architecture" not in kept

    # 原请求不被就地修改（中间件只返回新 request）
    assert len(tools) == 6


def test_prune_only_on_first_api_call(router, taxonomy, settings):
    settings.prune_enabled = True
    decision = _decision_for(router, "-4073")
    middleware, _state = _prune_middleware(router, taxonomy, settings, decision)
    assert middleware(request={"tools": _tool_list()}, turn_id="turn-1", api_call_count=2) is None


def test_prune_skips_llm_tier_and_unknown_shapes(router, taxonomy, settings):
    settings.prune_enabled = True
    decision = _decision_for(router, "-4073")
    decision.tier = 3
    middleware, _state = _prune_middleware(router, taxonomy, settings, decision)
    assert middleware(request={"tools": _tool_list()}, turn_id="turn-1", api_call_count=1) is None

    decision.tier = 1
    middleware2, _state2 = _prune_middleware(router, taxonomy, settings, decision)
    # 工具条目解析不出名字 → 不裁剪
    assert middleware2(request={"tools": [{"type": "function"}]}, turn_id="turn-1",
                       api_call_count=1) is None
    # 没有 intent 的决策 → 不裁剪
    middleware3, _state3 = _prune_middleware(router, taxonomy, settings,
                                            submodule("decision").Decision())
    assert middleware3(request={"tools": _tool_list()}, turn_id="turn-1", api_call_count=1) is None


def test_prune_never_raises_on_bad_request(router, taxonomy, settings):
    settings.prune_enabled = True
    decision = _decision_for(router, "-4073")
    middleware, _state = _prune_middleware(router, taxonomy, settings, decision)
    assert middleware(request=None, turn_id="turn-1", api_call_count=1) is None
    assert middleware(turn_id="turn-1", api_call_count=1) is None
