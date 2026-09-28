"""hermes-dag — 插件级 DAG 工作流引擎（Kanban = 执行层，插件 = 定义层 + 数据流层）

register(plugin_api)：
  - 注册 dag_define / dag_explain / dag_run / dag_status 四工具（toolset=hermes-dag）
  - 注册 /dag 斜杠命令
  - 注册 kanban_task_completed / kanban_task_blocked / on_kanban_dispatch_tick hooks
    （hook 回调只写跨进程队列秒回；advancer daemon 线程消化）
  - judge_fn 用 plugin_api.llm.complete_structured 包装（inconclusive ≠ clean 口径）

fail-open：任何初始化/运行故障只降级（条件边不自动推进，卡片仍可人工操作），
绝不拖垮主对话。板名/线程开关经插件 settings（board / advancer_enabled）可调。
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-dag")

BOARD_DEFAULT = "i2stream-ops"


def register(plugin_api):
    # 延迟导入：插件加载失败时不能影响 Hermes 其他插件
    try:
        from .schemas import SCHEMAS
        from .tools import DagEngine, make_tool_handlers
        from .commands import make_dag_command
        from .hooks import make_hooks
        from .runtime import is_gateway_process
        from .verdict import BASE_INSTRUCTIONS, JUDGE_SCHEMA
    except Exception:
        log.exception("hermes-dag: import failed, plugin disabled (fail-open)")
        return

    board = plugin_api.get_config("board", BOARD_DEFAULT) or BOARD_DEFAULT
    advancer_enabled = plugin_api.get_config("advancer_enabled", True)
    # webhook/advancer 只属于长驻 gateway 进程：短命 CLI 进程持有端口会挡住
    # 真 gateway（实测 EADDRINUSE），且抢走队列事件后退出会丢事件
    is_gateway = is_gateway_process()

    def judge_fn(instructions: str, input_text: str) -> dict:
        result = plugin_api.llm.complete_structured(
            instructions=instructions,
            input=[{"type": "text", "text": input_text}],
            json_schema=JUDGE_SCHEMA["schema"],
            schema_name="dag_verdict",
        )
        return result.parsed

    try:
        engine = DagEngine(board=board, judge_fn=judge_fn,
                           advancer_enabled=bool(advancer_enabled) and is_gateway)
    except Exception:
        log.exception("hermes-dag: engine init failed (fail-open)")
        return

    # tools
    handlers = make_tool_handlers(engine)
    for schema in SCHEMAS:
        name = schema["name"]
        try:
            plugin_api.register_tool(
                toolset="hermes-dag",
                name=name,
                schema=schema,
                handler=handlers[name],
            )
        except Exception:
            log.exception("hermes-dag: register tool %s failed", name)

    # slash command
    try:
        plugin_api.register_command("dag", make_dag_command(engine),
                                    description="DAG 工作流：list/status/run/explain",
                                    args_hint="list | status [run_id] | run <name> [json] | explain <name>")
    except Exception:
        log.exception("hermes-dag: register /dag failed")

    # hooks（只入队，秒回）
    try:
        completed_cb, blocked_cb, tick_cb = make_hooks(engine.store)
        plugin_api.register_hook("kanban_task_completed", completed_cb)
        plugin_api.register_hook("kanban_task_blocked", blocked_cb)
        plugin_api.register_hook("on_kanban_dispatch_tick", tick_cb)
    except Exception:
        log.exception("hermes-dag: register hooks failed")

    # webhook（跨机任务触发：0.0.0.0 + 强制 token；仅 gateway 进程启动）
    try:
        if plugin_api.get_config("webhook_enabled", True) and is_gateway:
            from .webhook import start_webhook

            webhook = start_webhook(
                engine,
                host=plugin_api.get_config("webhook_host", "0.0.0.0") or "0.0.0.0",
                port=int(plugin_api.get_config("webhook_port", 8620) or 8620),
                token=plugin_api.get_config("webhook_token"),
            )
            if webhook:
                plugin_api.on_unload(webhook.stop)
    except Exception:
        log.exception("hermes-dag: webhook init failed (fail-open)")

    plugin_api.on_unload(engine.shutdown)
    log.info("hermes-dag: registered (board=%s, gateway=%s, advancer=%s)",
             board, is_gateway, bool(advancer_enabled) and is_gateway)
