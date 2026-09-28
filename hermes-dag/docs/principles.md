# hermes-dag 原理文档

> 版本：v0.1.0（对应实现 `/hdd/demo/public/hermes-plugin/hermes-dag/`）
> 日期：2026-09-26
> 前置：`/hdd/demo/public/bkn/hermes-dag-workflow-plugin-design.md`（引擎设计，下称"引擎设计文档"）、
> `/hdd/demo/public/bkn/i2stream-dag-orchestration-design.md`（业务蓝图）
> 本文讲**现在跑的这套实现**的原理；与设计文档的差异见 §8（源码核实后修正的清单）。

---

## 1. 定位与分层

一句话：**i2stream-bkn 给诊断思路（怎么查），hermes-dag 管流程编排（何时查、谁查、查完走哪），
Kanban 管执行（重试/回收/审计），worker 运行时取证产生事实（查到了什么）。**

```
触发        会话（模型 dag_run / 人 /dag run）        ← 管"何时启动"
   ▼
编排层      hermes-dag 插件                           ← 管"流程"
            DSL / 条件边 / 剪枝 / verdict / 数据流账本
   ▼ 建卡 + 盖章 body
执行层      Hermes Kanban（零改动）                    ← 管"运行"
            状态机 / dispatcher / 熔断 / 回收 / 审计行
   ▼ spawn worker，body = 思路指针 + 输出契约
思路层      i2stream-bkn 工具链 + skill               ← 管"怎么查"
事实层      worker 现场只读取证                        ← 管"查到了什么"
```

设计约束（继承自引擎设计文档）：不改 Hermes 源码，全部走公开面（插件 API + `hermes kanban`
CLI + 官方 plugin-data 目录）；不重造 Kanban 的执行机器；插件 fail-open。

## 2. 核心对象模型

| 对象 | 落点 | 说明 |
|---|---|---|
| spec | `data/templates/<name>.yaml` 或 runs.db `specs` 表 | 图的静态定义：nodes/edges/params/judge；版本化可审阅 |
| run | runs.db `runs` 行 | 一次执行实例：`{dag}-{时间戳}-{随机}`；状态机 `running → completed/failed/aborted` |
| node | spec 里的节点 + runs.db `run_nodes` 行 | 逻辑任务；`kind: exec\|verdict`；可带 `when`（条件）与 `foreach`（动态扇出） |
| card | kanban.db 真实任务卡 | node 的物理载体；`run_nodes` 记账 node→card_id（含多 shard） |
| shard | run_nodes.shard | foreach 节点按命中元素拆多张卡，shard=元素 slug |
| outputs | dataflow.db `node_outputs` | worker 交卡的结构化事实：`{verdict, evidence[], artifacts[], ...}` |

**三库分工（双账本 + 汇聚层）**：
- `kanban.db`——任务状态**唯一权威**。卡的状态机、依赖提升、派发、熔断、审计行全归它；
- `runs.db`——插件自己的账本：run 状态、幂等键、node→card 记账、跨进程推进队列、事件日志、specs；
- `dataflow.db`——outputs 汇聚：judge 的聚合输入、body 模板渲染取值源、dag_status 展示。

崩溃恢复不依赖内存态：advancer 用 kanban 实况 + runs.db 记账对账重建视图（§4.e）。

## 3. 触发与推进机制

### 3.1 触发入口（纯任务触发，无定时器）

| 入口 | 用法 | 说明 |
|---|---|---|
| 模型工具 | `dag_run(dag, params, idempotency_key)` | 对话驱动：用户描述故障任务，模型判断后调工具 |
| 斜杠命令 | `/dag run <name> [json参数]` | 人手动触发；与工具走同一 `DagEngine.dag_run` |
| webhook | `POST :8620/dag/run`（header `X-Dag-Token`） | 外部系统跨机推送（监控告警/平台）；另有 `GET /dag/status?run_id=` 与免鉴权 `GET /health`；强制 token，未配置拒绝启动 |

