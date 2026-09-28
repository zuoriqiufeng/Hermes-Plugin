"""hooks.py — kanban 生命周期 hook → 跨进程推进队列

红线：kanban hook 全是 observer（返回值被丢弃），且 fired 在调用 complete 的
进程内（worker / CLI / gateway 均可能）——所以回调只做一件事：写 runstore
SQLite 队列秒回。求值/建卡全部由 advancer 线程消化。
任何异常只记日志（fail-open：DAG 引擎故障不得拖垮主对话/worker）。
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-dag.hooks")


def make_hooks(store):
    """工厂：返回 (completed_cb, blocked_cb, dispatch_tick_cb)。"""

    def on_kanban_task_completed(task_id=None, **kwargs):
        try:
            store.enqueue("kanban_task_completed", task_id,
                          board=kwargs.get("board"))
        except Exception:
            log.exception("dag hook enqueue failed (completed %s)", task_id)

    def on_kanban_task_blocked(task_id=None, reason=None, **kwargs):
        try:
            store.enqueue("kanban_task_blocked", task_id,
                          board=kwargs.get("board"),
                          payload={"reason": reason})
        except Exception:
            log.exception("dag hook enqueue failed (blocked %s)", task_id)

    def on_kanban_dispatch_tick(**kwargs):
        # 对账由 advancer 线程节流执行（reconcile_interval），这里不需要做事。
        # 保留注册以便未来需要 gateway 侧感知 tick。
        return None

    return on_kanban_task_completed, on_kanban_task_blocked, on_kanban_dispatch_tick
