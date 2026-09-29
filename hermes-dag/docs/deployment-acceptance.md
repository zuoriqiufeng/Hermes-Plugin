# hermes-dag 部署与验收记录

> 日期：2026-09-28
> 环境：Hermes 0.21.1（gateway 以 systemd 用户服务 `hermes-gateway.service` 运行，hermes_home=/root/.hermes）
> 结论：**全链路验收 PASS**——会话触发的真实 run 从建图到报告完整闭环（含条件剪枝与诚实降级）。

---

## 1. 部署步骤（已执行）

| 步骤 | 内容 | 结果 |
|---|---|---|
| 修复① | 模板 6 个 worker 节点加 `assignee: default`（dispatcher 对无 assignee 卡静默跳过，`skipped_unassigned`）；verdict 即时卡刻意不盖 | ✓ 沙箱验证：方向卡 assignee=default，verdict 卡=None |
| 修复② | `plugin.yaml` config_schema 改 Hermes 扁平格式（原 JSON Schema 风格被跳过并每次启动告警） | ✓ 重启后告警消失 |
| 修复③ | **webhook/advancer 限定 gateway 进程**（argv 连续对 `gateway run` 门控）+ 绑定短重试 | ✓ 见 §2，根因与修复详见文末 |
| 建板 | `hermes kanban boards create i2stream-ops` | ✓ `/root/.hermes/kanban/boards/i2stream-ops/kanban.db` |
| 配置 | `hermes config set plugins.entries.hermes-dag.settings.webhook_token <32B> ` + `settings.board=i2stream-ops` | ✓ |
| 重启 | `hermes gateway restart`（SIGUSR1 优雅排空） | ✓ PID 2454333，插件注册 `gateway=True, advancer=True` |

## 2. Webhook 验收（无副作用检查）

| 检查 | 结果 |
|---|---|
| `GET /health`（免鉴权） | `{"ok": true, "plugin": "hermes-dag"}` ✓ |
| `POST /dag/run` 无 token | HTTP 401 `missing or invalid X-Dag-Token` ✓ |
| `POST /dag/run` 错 token | HTTP 401 ✓ |
| 监听确认 | `ss`：`0.0.0.0:8620` LISTEN（pid=gateway）✓ |

## 3. 会话触发验收（M3 锚点，真实 run）

**触发**：`hermes chat -q "规则 4C0AC40B 报错了，你看看日志，发生了什么情况。请调用 dag_run…"`，
模型调用 `dag_run(dag=diag-rule-error, params={rule_id:4C0AC40B, error_code:CDB6157A, symptom:规则报错},
idempotency_key=rule-error:4C0AC40B:20260928)` → run `diag-rule-error-20260928-113857-239b29`。

**完整事件时间线**（runs.db，全程 11:38:57 → 12:28:33）：

```
11:38:57 run_created            params={rule_id:4C0AC40B, error_code:CDB6157A, symptom:规则报错}
11:38:58 card_created × 4       首层四方向卡（assignee=default，skills 已盖章）
11:49:13 node_done dir-conn     verdict=inconclusive（etcd 864键/BKN/Qdrant/i2Agent 全 0 命中）
11:55:56 node_done dir-rule     verdict=inconclusive（控制台 192.168.3.104:58086 连接被拒；etcd 185 规则无此规则）
12:00:46 node_done dir-cap      仅排除容量签名，全量进度缺证
12:04:56 node_done dir-log      全部可达节点无日志目录；9 个 agent 断连
12:05:06 card_created verdict   即时卡
12:05:07 verdict_done           judge: {"verdict":"inconclusive","hits":[],"reason":"四个方向均为inconclusive…按宁可误报不可漏报判inconclusive"}
12:05:07 card_created rootcause → 12:05:08 node_pruned (condition_false) → archived 占位卡 t_0ce07707
12:05:08 card_created report
12:28:33 node_done report → run_completed
```

**卡片终态**：4 方向 done + verdict done + rootcause **archived**（剪枝占位）+ report done。

## 4. 关键行为验证（逐项）

