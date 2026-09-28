# hermes-dag 运维覆盖面扩展路线（审计稿）

> 状态：**待审计**——本文档是决策材料，未实施任何图、未写任何代码
> 日期：2026-09-26
> 依据：两轮源码/本体探索实证（Hermes 0.21.1 审批机制；i2stream-bkn 护栏覆盖），关键数字见 §2/§6
> 审计方式：逐图审阅 §3，按 §5 决策点清单拍板；批准一张建一张

---

## 1. 背景与现状

现有 **1 张业务图**：`diag-rule-error`（规则报错四方向诊断），覆盖运维全生命周期中
"故障定位—报错类切片"，全程只读。三层覆盖面框架：

| 层 | 覆盖面 | 定位 |
|---|---|---|
| DAG 业务图层 | 1 个切片（刻意窄而深） | 高频稳定工序的固化加速层 |
| BKN + 会话层 | 接近全域（12 工具 + 30 skill，人驱动对话内） | 覆盖面上限 |
| 引擎层（hermes-dag） | 与场景无关，任意卡片依赖图 | 编排能力 |

扩图 = 在第 1 层增加切片；第 2/3 层不改。**覆盖面小不是缺陷**——部署/容灾这类低频高危
工序进图是浪费（进图口诀：跑过三遍、步骤能列出来才沉淀）。

## 2. 两个探索实证的设计约束（扩展计划的地质条件）

### 2.1 人工审批机制：Kanban 没有"运行中暂停"原语

源码实证（v0.21.1）：

| 机制 | 实测结论 | 结论 |
|---|---|---|
| `request-review` 流程 | **后置门**：worker 跑完送审（request-review 结束其 run、清 claim），人/审阅 worker `complete` 即批准 | 不能当"执行前等审批"用 |
| `block` / `unblock` | worker 可自 block（needs_input），人 unblock 后 **spawn 新 worker**（对话上下文丢失，仅持久记录重建） | 可用但体验差 |
| `approvals.mode` | 对 dispatcher spawn 的 worker（`chat -q`、stdin=DEVNULL、`HERMES_SINGLE_QUERY_SESSION=1`）只有两档：默认 **auto-deny**（fail-closed）或 `single_query_mode: approve`（全放行）——**无法交互审批单条命令** | 不能做人审通道 |

**采用的方案：无 assignee 审批卡**（全部复用现有原语，引擎改动 ~1 天）：

- dispatcher 按 assignee profile 认领任务 → **无 assignee 的卡永远不会被 spawn**（M0 实测过 ready 卡无人认领即静置）；
- advancer 建"审批卡"（无 assignee，body 写明审批指引：审计 fix-plan 卡的方案与 dry-run 输出 →
  `hermes kanban complete <审批卡> --summary "approved" --metadata '{"approval":"granted"}'`
  或 `--metadata '{"approval":"denied"}'`）；
- advancer 读审批卡 metadata：`granted` → 放行下游 execute 卡；`denied` → **剪枝子树**（复用现有
  archived 占位语义，report 仍直达、如实记录"人工拒绝"）；
- 审批动作留痕：卡片行 + metadata 永久审计。

### 2.2 BKN 护栏覆盖：写操作分三档（详表见 §6）

37 个操作可路由（21 写/管理类），护栏强度差异极大：

| 档 | 操作 | 依据 |
|---|---|---|
| **可自动化** | 规则 create / start / stop / delete / modify、register_db、compare_table | 脚本全带 `--dry-run` + confirm 标志（`--confirm-delete/--confirm-modify/--stop-parse/--force`）；17 条运行时规则中 4 条 blockable 兜底（-4071 删 RUNNING / -4031 重启 ABNORMAL / -4071 重启 RUNNING / -4016 注册 OFFLINE 节点） |
| **部分护栏** | activate_node、modify_db / delete_db、create_compare | 脚本有 dry-run，但规则无 error_code 只提示不阻断 |
| **人工专属** | **iadebug 修复类**（resumeforce / reset_track_position / fix_table_diff / fix_by_compare）、**repair_data**（无脚本）、**failover apply**（stub 返回 not_implemented）、proxy_etcd_import / del_alarm、delete_compare | 无 dry-run 或无脚本；data_loss 类规则被 RiskGuard 排除、永不阻断 |

另：**precheck 复合（prerequisites + risk + compatibility 三合一）目前不存在**——只有文档级指针，
无工具无 skill 链。G2 就是它的首个落地。

## 3. 候选图详设

### G1 `diag-symptom` 症状类诊断图

- **场景**：无明确错误码的四大症状工单——增量卡住（incremental_stuck）、数据不一致
  （data_mismatch）、性能下降（performance_degradation）、崩溃循环（crash_loop）。
  与 diag-rule-error（error_code 驱动）互补，覆盖值守最高频的另两类。
- **形状**：`params.symptom` 入口 → 4 方向卡并行（进程状态 / 数据库链路 / 数据校验 / 规则配置，
  判据从 `diagnose_db_link(symptom=...)` 返回的 `knowledge_context.recommended_steps` 与
  `skill_context.recommended_skills` 派生）→ verdict 扇入 → rootcause（when hit，foreach hits）→ report。
