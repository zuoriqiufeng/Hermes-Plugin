"""conftest.py — 测试夹具：不依赖 Hermes 运行时，用 importlib 以「包」的形式加载插件。

插件目录名带连字符（intent-router）不能直接 import，且插件内部用相对导入组织模块，
所以这里用 ``spec_from_file_location(..., submodule_search_locations=[...])`` 把它注册成
名为 ``intent_router`` 的包（与 reasoning-bridge / hermes-dag 的测试加载方式同源）。

``__init__.py`` 顶层只有 ``register()`` 定义与 ``__future__`` 导入，没有副作用，
因此可以安全执行。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_INIT = PLUGIN_DIR / "__init__.py"
PACKAGE_NAME = "intent_router_under_test"


def _load_package():
    """加载插件包（模块级执行一次：测试里可以直接 import 子模块）。"""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, PLUGIN_INIT, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_package()


def submodule(name: str):
    """取插件的子模块（如 ``taxonomy``）。"""
    return importlib.import_module("%s.%s" % (PACKAGE_NAME, name))


@pytest.fixture(scope="session")
def plugin_pkg():
    """插件包模块（模块级已加载，这里只做显式依赖声明）。"""
    return PLUGIN


@pytest.fixture(scope="session")
def taxonomy():
    loaded = submodule("taxonomy").load_taxonomy()
    assert loaded.intents, "内置意图表加载失败：%s" % loaded.warnings
    return loaded


@pytest.fixture()
def settings():
    config = submodule("config")
    value = config.Settings()
    value.normalize()
    return value


@pytest.fixture()
def router(taxonomy, settings):
    return submodule("router").Router(settings, taxonomy, llm_fn=None)


class FakeCtx:
    """伪造 Hermes 的 PluginContext（只实现被测代码真正用到的接口）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, *, llm: Any = None,
                 with_dispatch: bool = True) -> None:
        self._config = dict(config or {})
        self.hooks: Dict[str, Any] = {}
        self.middlewares: Dict[str, Any] = {}
        self.commands: Dict[str, Any] = {}
        self.tools: Dict[str, Any] = {}
        self.aux_tasks: Dict[str, Any] = {}
        self.unload: list = []
        self.emitted: list = []
        self.dispatched: list = []
        self.llm = llm
        self._with_dispatch = with_dispatch
        self.tool_result = '{"ok": true, "detail": "诊断结果占位"}'
        self.tool_error: Optional[Exception] = None

    # PluginContext 表面
    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_middleware(self, kind, callback):
        self.middlewares[kind] = callback

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        self.commands[name] = handler

    def register_tool(self, *, toolset, name, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_auxiliary_task(self, key, *, display_name="", description="", defaults=None):
        self.aux_tasks[key] = {"display_name": display_name, "defaults": defaults or {}}

    def on_unload(self, callback):
        self.unload.append(callback)

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))
        return 1

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.dispatched.append((tool_name, args))
        if self.tool_error is not None:
            raise self.tool_error
        return self.tool_result


@pytest.fixture()
def fake_ctx_factory():
    def build(config=None, **kwargs):
        return FakeCtx(config, **kwargs)
    return build
