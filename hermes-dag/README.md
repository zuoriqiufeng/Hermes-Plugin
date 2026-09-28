# hermes-dag

Hermes 插件级 DAG 工作流引擎：**Kanban = 执行层，插件 = 定义层 + 数据流层**。
首个业务图 `diag-rule-error`：任务触发的 i2stream-bkn 四方向诊断（日志 / 连接 / 容量 / 规则配置）。

- 原理与架构（完整版）：[docs/principles.md](docs/principles.md)
- 地基验证记录：[docs/m0-ground-verification.md](docs/m0-ground-verification.md)
- **部署与验收记录（2026-09-28，全链路 PASS）**：[docs/deployment-acceptance.md](docs/deployment-acceptance.md)
- **覆盖面扩展路线（审计稿，5 张候选图）**：[docs/expansion-roadmap.md](docs/expansion-roadmap.md)
- 业务蓝图：`/hdd/demo/public/bkn/i2stream-dag-orchestration-design.md`
- 引擎设计：`/hdd/demo/public/bkn/hermes-dag-workflow-plugin-design.md`

## 最终任务（北极星）

**任务进来 → 图跑起来 → 报告出去 → 证据留痕。**
用户在会话里抛出故障任务（「规则 4C0AC40B 报错了，看看日志」）→ 模型调 `dag_run` 一键建图 →
四方向卡并行只读取证（worker 零上下文：BKN 工具给思路、skill 给命令、现场取证交 evidence）→
judge 结构化裁决（inconclusive ≠ clean）→ 命中方向自动深挖根因 → 六维度报告回调 + 回复会话。
流程在 spec（版本化）、证据在 dataflow.db、状态在 kanban.db（唯一权威）、卡片行永久可审计。

## 工作流结构图（diag-rule-error）

```
        任务（会话「规则报错了看看日志」 / /dag run / webhook POST）
                                              │  dag_run（幂等键防重）
                                              ▼  编译首层（4 张方向卡）
┌─[ready]────────────┐┌─[ready]────────────┐┌─[ready]────────────┐┌─[ready]────────────┐
│ dir-log 日志方向   ││ dir-conn 连接方向  ││ dir-cap 容量资源   ││ dir-rule 规则配置  │
│ 思 diagnose_error  ││ 思 diagnose_db_link││ 思 resolve_relation││ 思 query_product   │
│ 命 log-analyzer    ││ 命 db-diagnostics  ││ 命 node-inspection ││ 命 rule-manager    │
│ 判 ERROR行/位点停滞││ 判 连接错误码/认证 ││ 判 磁盘/RSS/全量   ││ 判 规则状态/漂移   │
└──────────┬─────────┘└──────────┬─────────┘└──────────┬─────────┘└──────────┬─────────┘
            │                      │                      │                      │
            ┌                      ┴          ┼           ┴                      ┐
                                              │
                                              ▼
┌─[verdict · 即时卡 · 非 worker]─────────────────────────────────────────┐
│ judge：读 4 份 evidence 结构化裁决（不用工具、不查 BKN）               │
│ 出 {verdict: hit|clean|inconclusive, hits[], reason}                   │
│ 口径：inconclusive ≠ clean（宁可误报不可漏报）                         │
└────────────────────────┬────────────────────────────────────────────┬──┘
                           hit（命中 → 逐方向深挖）                              clean / inconclusive（剪枝直落）
                         │                                            │
┌─[ready]───────────────┴────────────────────────────────────────────────┐
│ rootcause · dir-log（×N）                                              │
│ foreach hits：逐命中方向建卡                                           │
│ ≥2 独立证据源 → 根因闭环（六维度）                                     │
└───────────────────────────┬────────────────────────────────────────────┘
                            │                                         │
                            ▼                                           ◄ rootcause 已剪枝（archived 占位），直达报告
┌─[report · 无条件出口]────┴─────────────────────────────────────────┴───┐
│ 必须给答复                                                             │
│ 六维度结论（现象/证据/根因/影响/建议/置信度）                          │
│ → send_report.py 回调（analysis-id = run_id）→ 会话回复                │
└────────────────────────────────────────────────────────────────────────┘

  卡状态：ready → running → done（Kanban 权威）· 剪枝卡 → archived（原生满足扇入）
  worker 卡共同交卡契约：kanban_complete(metadata.outputs={verdict, evidence[], artifacts[], rank})
  思 = BKN 思路工具 · 命 = 命令来源 skill · 判 = 命中判据 · 卡标题真实形如 [r1] dir-log
```