| 设计行为 | 验收证据 |
|---|---|
| 会话自决进图 + 幂等键 | run 实际建成，params 透传 ✓ |
| 首层编译 + assignee 派发 | 4 worker 进程 argv 实测：`-p default --skills i2stream-log-analyzer --toolsets …,hermes-dag,i2agent,i2stream-bkn,…` ✓ |
| worker 只读取证（BKN→Qdrant 纪律） | evidence 含 BKN 原始输出、etcd 键空间扫描、Qdrant enrichment、i2Agent 诊断历史 ✓ |
| 推进器三层 | **hook 在 worker 进程真实生效**（dir-rule 卡完成→node_done 间隔 ~4s，远超 60s reconcile 语义）；sync/对账兜底未需触发 ✓ |
| verdict 即时卡 + judge 口径 | inconclusive ≠ clean 被 judge 显式引用；hits 为空、rootcause 剪枝 ✓ |
| 剪枝占位卡满足扇入 | rootcause archived，report 正常建卡执行 ✓ |
| report 无条件出口 | 六维度报告 + payload 落 `/hdd/demo/public/reports/diag-rule-error-20260928-113857-239b29/` ✓ |
| 诚实降级（fail 不静默） | 回调被上游 API 拒（400 invalid analysis_id，本 run 无 analyses 记录）→ `report_sent=false` 如实记录并附原因 ✓ |
| LP sink 口径 | verdict=inconclusive → **不产候选**（patterns-pending 未创建）✓ |

## 5. 附带的真实发现（worker 现场取证产出）

- 控制台 `192.168.3.104:58086` 连接被拒（curl code=000）；
- 节点 houjr202 无 track 服务（-4030）；**9 个 agent 断连**；
- 输入疑点：`error_code=CDB6157A` 实为**另一规则的 UUID 前缀**，按错误码检索全落空——上游抽取环节的建议修正。

## 6. 部署过程中发现并修复的两个真 bug

1. **短命进程抢占 webhook 端口**：插件 register() 在每个加载插件的 hermes 进程执行——`hermes gateway restart` 客户端进程抢先绑定 0.0.0.0:8620 并持有整个重启等待期，导致**真正的新 gateway** webhook 以 `EADDRINUSE` 失败、客户端退出后端口空置。修复：webhook 与 advancer 均以 argv 连续对 `("gateway","run")` 门控，只在长驻 gateway 启动（同时消除临时进程消费队列事件后即退出的隐患）；绑定加 3×2s 短重试。单测 `test_is_gateway_process` / `test_webhook_bind_conflict_retries_then_gives_up` 锁定。
2. **无 assignee 卡静默不派发**：dispatcher 对 assignee 为空的 ready 卡进 `skipped_unassigned`（无日志、无告警），模板不盖 assignee 则整图永不推进。修复：模板 worker 节点盖 `assignee: default`；verdict 即时卡保持无 assignee（顺带消除「建卡与派发竞态」）。

## 7. 观察项与残余说明

- 本 run 总耗时 **~50 分钟**（四方向取证 10–26 分钟/卡 + report 23 分钟）——本次是"控制台不可达、多节点断连"的恶劣场景，探测面大；常态应短于此。
- 会话客户端在验收中被本机 8 分钟超时掐断（模型建图后又自行展开排查）；run 由 gateway 独立推进至完成，**不受影响**——恰好验证了"图跑完自己走"的哨兵属性。
- 回调目标（analysis_id）被上游 API 拒绝属环境侧问题（该 run 未在上游注册 analyses 记录），引擎侧行为正确（如实记录 `report_sent=false` + 原因）。
- 单测 21/21、冒烟 PASS（含 webhook 步骤），部署后据此记录归档。

## 8. 后续门控与效率加固（2026-09-28/29 追记）

- **api_server 门控**（2026-09-29）：日常 i2Console REST 会话不再暴露 `dag_*` 工具——
  `known_plugin_toolsets.api_server: [hermes-dag]`（config.yaml）。验证：`_get_platform_tools(cfg, "api_server")`
  解析无 hermes-dag、`cli` 平台保持可见；webhook/advancer/hooks 不依赖工具可见性，不受影响。
  ⚠ caveat：交互式 `hermes tools` 选择器保存时会重写 `known_plugin_toolsets[api_server]`，
  届时保持 api_server 下 hermes-dag 不勾选即可。
- **效率二期**：dag-ops 瘦身 profile + 取证预算 + report 合成节点 + dispatch 15s +
  rootcause 内联（模板 v2）——实测 49m33s → 15m13s（-69%），详见 [perf-optimization.md](perf-optimization.md)。
