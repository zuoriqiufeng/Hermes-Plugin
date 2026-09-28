# M0 地基验证记录 + 节点契约表

日期：2026-09-25
环境：Hermes Agent v0.21.1（/usr/local/lib/hermes-agent），沙箱库 `/tmp/hermes-dag-m0/kanban.db`
（`HERMES_KANBAN_DB` 钉定，gateway 调度器不可见，无 worker spawn 干扰）

## 一、六项地基实验（全部通过）

| # | 实验 | 命令要点 | 结果 |
|---|---|---|---|
| 1 | 多父扇入：全部父卡 done 才 ready | `create --parent P1 --parent P2` + 分别 complete + `kanban dispatch` | ✓ 单父 done 时 child=todo；双父 done 后 child=ready |
| 2 | **archived 父卡释放子卡**（剪枝基石） | `kanban archive P` → dispatch | ✓ 父卡 archive（未 done）后子卡立即 todo→ready |
| 3 | `kanban complete --summary --metadata` 结构化交接 | `complete T --summary s --metadata '{"verdict":"hit",...}'` | ✓ summary+metadata JSON 完整落 closing `task_runs` 行 |
| 4 | 幂等键去重 | 同 `--idempotency-key` create 两次 | ✓ 返回同一 task id，无重复卡 |
| 5 | unclaimed 卡直接 complete（verdict 虚拟卡路径） | `create` 后不经 claim 直接 `complete` | ✓ ready→done；且父卡全 done 时新建子卡初始即 ready（CAS 允许列表内） |
| 6 | `kanban swarm` 参考拓扑 | `swarm --worker ... --verifier ... --synthesizer ... --json` | ✓ 返回 `{root_id, worker_ids, verifier_id, synthesizer_id}`；root 立即翻 done 作黑板；worker→verifier→synthesizer 链式 parents |

### 对引擎设计的直接影响

1. **剪枝 = 子树 archive**：实验 2 证明 archived 满足扇入（`kanban_db.py:2038`，"done OR archived"），
   文档二 §7 的"剪枝扇入修正/移除父边"整块不需要——条件为假的节点从该节点 DFS archive 子树即可，
   Kanban 自己把下游提升起来。单测只需覆盖"archive 后下游确实 ready"。
2. **verdict 虚拟卡**：实验 5 证明 create→立即 complete 合法（never-claimed 卡合成 run）。
   推进器在父卡全 done 后：judge → create verdict 卡（父卡全 done 故初始 ready）→ complete(metadata=verdict outputs)
   → Kanban 原生提升下游。图在 kanban.db 里完整可审计。
3. **根卡初始状态是 ready**（非 todo）：无父卡即 ready。编译首层方向卡后无需手动 promote。
4. **`kanban show --json` 顶层结构**：`{task:{status,...}, latest_summary, parents, children, comments, events, runs:[{id,summary,metadata,...}]}`；
   `kanban list --json` 为扁平数组（含 status，不含 parents）——dag_status 用 list 拉全量 + show 拉单卡详情。
5. **待 M2 实测**：`kanban_task_completed` hook 在 CLI/worker 子进程触发时插件代码能否收到（设计上队列跨进程，
   且 `on_kanban_dispatch_tick` 60s 对账兜底在 gateway 侧必有）。

## 二、四方向节点契约表 v1（diag-rule-error 模板）

方向卡输出 schema（所有方向统一，盖章进 body）：

```json
{
  "verdict": "hit | miss | inconclusive",
  "evidence": ["原始日志行 / 命令输出摘录 / Qdrant 命中参数（必须含 enrichment 细节）"],
  "artifacts": ["/hdd/demo/public/reports/<run_id>/... 可选"],
  "direction_rank": 0
}
```

| 节点 | 类型 | BKN 工具入口 | skill 指针（命令唯一来源） | 命中判据（body 写死，思路≠结论） |
|---|---|---|---|---|
| `dir-log` 日志方向 | exec | `diagnose_error(error_code)` → diagnosis/suspected_process/dimensions；`search_qdrant` enrichment | `i2stream-log-analyzer`（grep -a track/load/iaback、iadebug track --rule、ps/\|/proc） | 日志中出现与症状匹配的 ERROR 行 / checksum 不一致 / 位点停滞 |
| `dir-conn` 连接方向 | exec | `diagnose_db_link(symptom=connection_error, db_type=auto, error_code)` → log_navigation/skill_context/knowledge_context | `i2stream-db-diagnostics`（骨架 + {db}.md + references/db-checkpoints.md + scripts/） | 连接类错误码存在 / 端口或认证失败证据 |
| `dir-cap` 容量资源方向 | exec | `resolve_relation(source_entity="symptom:...", relation_type="diagnosis_path")`；`search_qdrant` | `i2stream-node-inspection`（ps/ss/df//proc/coredumpctl 只读清单）；`i2stream-full-dump-monitoring`（全量完成标记） | 磁盘水位越线 / 进程 RSS 异常 / 全量未完成阻塞增量 |
| `dir-rule` 规则配置方向 | exec | `diagnose_error` 补充；`query_product(aspect=...)` 按需 | `i2stream-rule-manager`（只读动作：list_sync_rules / get_sync_rule_info + curl 认证检查） | 规则状态机异常（STOPPED/ABNORMAL）/ 配置与预期漂移 |
| `verdict` 裁决 | verdict（非 worker） | judge 输入 = 四卡 outputs | —（`ctx.llm.complete_structured`） | 汇总判定 `{verdict: clean\|hit\|inconclusive, hits:[方向id], reason}`；**inconclusive ≠ clean** |
| `rootcause-<dir>` 根因 | exec（when=hit） | 同命中方向工具集 | 命中方向 skill + `multi-evidence-diagnosis`（≥2 独立证据源、五问证伪） | 证据链闭合：现象→证据→根因→影响→建议 |
| `report` 报告 | exec（无条件） | — | `i2stream_report_sender`（send_report.py，`--analysis-id <run_id>`，必填 summary/root_cause/impact/suggestion） | 回调成功 + 会话回复 |

### 纪律（盖章进每张方向卡 body）

- 只读命令白名单：grep/cat/ps/ss/ls/df/stat/etcdctl get/iadebug 只读子命令/i2Stream 工具优先
- 先 BKN 后 qdrant；evidence 必须含 Qdrant enrichment 细节；≥2 独立证据源
- BKN 工具返回刻意不含具体命令（`_filter_command_fields`）→ 具体命令从 skill 拿，不得臆造写操作
- `verdict=inconclusive` 是合法结论，不算失败；但 judge 口径 inconclusive ≠ clean（宁可误报不可漏报）
- 完成定义 = `kanban_complete` 契约字段齐全；文字汇报不算完成