（verdict=hit 且有根因产出时，run 收尾顺带落 LP 候选到待审目录——见原理文档 §7。）

## 完整生命周期（一次任务触发的全过程）

```
══════════ 一个任务的生命周期：从「规则报错了看看日志」到结论回调 ══════════

T0 任务到达（三入口，任一）
   ├─ 会话：「规则 4C0AC40B 报错了，看看日志」
   │    └─ 模型自决进图（口诀：多方向并行 / 需审计留痕 / 高频稳定工序才进图）
   ├─ 人显式：/dag run diag-rule-error {"rule_id": "..."}
   └─ 外部系统：POST :8620/dag/run（header X-Dag-Token）
        │
        ▼
T1 dag_run 同步段（秒级）
   幂等检查 → 建 run 行 → 编译首层 4 张方向卡(ready) → 返回 run_id
        │
        ▼
T2 派发（Kanban dispatcher，≤60s tick）
   认领 ready 卡 ──spawn──► 4 个零上下文 worker（profile 进程）
        │
        ▼
T3 方向取证（worker 内 ReAct，分钟级，四路并行互不污染）
   每个 worker：
     ① BKN 工具拿思路  diagnose_error / diagnose_db_link（机理·方向·日志导航）
     ② 加载 skill 拿命令  只读白名单（grep/ps/iadebug/etcdctl get…）
     ③ 现场取证 + search_qdrant enrichment（evidence ≥2 独立证据源）
     ④ 交卡 kanban_complete(metadata.outputs = {verdict, evidence[], rank})
        │
        ▼
T4 推进（advancer 感知 ≤60s：hook 队列 / 60s 对账兜底）
   dataflow 落 outputs → 判断"父卡是否全终态"→ 到点建卡：
     ├─ verdict：judge 读四份 evidence 结构化裁决（不用工具、不查 BKN）
     │    → create + 立即 complete（即时卡）
     │    → {verdict: hit|clean|inconclusive, hits[], reason}
     ├─ when=false 的节点 → 占位卡 archived（剪枝；汇合点有活路径不剪）
     └─ foreach hits → 逐命中方向建 rootcause 卡
        │
        ▼
T5 下一层执行（同 T2→T3，逐层直至出口）
   rootcause 卡 ×N：根因深挖（现象→证据→根因→影响→建议，证据闭环）
   report 卡：六维度结论 → send_report.py 回调（analysis-id = run_id）
                       → 会话回复（带原始日志行 + Qdrant 细节）
        │
        ▼
T6 收尾（同步于最后一次推进）
   run completed
   └─ verdict=hit 且有根因 → LP 候选 JSON 落 patterns-pending/
      （人工评审 → 搬运进 <bkn>/log-mode/ —— 知识回流半环，闸门在人）

全程可观测：/dag status（会话） · GET :8620/dag/status（跨机） · kanban 卡片行（永久审计）
延迟预算：每层 ≈ 2×60s（推进对账 + 派发）；总时长 ≈ 3 层派发 + 各 worker 取证时长
```

分支说明：T4 里 verdict 若为 clean / inconclusive，rootcause 被剪枝（archived 占位卡），
流程直达 report——**任务触发图永远给答复**：hit 给根因，inconclusive 如实说明卡在哪，
clean 说明四方向均未发现异常。

## 快速开始（会话验收路径）

前置（一次性）：

```bash
hermes kanban boards create i2stream-ops   # 正式运行板（插件默认 board，跨板不能 link）
# 插件已软链 ~/.hermes/plugins/hermes-dag 且已 plugins enable；重启 gateway 后生效
```

然后在 Hermes 会话里对模型说：

> 规则 4C0AC40B 报错了，你看看日志，发生了什么情况

预期：模型调 `dag_run(dag="diag-rule-error", params={...})` → 四方向卡并行 spawn →
全部交卡后 judge 裁决 → 命中方向 rootcause 深挖 → report 回调（send_report.py，
analysis-id = run_id）→ 会话收到带证据链的结论。`/dag status` 全程可查。

## 触发机制（简版）

**没有 cron**——任务触发，三个入口：