- **cron 未做**（按决策最后考虑）——当前没有自动周期触发。
- **进图决定权：模型自决 + 口诀引导**。`dag_run` 工具描述写明判据：多方向并行取证 /
  需要审计留痕 / 高频稳定工序，才值得进图；简单单点问题模型直接在会话查证回答，
  不为一次 grep 建图。观察到模型滥用再加闸（收紧为显式触发或 triage 前置裁决）。
- 幂等：相同 `idempotency_key` 返回既有 run（runs.db 唯一索引），不重复建图。
- 参数校验：spec.params 声明 required 的键缺失直接拒绝。
- spec 解析优先级：`dag_define` 落库的 spec > 内置模板 `data/templates/<dag>.yaml`。

### 3.2 全链路时序

```
用户会话               插件(DagEngine/advancer)        Kanban(gateway 内置)        worker
  │「规则报错，看日志」      │                              │                        │
  │──► 模型调 dag_run ───►│                              │                        │
  │                      │ 校验参数/幂等 → 建 run 行       │                        │
  │                      │ 编译首层4方向卡(create --parent/--skill/幂等键) ──► ready │
  │◄─ run_id ────────────│                              │                        │
  │                      │                    dispatcher 60s tick 认领 ready 卡   │
  │                      │                    ──spawn──► 零上下文 worker          │
  │                      │                              │   调 diagnose_* 拿思路   │
  │                      │                              │   加载 skill 拿只读命令  │
  │                      │                              │   现场取证              │
  │                      │                              │◄─ kanban_complete ─────│
  │                      │                              │   (summary, metadata.  │
  │                      │                              │    outputs) 落 task_runs│
  │                      │ ▲ 推进器感知完成（三层，见 3.3）│                        │
  │                      │ ├─ dataflow 落 outputs + 记账 done                     │
  │                      │ ├─ 父全终态 → verdict: judge(complete_structured)       │
  │                      │ │    → create+立即 complete（即时卡）                    │
  │                      │ ├─ when=false → 子树剪枝（archived 占位卡，§4.b）        │
  │                      │ └─ foreach hits → 逐 shard 建 rootcause 卡              │
  │                      │                    dispatcher spawn 下一层 ──►          │
  │                      │                    ……逐层直至 report 卡                  │
  │◄─ report 卡 worker：send_report.py 回调（analysis-id=run_id）+ 会话回复          │
  │                      │ run completed → verdict=hit 且有根因 → LP 候选落盘       │
```

要点：**卡与卡的执行推进完全归 Kanban**（dispatcher 认领、claim、熔断、回收）；
插件只在"父卡全终态"这个时刻**到点建卡**。Kanban 永远看到完整 DAG 拓扑。

### 3.3 推进的三层兜底

| 层 | 机制 | 延迟 | 现状与适用 |
|---|---|---|---|
| ① hook 队列 | `kanban_task_completed` / `kanban_task_blocked` 回调只写 runs.db 队列秒回；advancer 线程 2s 轮询消费（`UPDATE … WHERE consumed=0` 原子取，多进程消费者安全） | 秒级 | hook 在**调用 complete 的进程**触发。冒烟实测：CLI 进程（`hermes kanban complete`）不加载插件 → 不入队；worker 进程（`hermes chat`）待会话验收 |
| ② 60s 对账 | advancer daemon 线程每 `reconcile_interval`=60s 对所有活动 run 调 advance_run：以 kanban 实况校正记账（sync）→ 补推进 | ≤60s | **当前主通道**。gateway 必有（插件随 gateway 加载）；覆盖 hook 丢失、进程崩溃、双消费者竞争 |
| ③ 重入对账 | 新的 `dag_run` 调用末尾 advance_run | 按需 | gateway 重启后的恢复路径之一 |

注意：`dag_status` **纯只读**（读 kanban + 双库展示），不触发推进。

实现细节：`_sync_from_kanban` 在每次 advance 开头用 `kanban list` 实况校正——
kanban done/archived 的卡若记账未同步，则补 dataflow outputs（读该卡 closing run 的
metadata）并提升节点状态。这让三层兜底收敛为同一个幂等入口 `advance_run`。

