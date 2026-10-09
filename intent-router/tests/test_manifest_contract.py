"""清单契约测试：plugin.yaml 必须能被真实解析器接受、且与实际注册行为一致。

仓库约定「hook 名只能用 hermes VALID_HOOKS 中存在的」——这条只能靠清单测试兜住：
hook 名写错时 Hermes 只 warn 并静默存下（永不触发），是典型的「装了但没生效」故障。

校验顺序刻意与 ``hermes plugins validate`` 对齐：先能解析，再逐项对账。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_DIR / "plugin.yaml"

# hermes_cli.plugins.VALID_HOOKS 的快照（本插件只用其中一个）。
# 若本机装了她 Hermes 运行时，测试会优先用真实清单做校验（见 _valid_hooks）。
VALID_HOOKS_SNAPSHOT = {
    "pre_tool_call", "post_tool_call", "transform_terminal_output", "transform_tool_result",
    "transform_llm_output", "pre_llm_call", "post_llm_call",
    "on_stream_start", "on_stream_delta", "on_stream_end", "on_interim_message",
    "pre_verify", "pre_api_request", "post_api_request", "api_request_error",
    "transform_api_error_classification", "on_session_start", "on_session_end",
    "on_session_finalize", "on_session_reset", "on_skill_lifecycle",
    "subagent_start", "subagent_stop", "pre_gateway_dispatch",
    "pre_approval_request", "post_approval_response", "pre_transcription",
    "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
    "on_kanban_worker_spawned", "on_kanban_worker_exited", "on_kanban_worker_stale_claim",
    "on_kanban_task_updated", "on_kanban_dispatch_tick", "gateway_platform_event",
    "pre_command",
}

_CONFIG_TYPES = {"str", "string", "int", "integer", "float", "number", "bool", "boolean",
                 "list", "array", "dict", "object"}
_TYPE_SAMPLES = {
    "str": str, "string": str, "int": int, "integer": int, "float": float, "number": float,
    "bool": bool, "boolean": bool, "list": list, "array": list, "dict": dict, "object": dict,
}


def _valid_hooks():
    """优先用真实运行时的 VALID_HOOKS；跑不到（系统 python 没装 hermes）时用快照。"""
    try:
        import sys
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        from hermes_cli.plugins import VALID_HOOKS  # type: ignore
        return set(VALID_HOOKS), True
    except Exception:
        return set(VALID_HOOKS_SNAPSHOT), False


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_parses_and_has_required_fields(manifest):
    """plugin.yaml 必须是合法 YAML 且带 name/version/description（validate 的第一关）。

    这条测试是有来历的：描述里出现未加引号的 ``{cli: shadow}`` 会让整个清单解析失败，
    而插件注册代码本身完全正常——只测代码不测清单就会漏掉这类故障。
    """
    assert isinstance(manifest, dict)
    for field in ("name", "version", "description", "author", "kind"):
        assert manifest.get(field), "plugin.yaml 缺 %s" % field
    assert manifest["name"] == PLUGIN_DIR.name
    assert manifest["kind"] in {"standalone", "backend", "exclusive", "platform", "model-provider"}


def test_declared_hooks_are_real_and_match_register(manifest, monkeypatch, tmp_path):
    """声明的 hook 必须存在于 VALID_HOOKS，且 register() 真的注册了它。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from conftest import FakeCtx, PLUGIN

    hooks = manifest.get("provides_hooks") or []
    assert hooks, "本插件至少要用 pre_llm_call"
    valid, from_runtime = _valid_hooks()
    unknown = [hook for hook in hooks if hook not in valid]
    assert unknown == [], "声明了 VALID_HOOKS 里没有的 hook（Hermes 只会静默 warn）：%s" % unknown
    assert "pre_llm_call" in hooks

    ctx = FakeCtx()
    PLUGIN.register(ctx)
    for hook in hooks:
        assert hook in ctx.hooks, "声明了 %s 但 register() 没注册" % hook
    # 注册但不声明的 hook 也要能查出来（清单与实际对账）
    assert set(ctx.hooks) <= set(hooks), "注册了未声明的 hook：%s" % (set(ctx.hooks) - set(hooks))


def test_declared_tools_match_registration(manifest, monkeypatch, tmp_path):
    """provides_tools 只有真会注册时才允许声明（否则 validate 会报 declares-but-not-registered）。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from conftest import FakeCtx, PLUGIN

    declared = set(manifest.get("provides_tools") or [])
    default_ctx = FakeCtx()
    PLUGIN.register(default_ctx)
    assert declared == set(default_ctx.tools), (
        "默认配置下注册的工具（%s）与声明（%s）不一致" % (set(default_ctx.tools), declared))

    enabled_ctx = FakeCtx({"expose_tool": True})
    PLUGIN.register(enabled_ctx)
    assert "intent_classify" in enabled_ctx.tools


def test_config_schema_types_and_defaults(manifest):
    """config_schema 的 type 必须是受支持的类型，且 default 与 type 相符。

    类型写错时 Hermes 只 warn 并跳过该键，插件拿到的是默认值——静默的配置失效。
    """
    schema = manifest.get("config_schema") or {}
    assert schema, "配置项应写进 config_schema"
    for key, spec in schema.items():
        assert isinstance(spec, dict), key
        assert "description" in spec, "%s 缺 description" % key
        declared_type = spec.get("type", "str")
        assert declared_type in _CONFIG_TYPES, "%s: 未知类型 %r" % (key, declared_type)
        if "default" in spec:
            expected = _TYPE_SAMPLES[declared_type]
            assert isinstance(spec["default"], expected), (
                "%s: default %r 与 type %s 不符" % (key, spec["default"], declared_type))


def test_config_schema_covers_every_setting():
    """Settings 的每个字段都必须在清单里声明（否则运维无法从配置面调整）。"""
    from conftest import submodule

    config = submodule("config")
    from dataclasses import fields
    declared = set(submodule("config")._SCHEMA_TYPES)
    actual = {field.name for field in fields(config.Settings)}
    assert actual == declared, "Settings 字段与 _SCHEMA_TYPES 不一致：%s" % (
        actual ^ declared)


def test_defaults_in_manifest_match_code_defaults(manifest):
    """清单里的 default 必须与 dataclass 默认值一致（两处不一致会让行为无法解释）。"""
    from dataclasses import fields
    from conftest import submodule

    config = submodule("config")
    settings = config.Settings()
    schema = manifest["config_schema"]
    mismatched = []
    for field in fields(config.Settings):
        spec = schema.get(field.name)
        if not spec or "default" not in spec:
            continue
        if spec["default"] != getattr(settings, field.name):
            mismatched.append((field.name, spec["default"], getattr(settings, field.name)))
    assert mismatched == [], "清单默认值与代码默认值不一致：%s" % mismatched
