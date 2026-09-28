# Hermes-Plugin

Hermes 插件库：存放我们自研的 Hermes 用户插件（`~/.hermes/plugins/` 的源头仓库），
按「一个插件一个子目录」组织，后续会扩展加入更多插件。

所有插件共享同一套约束——**不改 Hermes 本体**（安装目录会被升级覆盖），
只通过官方插件机制（`plugin.yaml` + `register(ctx)` + hook）扩展能力；
任何插件故障都不得影响对话主流程。

## 目录结构

```
hermes-plugin/
├── README.md
├── reasoning-bridge/          # 深度思考过程展示（JSONL 文件桥）
│   ├── plugin.yaml            # 插件清单（name/version/hooks…）
│   ├── __init__.py            # register(ctx) + hook 实现
│   ├── README.md              # 插件级文档（协议/部署/验证）
│   └── tests/                 # 插件级 pytest
├── hermes-dag/                # Kanban 之上的 DAG 工作流引擎（编排/条件边/裁决）
│   ├── plugin.yaml            # 插件清单（provides_tools/hooks + config_schema）
│   ├── __init__.py            # register(ctx)：工具 + /dag + hooks + advancer + webhook
│   ├── *.py                   # dsl/evaluator/compiler/advancer/verdict/runstore…
│   ├── data/templates/        # 业务 DAG 模板（只读资产，随插件分发）
│   ├── docs/                  # 原理 / M0 验证 / 扩展路线 / 部署验收记录
│   └── tests/                 # 单测（Fake Kanban）+ 真 CLI 集成冒烟
└── <future-plugin>/           # 后续插件按同样约定落位
```

## 当前插件

| 插件 | 说明 | 状态 |
|------|------|------|
| [reasoning-bridge](./reasoning-bridge/) | 把模型 reasoning 增量落成按会话隔离的 JSONL，供 i2Agent tail 后经 SSE 在 i2Console 展示「深度思考」区块。观察者模式，需 `plugins.stream_reasoning_deltas: true` | 已上线验证 |
| [hermes-dag](./hermes-dag/) | Kanban 之上的插件级 DAG 工作流引擎：YAML DSL、`dag_*` 工具、条件边（动态建卡+子树剪枝）、`ctx.llm` 结构化裁决、webhook 跨机触发；首个业务图 `diag-rule-error`（i2stream-bkn 四方向诊断）。需配 `webhook_token`；worker 卡须带 assignee；gateway 重启后生效 | 已上线验证（2026-09-28 全链路验收） |

## 部署约定

本机惯例是**软链接**到用户插件目录（不拷贝，改源码即生效）：

```bash
ln -s /hdd/demo/public/hermes-plugin/<plugin-name> ~/.hermes/plugins/<plugin-name>
hermes plugins enable <plugin-name>
# 部分插件需要额外配置 ~/.hermes/config.yaml（见各插件 README）
# 涉及 gateway 进程级配置的插件启用后需重启 hermes gateway
```

## 插件开发约定

新增插件时保持与现有插件一致的骨架与原则：

1. **清单 + 入口**：目录下必须有 `plugin.yaml`（声明 `hooks`/`provides_tools`，
   hook 名只能用 hermes `VALID_HOOKS` 中存在的）和 `__init__.py` 的 `register(ctx)`。
2. **纯 stdlib 优先**：不声明 `requires_env`，不引第三方依赖，避免部署差异。
3. **失败隔离（fail-open）**：hook/工具内所有异常记录日志后吞掉——插件无论观察者
   还是工具型，任何故障只允许丢插件自身功能，不得影响对话主流程与已注册的流转。
4. **自带测试**：`tests/` 下用 pytest 覆盖核心契约（可 importlib 加载模块、
   伪造 ctx/payload，不依赖 hermes 运行时），`python3 -m pytest tests/ -q` 通过后再启用。
5. **文档随插件走**：每个插件目录内自带 README（协议、配置、验证步骤、故障语义）。

## 验证插件加载

```bash
hermes plugins list          # 确认 enabled
grep "unknown hook" ~/.hermes/logs/agent.log | tail   # 应无本库插件的 unknown hook 告警
```