### 3.4 延迟预算与调优

每层 DAG 的推进 ≈ 2×60s（对账发现父卡终态 + dispatcher 派发下一层）。四方向图全程
约 3 层派发，任务触发场景可接受。旋钮：

- `kanban.dispatch_interval_seconds`（Kanban 侧派发，默认 60s）；
- advancer 的 `reconcile_interval`（插件侧对账，默认 60s，代码内常量）；
- 若 worker 进程 hook 验收生效，则推进降为秒级，只剩派发延迟。

## 4. 五个核心原理

### a. Kanban 即执行层：done OR archived 扇入

Kanban 原生提升规则：一张卡的**全部父卡处于 done 或 archived** 才 `todo/ready` 提升
（`kanban_db.py:2038`；双保险：claim 时复检）。这就是 DAG 的入度归零语义——
插件不需要自造依赖跟踪，只需要**到点建卡**，Kanban 自己把下游跑起来。

### b. 剪枝 = archived 占位卡 + 汇合点语义

条件边没有原生支持，插件用"动态建卡 + 显式剪枝"合成：

- `when` 求值（受限表达式，§6）为 **false** → 从该节点起 DFS 剪枝：
  每个被剪节点建一张**占位卡并立即 archive**（comment 记 reason=condition_false）。
  archived 天然满足扇入（原理 a），所以**不需要**引擎设计文档设想的"剪枝扇入修正/移除父边"
  ——那个最易错环节在实现里不存在。
- **汇合点语义**：DFS 剪枝时，下游节点只有当其**全部父路径都在剪枝闭包内**才递归剪掉；
  存在任何活父路径的汇合点保持 pending，等其余父完成后正常建卡运行
  （被剪父的 archived 占位卡满足扇入）。这保证 `report` 这类无条件出口不被误剪——
  任务触发的图必须给出答复。此语义由单测
  `test_miss_path_prunes_subtree_with_placeholder` / `test_join_with_live_path_runs` 锁定。
- 深层子树的占位卡若建卡时父卡未齐（罕见，kanban 故障恢复期），记 `pruned-pending`
  延迟补建（advance_run 循环里重试）。

### c. verdict 虚拟卡：judge → create + 立即 complete

verdict 节点**不是 worker 会话**。所有父卡 done 后，advancer 调
`plugin_api.llm.complete_structured`（keyword-only：`instructions` + `input` 块 +
`json_schema`；默认借活跃主模型）做结构化裁决，然后 create 一张真实 verdict 卡并
**立即 complete**（unclaimed 卡完成走合成 run 路径，M0 实验 5），metadata=裁决 outputs：

- 下游卡的 parents 引用真实卡 id，Kanban 原生扇入继续工作；
- 图在 kanban.db 里**完整可审计**（裁决是一次真实交卡，不是账本里的影子）；
- 裁决结果同时落 dataflow，供 `when`/`foreach`/report 渲染使用。
- 口径（写死在 judge instructions，spec 只补领域描述）：只依据 evidence 裁决；
  任一方向 hit → hit；无 hit 有 inconclusive → **inconclusive ≠ clean**；
  全 miss → clean；judge 异常 fail-open 为 inconclusive（report 如实说明，不静默）。

### d. 数据流收窄：原生回放 + dataflow.db 三职责

v0.21.1 的 Kanban 已原生把父卡的 summary+metadata 回放进子卡 worker prompt
（`_ctx_parent_results`）——引擎设计文档里"上游结果靠创建者手工粘"的痛点已部分消失。
因此 dataflow.db 职责收窄为三件：

1. judge 的 run 级聚合输入（各方向 outputs 汇总）；
2. body 模板显式渲染 `{{ nodes.<id>.outputs.<key> }}` / `{{ params.<k> }}` / `{{ run.id }}`
   （比原生回放更可控，多 shard 合并为 `{"items": [...]}`）；
3. dag_status 的 outputs 摘要展示。

