"""thinking-language 插件级测试。

不依赖 hermes 运行时：用 importlib 从文件加载插件模块，伪造 ctx 与
hook payload，直接断言注入内容、注册行为与异常降级。

运行：python3 -m pytest tests/ -v
"""
import importlib.util
from pathlib import Path

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"


def load_plugin():
    """加载一份全新状态的插件模块（重置 _announced 等模块级状态）。"""
    spec = importlib.util.spec_from_file_location("thinking_language_under_test", PLUGIN_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_returns_context_rule():
    mod = load_plugin()
    result = mod.on_pre_llm_call(platform="api_server", session_id="api-x")
    assert isinstance(result, dict)
    assert result["context"] == mod.LANGUAGE_RULE
    assert "中文" in result["context"]
    # 规则应同时约束保留代码/术语原文，避免把代码也翻掉
    assert "代码" in result["context"]


def test_registers_pre_llm_call():
    mod = load_plugin()
    ctx = FakeCtx()
    mod.register(ctx)
    assert list(ctx.hooks) == ["pre_llm_call"]
    assert callable(ctx.hooks["pre_llm_call"])


def test_failure_degrades_to_none(monkeypatch):
    """任何异常（此处以首次诊断日志抛错为例）都降级为 None，不向外抛。"""
    mod = load_plugin()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod.logger, "info", boom)
    assert mod.on_pre_llm_call(platform="api_server") is None


def test_repeated_calls_always_inject():
    """注入不进会话历史，每一轮都必须重新返回规则。"""
    mod = load_plugin()
    for _ in range(3):
        assert mod.on_pre_llm_call(platform="api_server")["context"] == mod.LANGUAGE_RULE