- **触发**：三入口同现图（会话自决 / `/dag run` / webhook）。
- **护栏**：全只读（同现图纪律）。
- **前置依赖**：无（引擎、BKN 契约全部现成）。
- **成本**：0.5–1 天（spec + 判据提炼 + 测试）。
- **风险**：低——结构与现图同构，只是判据内容不同。
- **覆盖增量**：故障定位从"报错类"扩到"症状类"。
- **验收**：真实 run（如制造一个增量停滞现象）走通 hit 与 miss 双路径。
- **决策点**：四方向取哪几路（建议沿用 `diagnose_db_link` 的 symptom→dimensions 映射，不另造）。

### G2 `precheck` 变更前置检查图

- **场景**：任何受控写操作执行前的强制检查——人执行变更前手动触发，或作为 G3 execute 卡的父卡。
  同时是**全体系首个 precheck 复合落地**（§2.2 证实目前不存在）。
- **形状**：`params = {operation, target, current_state}` → 4 张并行检查卡：
  ① `get_prerequisites(operation)` → 前置链条核对；
  ② `check_action_risk(action, current_state)` → 机器可判的 PASS/BLOCK + error_code；
  ③ `check_compatibility(source, target)`（涉及数据库对象时）→ 硬约束矩阵；
  ④ `resolve_operation(operation)` → 技能路由 + `constraint_ids` 清单
  → verdict（合成 **PASS / BLOCK + 逐条原因**）→ report。
- **触发**：会话 / webhook（监控或工单系统在变更窗口前自动触发）。
- **护栏**：纯只读。
- **前置依赖**：无。
- **成本**：1 天。
- **风险**：低。
- **覆盖增量**：从"事后诊断"扩到"事前护栏"；独立价值高（不建 G3 也值得建）。
- **验收**：对 `delete_sync_rule`（RUNNING 状态）产出 BLOCK + -4071 + 证据；对合法操作产出 PASS。
- **决策点**：无——建议直接批准（除非你认为 G2 应并入 G3 不单独建）。

### G3 `diag-fix-verify` 诊断→审批→处置→验证闭环图

- **场景**：诊断命中后的受控修复闭环——覆盖面从"定位"扩到"闭环"（hit 之后的事）。
- **形状**：
  ```
  [G1/G0 命中] → fix-plan 卡（只出方案 + 必须 --dry-run 预演输出，不执行）
                       │
                       ▼
               审批卡（无 assignee，人 complete：granted / denied）
                       │ granted                │ denied → 剪枝子树
                       ▼
               execute 卡（白名单脚本 + confirm 标志，--max-runtime 必配）
                       │
                       ▼
               verify 卡（只读回读验证） → report
  ```
- **写操作白名单（最小集，§2.2"可自动化"档）**：规则 create / start / stop / delete / modify、
  register_db、compare_table。**排除** §2.2"人工专属"档全部操作。
- **五层护栏**：
  1. 白名单——execute 卡 body 写死允许的脚本与参数形态，spec 层面枚举；
  2. dry-run——execute worker 必须先跑 `--dry-run` 并把输出写进 evidence，再 `--confirm-*` 执行；
  3. precheck 父卡——execute 的父卡含 G2 precheck（verdict=PASS 才放行）；
  4. 人工审批卡——无 assignee，人 complete，denied 剪枝；
  5. fail-closed 兜底——execute worker profile 的 `approvals.single_query_mode` 保持默认
     deny（白名单外任何危险命令自动拒绝）；RiskGuard 保持 `block: true`。
- **引擎配套改动（~1 天）**：① `compiler` 透传 `--max-runtime`（execute 卡必配，防失控）；
  ② advancer 识别审批卡 metadata（`denied` → `_prune_subtree`，`granted` → 正常推进——本质是
  一种 when 条件，改动极小）。
- **前置依赖**：G2（execute 父卡）；审批机制确认（§2.1 方案）；execute 用哪个 profile。
- **成本**：2–3 天（含引擎 1 天 + spec/判据/测试）。
- **风险**：**最高**（首次写操作自动化）。缓解=五层护栏 + 白名单极小 + 每层留痕。
- **覆盖增量**：运维闭环从"知道问题"到"解决问题并验证"。
- **验收**：非生产靶环境跑通"人为制造 ABNORMAL 规则 → 诊断 hit → fix-plan → 审批 granted →
  execute（dry-run→confirm）→ verify 回读 → report"；再跑一遍 denied 路径确认剪枝与留痕。
- **决策点**：白名单范围（7 类还是更小起步）；execute profile 选择；审批动作是否要二次确认。

### G4 `inspect-daily` 巡检图

- **场景**：定时五方向扫描（磁盘/内存/规则状态/全量进度/diff 日志），clean → 静默，hit → 报告。
- **形状**：同现图骨架 + **静默分支**（clean 时 report 卡只回调不进会话——cron 模式天然无会话）。
- **触发**：cron——**依赖你"最后考虑"的决策**。
- **护栏**：全只读。
- **前置依赖**：cron 触发决策。
- **成本**：1 天（引擎零改动）。
- **风险**：低。
- **覆盖增量**：从"被动触发"到"主动发现"。
- **验收**：连续两轮 cron 触发，clean 静默 + 注入异常后 hit 报告。
- **决策点**：cron 接管方式（新 cron job vs 现有哨兵升级）；[SILENT] 口径。