| 入口 | 用法 | 谁用 |
|---|---|---|
| 模型工具 `dag_run(dag, params, idempotency_key)` | 对话驱动，模型判断需要建图时调用 | 模型 |
| 斜杠命令 `/dag run <name> [json参数]` | 人手动触发，与工具走同一 `DagEngine.dag_run` 入口 | 人 |
| webhook `POST :8620/dag/run`（header `X-Dag-Token`） | 外部系统跨机推送（监控告警/平台）；另有 `GET /dag/status?run_id=`、`GET /health` | 外部系统 |

**进图决定权：模型自决 + 口诀引导**——`dag_run` 工具描述写明判据（多方向并行取证 /
需要审计留痕 / 高频稳定工序才进图），简单单点问题模型直接在会话里回答，不为一次 grep 建图。
相同 `idempotency_key` 重复触发返回既有 run，不会重复建图。webhook 强制 token：
未配置 `webhook_token` 服务拒绝启动（0.0.0.0 无鉴权不可接受）。

run 启动后：入口同步编译首层卡 → **Kanban 内置 dispatcher**（60s tick）认领 ready 卡、
spawn worker → worker 交卡后由**推进器**接续建下一层卡。推进器三层兜底：

| 层 | 机制 | 延迟 | 现状 |
|---|---|---|---|
| ① | `kanban_task_completed` hook → SQLite 队列 → advancer 线程消费 | 秒级 | 冒烟实测 CLI 进程不加载插件 hook；worker 进程待验收 |
| ② | advancer 线程每 60s reconcile（扫 kanban 实况补推进） | ≤60s | **当前主通道** |
| ③ | `dag_run` 重入顺带对账 | 按需 | 兜底 |

`/dag status` 是纯只读，不触发推进。每层 DAG 延迟预算 ≈ 2×60s（推进对账 + 卡片派发）；
要提速调 `kanban.dispatch_interval_seconds`。详见 [docs/principles.md §3](docs/principles.md)。

## 工具面

| 工具 | 语义 |
|---|---|
| `dag_define(spec_yaml, validate_only)` | 解析/校验/环检测/条件表达式静态检查/（落库） |
| `dag_explain(dag)` | 静态结构展示（节点/依赖/when/foreach/skills），供人/模型审阅，不执行 |
| `dag_run(dag, params, idempotency_key)` | 幂等触发 run：编译首层卡并开始推进 |
| `dag_status(run_id)` | 节点状态矩阵 + ASCII 依赖图 + outputs 摘要 + 事件时间线（**只读**） |
| `/dag list\|status\|run\|explain` | 斜杠命令（同一引擎逻辑，与工具不分叉） |

## 模板

- `data/templates/diag-rule-error.yaml`——四方向诊断图：
  `dir-log / dir-conn / dir-cap / dir-rule`（并行）→ `verdict`（judge 扇入裁决）→
  `rootcause`（`when: verdict=='hit'`，`foreach: hits`，每命中方向一张卡）→
  `report`（无条件出口，任务触发必须给答复）。
- 新增模板两种方式：YAML 放 `data/templates/`（随插件分发，`<name>.yaml` 即 `<name>`），
  或会话里 `dag_define(spec_yaml)` 落库（存 runs.db 的 specs 表，优先于内置模板）。
- **worker 节点必须盖 `assignee`**（本模板为 `default`）——dispatcher 对无 assignee 的
  ready 卡静默跳过（`skipped_unassigned`）；verdict 即时卡刻意不盖（推进器建后立即完成，
  无 assignee 保证 dispatcher 不会抢跑）。
- 铁律：spec 只盖**思路入口指针**（BKN 工具调用建议 + skill 名）与判据，
  结论只能来自 worker 运行时取证——**思路 ≠ 结论**。

## 目录结构

