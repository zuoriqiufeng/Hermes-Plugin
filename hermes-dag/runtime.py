"""runtime.py — 进程角色判定

为什么需要：插件 register() 会在**每个加载插件的 hermes 进程**里执行——包括
`hermes gateway restart` 客户端、`hermes chat -q` 会话、config 命令等短命进程。
webhook 与 advancer 只应属于**长驻 gateway 进程**：

- webhook：短命进程若抢先绑定端口（实测：restart 客户端抢占 8620，导致新 gateway
  的 webhook 以 EADDRINUSE 失败且在客户端退出后无人监听）；
- advancer：短命进程从跨进程队列取走事件（标记 consumed）后若退出，事件即丢失
  （虽有 60s reconcile 兜底，但属无谓损耗）。

判定用 argv 连续对 `("gateway", "run")`——gateway 由 systemd 以
`python -m hermes_cli.main gateway run` 启动；`gateway restart|stop|start` 客户端
的 argv 是 ("gateway", "restart") 等，不会误判。环境变量（HERMES_SUPERVISED_CHILD）
语义较泛，不作主依据。
"""

from __future__ import annotations

import sys


def is_gateway_process(argv=None) -> bool:
    """当前进程是否为长驻 gateway（argv 中含连续对 gateway, run）。"""
    args = list(sys.argv if argv is None else argv)
    for i in range(len(args) - 1):
        if args[i] == "gateway" and args[i + 1] == "run":
            return True
    return False
