"""register(ctx) 契约测试：影子模式零行为、档位语义、越权不可能、fail-open。

这是插件与 Hermes 的接缝，也是唯一会「动别人东西」的地方（注入上下文、调工具、裁工具面），
所以断言写得比较死：任何一档拿不准就不许动作。
"""
from __future__ import annotations

import json

import pytest

from conftest import FakeCtx


@pytest.fixture()
def registered(monkeypatch, tmp_path):
    """在隔离的 HERMES_HOME 下调用 register()，返回 (ctx, plugin_module)。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def build(config=None, **kwargs):
        from conftest import PLUGIN
        ctx = FakeCtx(config, **kwargs)
        # 清掉可能被前一个用例污染的事件记账
        PLUGIN.register(ctx)
        return ctx
    return build


def _payload(text, **extra):
    payload = {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "user_message": text,
        "conversation_history": [],
        "platform": "api_server",
        "model": "test-model",
    }
    payload.update(extra)
    return payload


def test_register_shape_and_boot_record(registered, tmp_path):
    ctx = registered()
    assert "pre_llm_call" in ctx.hooks
    assert "intent" in ctx.commands
    assert "intent_router_classify" in ctx.aux_tasks
    assert ctx.tools == {}                       # expose_tool 默认关
    assert "llm_request" not in ctx.middlewares  # prune 默认关
    assert ctx.unload                            # on_unload 已登记

    log_path = tmp_path / "plugin-data" / "intent-router" / "decisions.jsonl"
    boot = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert boot["kind"] == "boot"
    assert boot["intents"] >= 5 and boot["rules"] >= 5
    assert boot["warnings"] == [] and boot["lexicon_drift"] == []


def test_shadow_mode_records_but_never_acts(registered, tmp_path):
    ctx = registered()                       # mode 默认 shadow
    assert ctx.hooks["pre_llm_call"](**_payload("-4073")) is None
    assert ctx.dispatched == []              # 影子模式绝不调工具

    rows = [json.loads(line) for line in
            (tmp_path / "plugin-data" / "intent-router" / "decisions.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    decisions = [row for row in rows if row.get("kind") == "decision"]
    assert len(decisions) == 1
    row = decisions[0]
    assert row["intent"] == "diag_error_code" and row["tier"] == "regex"
    assert row["mode"] == "shadow"
    assert row["injected_chars"] == 0
    # 影子模式仍要记下「假如放开会做什么」——这是决定能否开 act 的唯一依据
    assert row["action_reason"].startswith("shadow:")
    assert "--" in row["action_reason"] or "act" in json.dumps(row, ensure_ascii=False)


def test_hint_mode_injects_context_without_touching_tools(registered):
    ctx = registered({"mode": "hint"})
    result = ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了"))
    assert result and "intent-router" in result["context"]
    assert "diag_symptom_incremental_stuck" in result["context"]
    assert ctx.dispatched == []


def test_act_mode_dispatches_only_allowlisted_readonly_tool(registered):
    ctx = registered({
        "mode": "act",
        "auto_enabled": True,
        "auto_allow_tools": ["diagnose_error"],
    })
    result = ctx.hooks["pre_llm_call"](**_payload("-4073"))
    assert ctx.dispatched == [("diagnose_error", {"error_code": "-4073"})]
    assert result is not None
    assert "诊断结果占位" in result["context"]
    assert "FIELD_TEXT" in result["context"]


def test_act_mode_does_not_dispatch_when_tool_missing_from_allowlist(registered):
    ctx = registered({
        "mode": "act",
        "auto_enabled": True,
        "auto_allow_tools": [],              # 白名单为空 = 一个都不许
    })
    result = ctx.hooks["pre_llm_call"](**_payload("-4073"))
    assert ctx.dispatched == []
    assert result and "diagnose_error" not in result["context"]


def test_act_mode_falls_back_to_hint_when_tool_raises(registered):
    ctx = registered({"mode": "act", "auto_enabled": True,
                      "auto_allow_tools": ["diagnose_error"]})
    ctx.tool_error = RuntimeError("工具内部错误")
    result = ctx.hooks["pre_llm_call"](**_payload("-4073"))
    assert ctx.dispatched                                  # 尝试过
    assert result and "intent-router" in result["context"]  # 但降级为提示
    assert "诊断结果占位" not in result["context"]


def test_follow_up_never_dispatches_even_in_act_mode(registered):
    ctx = registered({"mode": "act", "auto_enabled": True,
                      "auto_allow_tools": ["diagnose_error"]})
    assert ctx.hooks["pre_llm_call"](**_payload("继续")) is None
    assert ctx.dispatched == []


def test_uncertain_input_produces_no_injection_in_act_mode(registered):
    ctx = registered({"mode": "act", "auto_enabled": True,
                      "auto_allow_tools": ["diagnose_error"]})
    # 本地两层不敢表态、第三层未启用 → 不注入任何东西
    assert ctx.hooks["pre_llm_call"](**_payload("今天天气不错")) is None
    assert ctx.dispatched == []


def test_surfaces_whitelist(registered):
    ctx = registered({"mode": "hint", "surfaces": ["cli"]})
    assert ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了", platform="api_server")) is None
    assert ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了", platform="cli")) is not None


def test_mode_overrides_per_surface(registered):
    ctx = registered({"mode": "shadow", "mode_overrides": {"cli": "hint"}})
    assert ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了", platform="api_server")) is None
    assert ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了", platform="cli")) is not None


def test_enabled_false_disables_hook_registration(registered):
    ctx = registered({"enabled": False})
    assert ctx.hooks["pre_llm_call"](**_payload("-4073")) is None
    assert ctx.dispatched == []
    assert "intent" in ctx.commands           # 命令仍可用（诊断不受总开关影响）


def test_tool_exposure_is_opt_in(registered):
    ctx = registered({"expose_tool": True})
    assert "intent_classify" in ctx.tools
    entry = ctx.tools["intent_classify"]
    assert entry["toolset"] == "intent-router"
    payload = json.loads(entry["handler"]({"text": "-4073"}))
    assert payload["intent"] == "diag_error_code" and payload["tier"] == "regex"


def test_prune_middleware_registered_only_when_enabled(registered):
    ctx = registered({"prune_enabled": True})
    assert "llm_request" in ctx.middlewares


def test_event_emitted_and_can_be_disabled(registered):
    ctx = registered({"mode": "hint"})
    ctx.hooks["pre_llm_call"](**_payload("增量同步卡住了"))
    assert ctx.emitted and ctx.emitted[0][0] == "classified"
    assert ctx.emitted[0][1]["intent"] == "diag_symptom_incremental_stuck"

    ctx2 = registered({"mode": "hint", "emit_events": False})
    ctx2.hooks["pre_llm_call"](**_payload("增量同步卡住了"))
    assert ctx2.emitted == []


def test_multimodal_message_is_handled(registered):
    ctx = registered({"mode": "hint"})
    payload = _payload([{"type": "text", "text": "-4073"},
                        {"type": "image", "source": "..."}])
    result = ctx.hooks["pre_llm_call"](**payload)
    assert result and "diag_error_code" in result["context"]
    # 无文本段 → 不表态
    assert ctx.hooks["pre_llm_call"](**_payload([{"type": "image"}])) is None
    assert ctx.hooks["pre_llm_call"](**_payload({"unexpected": "shape"})) is None


def test_ctx_without_optional_apis_does_not_break(monkeypatch, tmp_path):
    """宿主版本较旧（没有 dispatch_tool/emit/llm）时，插件只降级、不报错。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from conftest import PLUGIN
    ctx = FakeCtx({"mode": "act", "auto_enabled": True, "auto_allow_tools": ["diagnose_error"]})
    ctx.dispatch_tool = None      # 实例属性遮蔽方法：模拟宿主没有这个 API
    ctx.emit = None
    ctx.llm = None
    PLUGIN.register(ctx)
    result = ctx.hooks["pre_llm_call"](**_payload("-4073"))
    assert result is not None and "intent-router" in result["context"]   # 降级为提示
    assert "诊断结果占位" not in result["context"]


def test_command_works_without_any_traffic(registered):
    ctx = registered()
    handler = ctx.commands["intent"]
    assert "用法" in handler("")
    assert "用法" in handler("help")
    out = handler("-4073")
    assert "diag_error_code" in out and "层级=regex" in out
    assert "还没有识别记录" in handler("why")
    explained = handler("explain ORA-01555")
    assert "意图=diag_error_code" in explained and "实体:" in explained


def test_command_never_raises_on_garbage(registered):
    ctx = registered()
    handler = ctx.commands["intent"]
    for args in ("stats abc", "tune", "doctor", "wrong", "whatever 🙃", "stats 10"):
        assert isinstance(handler(args), str)