```
hermes-dag/
├── plugin.yaml            # 插件清单（provides_tools/hooks、config_schema）
├── __init__.py            # register()：工具 + /dag + hooks + advancer 线程（fail-open）
├── schemas.py             # dag_* 工具 JSON Schema（LLM 所见）
├── dsl.py                 # YAML spec 解析/校验/环检测/edges 并集
├── evaluator.py           # when/foreach 受限表达式（ast 白名单，禁 eval）
├── render.py              # {{ path.to.value }} mini 渲染
├── kanban_client.py       # hermes kanban CLI 子进程封装（引擎唯一写卡通道）
├── runstore.py            # runs.db：run 账本/幂等键/node→card 记账/跨进程队列/事件
├── dataflow.py            # dataflow.db：node outputs 汇聚（judge 聚合/渲染/展示）
├── compiler.py            # node→卡：body 七段契约、parents 翻译、幂等键、即时卡/剪枝占位
├── verdict.py             # judge：complete_structured 裁决 + 口径 + fail-open
├── advancer.py            # 推进器：sync 对账/条件求值/剪枝/foreach/收尾/挂 sink
├── hooks.py               # kanban hook → SQLite 队列（秒回，fail-open）
├── tools.py / commands.py # DagEngine 门面 + 四工具 + /dag
├── sink.py                # pattern-sink：LP 候选 → patterns-pending/（不自动回写 BKN）
├── data/templates/        # 内置业务图
├── docs/                  # 原理文档 + M0 验证记录
└── tests/                 # 单测（Fake Kanban）+ 真 CLI 集成冒烟
```

状态存储：`~/.hermes/plugin-data/hermes-dag/`（官方 plugin-data 目录，WAL SQLite：
`runs.db` + `dataflow.db` + `patterns-pending/`）。插件目录本身只放只读资产。

## 开发与测试

```bash
# 单测（不依赖 hermes CLI；Kanban 用 Fake 模拟）
python3 -m pytest hermes-dag/tests/test_dag.py -q

# 集成冒烟（真实 CLI + 沙箱库 /tmp/hermes-dag-smoke，gateway 不可见，不 spawn worker）
mkdir -p /tmp/hermes-dag-smoke && rm -f /tmp/hermes-dag-smoke/*
HERMES_KANBAN_DB=/tmp/hermes-dag-smoke/kanban.db hermes kanban init
/hdd/demo/public/venv/bin/python hermes-dag/tests/smoke_integration.py
```

测试覆盖要点：hit 路径（方向→verdict→foreach shard→report→completed）、miss 路径
（剪枝占位 + report 照常出口）、foreach 空集占位、汇合点有活父路径不剪、
hook 丢失后的 sync 恢复、LP 候选生成（hit 产 / miss 不产）、幂等重入、注入拒绝。

## 插件设置（config.yaml → plugins.entries.hermes-dag.settings）

| 键 | 默认 | 说明 |
|---|---|---|
| `board` | `i2stream-ops` | 所有 DAG 卡所在板（跨板不能 link，须专用板） |
| `advancer_enabled` | `true` | 是否在本进程启动 advancer daemon 线程 |
| `judge_model` | — | **预留未接线**（config_schema 已声明，实现暂固定借活跃主模型；接线需 `llm.allow_model_override` 门控） |
| `webhook_enabled` | `true` | 是否启动 webhook HTTP 入口 |
| `webhook_host` | `0.0.0.0` | webhook 监听地址（跨机可达） |
| `webhook_port` | `8620` | webhook 监听端口 |
| `webhook_token` | — | **必配**，否则 webhook 拒绝启动；调用方带 `X-Dag-Token` 头，恒时比较 |

## 核心语义速记（源码级核实，v0.21.1）

1. Kanban 扇入条件 = 父卡 `done OR archived`（kanban_db.py:2038）→ **剪枝 = archived 占位卡**，无需改链路；
2. verdict 虚拟卡：judge 后 create + 立即 complete（unclaimed 卡合成 run），
   图在 kanban.db 完整可审计，下游由 Kanban 原生提升；
3. Kanban 原生把父卡 summary+metadata 回放进子卡 prompt → dataflow.db 只管 judge 聚合/渲染/展示；
4. 双账本：kanban.db 永远权威；runs.db 只记账（崩溃恢复靠对账，不靠内存态）；
5. 卡级幂等键 `dag:{run}:{node}[:shard]` + run 级 idempotency_key，重复推进不重复建卡。

## 边界（本期不做）

cron 触发（最后考虑）/ `hermes dag` CLI / script 节点 / 全局 worker prompt 段 /
BKN 自动回写（LP 候选人工评审搬运）/ 多 profile 角色化（spec 预留 assignee 字段）。

后续扩图候选（症状诊断 / 变更前置检查 / 处置闭环 / 巡检）及"明确不做"负面清单，
见 [docs/expansion-roadmap.md](docs/expansion-roadmap.md)——待逐图审计决策后实施。