大体量证据走 artifacts：outputs 只放摘要，全量落 `/hdd/demo/public/reports/<run_id>/`。

### e. 三层推进兜底与双账本恢复

见 §3.3。恢复原则：**kanban.db 永远权威，runs.db 只是账本**。任何不一致
（hook 丢失、进程崩溃、记账落后）都在下一次 advance 的 sync 步骤用 kanban 实况
校正；单测 `test_sync_recovers_lost_hook` 模拟"worker 完成但 hook 事件全丢"，
reconcile 后记账/dataflow/推进全部补齐。

## 5. 幂等与恢复

| 层 | 机制 |
|---|---|
| run | runs.db `idempotency_key` 唯一索引；重复 `dag_run` 返回既有 run_id |
| 卡 | 幂等键 `dag:{run}:{node}[:shard]`（Kanban 同键返回既有卡，M0 实验 4）；记账 ON CONFLICT 更新 |
| 推进 | advance_run 幂等可任意重入：已终态节点跳过、已建卡节点跳过（记账 + 幂等键双保险）；进程内 per-run 锁串行化，跨进程并发靠幂等键收敛 |
| 崩溃 | 无内存态依赖：gateway 重启后插件重载 → advancer 线程 → 60s reconcile 补推进；verdict 卡半途崩溃由 sync 发现 kanban done 补齐 |

## 6. 判定口径与"思路 ≠ 结论"

铁律：**BKN 输出的是诊断思路（方向/机理/该查什么/建议命令指针），verdict/evidence 只能由
worker 运行时取证产生。** 落进实现的三处：

1. **body 七段契约**（[输入][目标][判据][思路入口][命令][决策][输出] + DAG 契约附录）：
   盖的是 BKN 工具调用建议 + skill 指针 + 只读命令白名单 + 输出 schema
   `{verdict: hit|miss|inconclusive, evidence[], artifacts[], direction_rank}`；
   evidence 必须含原始日志行与 Qdrant enrichment 细节，≥2 独立证据源。
2. **BKN 工具契约保证**：BKN 工具刻意剥离具体命令（`_filter_command_fields`）→
   具体命令只能从 skill 拿——"命令唯一来源是 skill"不是纪律而是结构强制。
3. **judge 隔离**：verdict 只读各方向卡的 evidence，没有工具、不查 BKN——
   查 BKN 只会拿到另一个"怎么查"，拿不到"查到了什么"。

`when`/`foreach` 表达式是受限 DSL（ast 白名单：`==/!=/in/not in/and/or/not` + 
`nodes.<id>.outputs.<key>` / `nodes.<id>.status` / `params.<name>` + 字面量），
静态校验拒绝一切其他语法（调用、下标、算术、双下划线……），求值走自写解释器，禁 `eval`。

## 7. 知识回流（半环）

run completed 时，若 verdict=hit 且 run 内存在根因产出（任一节点 outputs 含
`root_cause`，不耦合节点命名），sink 生成 LP 候选草稿：

```
~/.hermes/plugin-data/hermes-dag/patterns-pending/<run_id>.json
  {status: candidate, source: {run_id}, proposed_patterns: [{root_cause, symptom,
   remediation, error_codes, evidence_count}], updated_at}
```

字段对齐 i2stream-bkn 的 LP schema（`skill/i2stream-log-pattern-modeler`）。
**不自动回写 BKN**：候选由人 / `i2stream-bkn-updater` 流程评审后搬运进
`<bkn>/log-mode/` 正式库。没有这半环，DAG 只是消耗思路的管道；有了它才是
"值守一次、思路长一截"的闭环——但回写的闸门必须留在人手里。

## 8. 与设计文档的差异清单（源码核实修正）

