# hermes-dag 效率优化：基线、改造与实测对照

> 日期：2026-09-28/29
> 对照对象：同一模板 `diag-rule-error`、同参数（rule_id=4C0AC40B / error_code=CDB6157A / 症状=规则报错）、同环境
> 基线 run：`diag-rule-error-20260928-113857-239b29`（11:38，inconclusive 路径，rootcause 被剪枝）
> 优化 run：`diag-rule-error-20260928-210441-2f30ea`（21:04，**hit 路径**，rootcause 真实运行 + 合成报告）

## 1. 基线诊断（优化前的实测）

基线 5 个 worker 会话：**259 次 API 调用、668K 未缓存输入、25.27M 缓存重读、190K 输出**，端到端 **49m33s**。

三个根因（全部实证，编排开销仅 ~41s 不是问题）：

1. **每轮 prompt 底座 ~108K tokens**：~20 个 toolsets 的工具 schema + 整篇 SKILL.md 注入
   （log-analyzer 1167 行）+ memory/kanban 指引，每轮全量重读（~96% 走缓存但 volume 仍在）；
2. **轮次无预算**：dir-cap 71 次、report 60 次调用；dir-log 的 16 节点 `find` 全量扫描——
   工具输出占会话消息内容的 99%；
3. **report 是 23 分钟独立 worker 会话**，而其工作本质是"汇总已有证据 → 生成报告 → 回调"。

模型本身已是 flash 档（mimo-v2.6-flash），换模型不是杠杆；缓存已生效（cache_read 远大于 input）。

## 2. 改造项

| # | 改造 | 类型 |
|---|---|---|
| A | **dag-ops 瘦身 profile**：`platform_toolsets.cli` 20→6（terminal/file/i2agent/i2stream-bkn/skills/todo；kanban 由 dispatcher 强制追加）、memory 关闭、SOUL 最小化、plugins 共享目录 + 仅启用 i2stream-bkn/hermes-dag；模板 assignee→dag-ops | 配置 |
| B | **取证预算纪律**进契约附录：≤20 次工具调用；>200 行输出先落文件再摘录；禁止全量 keyspace/全盘 find dump；≥2 独立源即交卡 | 模板/编译器 |
| C | **report 折叠为合成节点**（`synthesis: report`）：advancer 汇总上游 outputs → 单次 `ctx.llm.complete_structured` → 落盘 + 可选 `callback_address` 回调 → 占位卡即时完成（与 verdict 同模式），不再开 worker 会话 | 引擎 |
| D | `kanban.dispatch_interval_seconds` 60→15 | 配置 |
| — | 顺带修复：dag-ops worker 的 hook 入队必须与 gateway 共享队列——`default_db_path()` 从 profile home 上溯到基座 home（否则事件分裂，只能靠 60s 对账） | 引擎 |

## 3. 实测对照

### 3.1 总量（5 worker 会话口径；优化 run 含 rootcause，比基线多做一段深挖）

| 指标 | 基线 | 优化 run | 变化 |
|---|---|---|---|
| API 调用 | 259 | **143** | **-45%** |
| 未缓存输入 | 668,258 | 493,885 | -26% |
| **缓存重读** | **25,266,752** | **8,603,136** | **-66%** |
| 输出 | 189,624 | 123,844 | -35% |
| 端到端时长 | 49m33s | **22m03s** | **-55%** |
| spawn 延迟（首卡） | 17–18s | 10s | -40% |
| 每轮均摊重读 | ~97.5K | ~60.2K | -38% |

### 3.2 分卡（优化 run；hit 路径）

| 卡 | calls | cacheR | 时长 |
|---|---|---|---|
| dir-log | 29 | 1.75M | 9m59s（基线 25m39s / 34 calls） |
| dir-conn | 31 | 1.52M | 9m43s（基线 9m54s） |
| dir-cap | 26 | 1.62M | 8m38s（基线 21m29s / 71 calls） |
| dir-rule | 36 | 2.05M | 12m44s（基线 16m39s / 62 calls） |
| rootcause·dir-conn | 21 | 1.66M | 10m20s（基线无此环节） |
| report | — | — | **合成即时完成**（基线 23m13s / 60 calls） |

### 3.3 质量说明（公平性）

- 优化 run 判了 **hit** 并真实运行了 rootcause（基线 inconclusive 剪枝）——**做了更多工作仍 -55% 时长 / -66% 重读**，对照偏保守。
- 命中是真实发现：dir-conn 取证到源端 `172.20.73.244→10.1.131.11:3306` 间歇性会话中断
  （29 组同刻成组异常，源库 Aborted_connects 29.2% 独立佐证）；合成报告如实标注
  "对 DAG 输入 4C0AC40B 仍未验证"——不编造闭环的纪律在合成路径同样成立（llm_synthesized=true）。
- 本 run 未传 `callback_address` → 回调按设计跳过（`callback.skipped=true`）。

## 4. 复现命令

```bash
# tokens/calls/时长 per worker（dag-ops profile 自记账）
sqlite3 -readonly "file:/root/.hermes/profiles/dag-ops/state.db?mode=ro" \
 "SELECT id,api_call_count,input_tokens,cache_read_tokens,output_tokens,CAST(ended_at-started_at AS INT) FROM sessions WHERE source='kanban' ORDER BY started_at"

# 事件时间线（runs.db）
sqlite3 -readonly "file:/root/.hermes/plugin-data/hermes-dag/runs.db?mode=ro" \
 "SELECT datetime(ts,'unixepoch','localtime'),kind FROM events WHERE run_id='diag-rule-error-20260928-210441-2f30ea'"
```

## 5. 残余与后续

- 每轮底座仍有 ~60K：hermes-dag 工具集仍随插件透传进 worker prompt（可加 `agent.disabled_toolsets: [hermes-dag]` 微调）；KANBAN_GUIDANCE/SOUL 等稳定块可再压（源码级改动，暂缓）。
- BKN 技能拆分（log-analyzer 1167 行→骨架式按需加载）属 BKN 仓库工作，收益估计每张日志卡再省 ~10K/轮——列为建议。
- stale-stream（基线中 181s 事件）为 provider 稳定性问题，插件侧无法根治。
- 基线 run 为 inconclusive（该环境本就查无此规则），优化 run 挖出真实连接问题——两 run 输入相同、环境状态不同，轮次差异亦受此影响；对照以总量口径为准。