### G5 明确不做（负面清单）

| 不做 | 原因（实证） | 解锁条件 |
|---|---|---|
| failover 自动化 | apply 未实现（stub 返回 not_implemented）；must_full_verify_before_failover 规则永不触发；planned/forced 是人工决策 | BKN 补实现 + 规则加 error_code；且 planned/forced 永远人工 |
| iadebug 修复类（resumeforce/reset_track_position/fix_table_diff/fix_by_compare） | 无脚本无 dry-run，data_loss 类约束被 RiskGuard 排除（永不阻断），出事不可拦截 | BKN 补 dry-run + 规则可阻断化 |
| repair_data | 无脚本（compare-mechanism.md 明示未提供） | 同上 |
| proxy_etcd_import / del_alarm、delete_compare | data_loss 约束不阻断 / 零约束 | 同上 |
| 部署/装机/容灾切换进图 | 低频高危、步骤随环境变、人工决策点密集 | 原则上不进图 |

## 4. 建议排序与依据

```
G1（0.5-1天，零前置，补最大频次缺口）
  → G2（1天，precheck 首落地，独立可用 + 为 G3 铺路）
    → G3（2-3天，覆盖面最大扩展，安全设计最重，建议在靶环境验收后才碰生产）
      → G4（等 cron 决策）
```

每张图跑通真实验收再建下一张。G2 即使不建 G3 也值得单独建（人执行变更前的检查清单自动化）。

## 5. 用户决策点清单

| # | 决策点 | 选项 |
|---|---|---|
| ① | 建图顺序 | 按建议 G1→G2→G3→G4，或自选 |
| ② | G3 白名单范围 | 最小 7 类（规则 5 类 + register_db + compare_table）起步，还是先只放 compare_table 单类试点 |
| ③ | 审批卡机制 | 确认"无 assignee 卡 + complete metadata granted/denied"方案（§2.1），还是改用 block/unblock |
| ④ | RiskGuard blockable 扩充 | 当前仅 4 条可阻断——是否投入 BKN 数据工程（给 13 条 critical 规则补 error_code）后再建 G3，还是 4 条 + 白名单 + fail-closed 先跑 |

## 6. 附：护栏覆盖总表（探索实证）

| 操作 | prerequisites | 运行时规则 | 可阻断 | 脚本 dry-run | 档位 |
|---|---|---|---|---|---|
| create_sync_rule | ✅（both_nodes_NORMAL、must_dry_run_first；前置链 activate→register_db） | 2 | ✗（提示/dry-run 门） | ✅ `--dry-run/--create` | 可自动化 |
| start_sync_rule（含 restart 归一） | ✅ 3 规则 | 3 | **✅ -4031 / -4071** | ✅ `operate_rule.py --dry-run` | 可自动化 |
| stop_sync_rule | ✅ 2 规则 | 2 | ✗（FULLSYNC→error_code 空） | ✅ `--stop-parse` + dry-run | 可自动化 |
| delete_sync_rule | ✅ 3 规则 | 3 | **✅ -4071** + dry-run 门 | ✅ `--confirm-delete/--force` + dry-run | 可自动化 |
| modify_sync_rule | ✅ | 1 | ✗ | ✅ `--confirm-modify` + dry-run | 可自动化 |
| register_db | ✅ 前置链 worknode activated | 2 | **✅ -4016** | ✅ `--dry-run/--create` | 可自动化 |
| compare_table（create_compare） | ✅（9:00-18:00 阈值规则） | 1（时间窗） | ✗ | ✅（rule-manager `compare_table.py --dry-run`） | 可自动化 |
| activate_node | ✅（param_constraint 类） | 0（被排除） | ✗ | ✅ `--dry-run/--apply` | 部分护栏 |
| modify_db / delete_db | ✅ | 1 | ✗（降级/提示） | ✅ `--apply/--force` + dry-run | 部分护栏 |
| failover | ✅ must_full_verify（永不触发） | 1 | ✗ | dry-run 有、**apply 未实现** | 人工专属 |
| repair_data | ✅ must_backup_target_table | 0（data_loss 被排除） | ✗ | **无脚本** | 人工专属 |
| iadebug_resumeforce / reset_track_position / fix_table_diff / fix_by_compare | ✅（data_loss 行仅展示） | 0 | ✗ | ✗（CLI prose 警告） | 人工专属 |
| proxy_etcd_import / del_alarm | ✅（data_loss） | 0 | ✗ | ✗ | 人工专属 |
| delete_compare | ✗ | 0 | ✗ | stub | 人工专属 |

规则总量：`constraints.bkn` 25 条 ID → 运行时组装 17 条（data_loss/param_constraint 类被排除）→
**4 条可阻断**（全部 critical + 有 error_code）；RiskGuard `block: true` 已启用。