| # | 设计文档假设 | 实测/实现 | 影响 |
|---|---|---|---|
| 1 | 剪枝需"扇入修正"（移除被剪父边，等价虚拟 done） | Kanban 提升条件原生含 archived（kanban_db.py:2038），archived 占位卡即虚拟 done | 最易错环节整块删除 |
| 2 | `ctx.llm.complete_structured(JSON schema)` | 真实签名为 keyword-only `complete_structured(*, instructions, input=[…], json_schema=…)`，需 ≥1 input 块 | verdict.py 按真实签名封装 |
| 3 | 数据流需解决"上游结果靠手工粘 body" | Kanban 原生回放父卡 summary+metadata 进子卡 prompt | dataflow.db 职责收窄为聚合/渲染/展示 |
| 4 | 剪枝整子树一律剪除 | 汇合点有活父路径必须照常运行（否则 report 出口被误剪） | 汇合点语义 + 两条单测锁定 |
| 5 | kanban hook 回调超时 30s 红线 | kanban hook 不在超时约束集内，但 observer-only（返回值丢弃） | hook 只入队的设计不变，理由从"超时"改为"observer + 跨进程" |
| 6 | "worker 互相不可见"（官方口径） | `kanban_show` 对 worker 全板可读、跨卡 comment 不受限 | 正确性不依赖不可见性（契约全在 body）；信息卫生靠专用 board |
| 7 | hook 在 gateway 触发 | 在**调用 complete 的进程**触发；CLI 进程不加载插件（冒烟实测），worker 进程待验收 | 三层兜底成为必需而非可选；60s 对账为当前主通道 |
| 8 | `register_cli_command(name, handler)` | 真实签名要求 `setup_fn`（argparse 子解析器） | `hermes dag` CLI 后置（本期用工具 + /dag 覆盖） |
| 9 | 卡初始状态 todo | 无父卡创建即 ready；父全 done 时创建即 ready | verdict 即时卡 create+complete 直通 |

## 9. 复杂度控制与适用边界

- **图数量 ∝ 问题类型数量**，不是问题数量：六维度方向集有限，日常值守预计 2~3 张模板
  （巡检 + 诊断 + 可选修复验证）覆盖大部分；骨架固定、个案是 params 数据
  （类比 SQL 语句与绑定变量）。
- **进图口诀**：跑过三遍、步骤能列出来的 → 沉淀成图；第一次见的 → 留在会话跑 ReAct。
  与 skill 沉淀逻辑一致：先有重复实践，后有固化资产。
- **执行型 vs 推理型**：诊断问题图是主体（流程可枚举）；业务/设计问题图是"边框"
  （取数扇入 + 评审扇入是图，中间的灵魂推理留会话）——图的核心贡献是强制扇入：
  来源不全的方案在结构上出不了图。
- 复杂度守恒但显式化净赚：从隐式（活在每次对话即兴里，每次重新支付）变成显式
  （写在 spec 一次，可审阅、版本化、每次执行一致）。

## 10. 安全与 fail-open

- **只读取证**：方向卡 body 命令白名单（grep/cat/ps/ss/ls/df/stat/etcdctl get/iadebug 只读），
  写操作本期不在任何节点出现；RiskGuard（BKN 插件 pre_tool_call hook）在 worker 进程
  全局生效，灰度期只记录不阻断；i2Agent exec_shell 高危关键字另有会话级审批。
- **webhook 鉴权**：所有 `/dag/*` 请求强制 `X-Dag-Token`（hmac.compare_digest 恒时比较）；
  token 未配置服务拒绝启动（0.0.0.0 无鉴权不可接受）；`/health` 免鉴权仅暴露存活；
  body 上限 1MB；HTTP 服务为 daemon 线程，随插件卸载关闭。
- **表达式防注入**：受限 DSL 白名单 + 自写解释器（§6）。
- **fail-open**：hook 回调只入队且 try/except 全包；advancer 循环异常只记日志；
  引擎任何故障降级为"条件边不自动推进"，卡片仍在 kanban 里可人工 complete/archive/unblock；
  judge 失败降级为 inconclusive（不静默、report 如实说明）。
- **数据边界**：插件可变状态全部在官方 plugin-data 目录；插件目录只放只读资产；
  LP 候选不自动进 BKN（闸门在人）。
