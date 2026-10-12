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
├── intent-router/             # 分层意图识别（正则 → 词典/BM25 → 大模型）
│   ├── plugin.yaml            # 插件清单（provides_hooks + config_schema）
│   ├── __init__.py            # register(ctx)：pre_llm_call + 工具面裁剪 + /intent + 可选工具
│   ├── *.py                   # normalize/entities/regex_tier/aho/bm25/scorer/router/actions…
│   ├── data/                  # 意图表 + 词表快照 + 标注语料（只读资产）
│   ├── tools/gen_lexicon.py   # 开发期：从 i2stream-bkn constants 生成词表快照
│   ├── docs/                  # 原理 / 阈值调参 / 上线路线 / 日志与排查
│   └── tests/                 # 单测 + 语料回归（importlib 加载，不依赖运行时）
├── thinking-language/         # 深度思考语言指令（pre_llm_call 注入中文规则）
│   ├── plugin.yaml            # 插件清单（provides_hooks: pre_llm_call）
│   ├── __init__.py            # register(ctx)：每轮注入 LANGUAGE_RULE
│   ├── README.md              # 插件级文档（机制/配置/验收）
│   └── tests/                 # 插件级 pytest（importlib 加载，不依赖运行时）
└── <future-plugin>/           # 后续插件按同样约定落位
```

## 当前插件

| 插件 | 说明 | 状态 |
|------|------|------|
| [reasoning-bridge](./reasoning-bridge/) | 把模型 reasoning 增量落成按会话隔离的 JSONL，供 i2Agent tail 后经 SSE 在 i2Console 展示「深度思考」区块。观察者模式，需 `plugins.stream_reasoning_deltas: true` | 已上线验证 |
| [hermes-dag](./hermes-dag/) | Kanban 之上的插件级 DAG 工作流引擎：YAML DSL、`dag_*` 工具、条件边（动态建卡+子树剪枝）、`ctx.llm` 结构化裁决、webhook 跨机触发；首个业务图 `diag-rule-error`（i2stream-bkn 四方向诊断）。需配 `webhook_token`；worker 卡须带 assignee；gateway 重启后生效 | 已上线验证（2026-09-28 全链路验收） |
| [intent-router](./intent-router/) | 分层意图识别：第一层正则（高精度直判）、第二层 Aho-Corasick 词典 + 字符二元组 BM25 + 三重门控（阈值/margin/证据条数）、第三层大模型只吃残差；高置信时直连**只读**工具并把结果注入本轮上下文，主模型一次请求即可作答。默认 `mode: shadow`（只记录、行为零改动），按 `docs/rollout.md` 逐档放开；`/intent` 命令族用于调参与自检；自带可读日志（可配目录、按大小+按日期双轮转、文件名带日期、落盘强制脱敏，见 `docs/logging.md`） | 已启用，**shadow only**（采数期；hint/act/裁剪全关，理由见该插件 `docs/decision-shadow-only.md`） |
| [thinking-language](./thinking-language/) | 通过官方 `pre_llm_call` 钩子每轮注入一条语言规则，让模型的深度思考（reasoning）用中文输出——针对「思考大多返回英文」（hermes 主系统提示词为英文）。规则仅 API 调用时注入 user message、不落会话历史；异常降级为本轮不注入，绝不打断对话 | 已部署待 gateway 重启生效（2026-10-12；pytest/清单校验/启用登记全过，重启后跑 e2e 中文占比验收） |

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
