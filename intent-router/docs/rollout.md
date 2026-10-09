# 上线路线（影子 → 提示 → 直连 → 裁剪）

> **当前状态：shadow only（2026-10-09 决定）。**
> 只开观测档，hint / act / 裁剪 / 第三层全部关闭——配置上无需改动，插件默认档位就是如此。
> 理由与重启各功能的前置条件见 [decision-shadow-only.md](./decision-shadow-only.md)：
> 本场景是 Case B（意图识别本来就在主模型的工具选择里，没有独立分类调用），
> 且按轮裁工具面与本系统的前缀缓存冲突。**先采一周数据再谈放档。**

原则：**每一档都独立可验收、可回退，且上一档的数据是下一档的准入条件。**
不要跳过 shadow 直接开 act——那等于用真实会话去试词表的准确率。

## P0 部署 + 影子模式（当前状态：已完成）

```bash
ln -s /hdd/demo/public/hermes-plugin/intent-router ~/.hermes/plugins/intent-router
hermes plugins validate intent-router     # 应全绿、无告警
hermes plugins enable intent-router
hermes plugins doctor intent-router       # 真实运行时契约校验
# 重启 gateway 后生效（涉及进程级加载）
```

配置保持默认（`mode: shadow`）。**验收标准**：

- 行为零改动：同一句话在装插件前后回答无差异（shadow 不注入任何东西）；
- 日志有东西：`~/.hermes/plugin-data/intent-router/decisions.jsonl` 出现 `boot` 与 `decision` 行；
- boot 行里 `warnings: []`、`lexicon_drift: []`。

已实测：boot 记录 `intents=9 rules=7 keyword_terms=105 mode=shadow`，
`/intent doctor`、`/intent stats`、`/intent tune` 经真实命令注册表分发正常。

## P1 开提示（hint）

```yaml
plugins:
  entries:
    intent-router:
      settings:
        mode: hint
```

只注入一行意图提示（少量 token），不改工具面、不动工具。**验收标准**：

- `/intent stats` 里 `actions.hint` 占比与 `classified_rate` 相符；
- `injected_chars` 均值在几十字符量级（若明显偏大，说明有意图的注入块写得太啰嗦）；
- 用 watcher（`/hdd/demo/public/watcher`）对比改前改后的**:每轮 token 与端到端延迟**，
  确认「少花路由推理」这个假设成立。**这一步的数据是 P2 的准入条件**——
  如果 hint 没有可测收益，就没必要冒险开 act。

## P2 开直连（act，小范围）

先只放一个意图、一个工具（例如错误码查询 → `diagnose_error`）：

```yaml
plugins:
  entries:
    intent-router:
      settings:
        mode: act
        auto_enabled: true
        auto_allow_tools: [diagnose_error]   # 只读工具，逐个加
```

**验收标准**：

- `/intent stats` 里 `acted` 命中预期的意图，且 `tools.allow` 范围外的意图**没有**被自动执行；
- 该意图上该请求的**额外工具调用降为 0**，主模型只发生 1 次请求（对照 watcher 的 tool_call 数）；
- 端到端延迟与 token 相比 P1 再降一档，且回答质量没有下降（人工抽查 10 条真实会话）；
- 出现任何「答非所问」，先回退到 hint，再查词表（**不要**直接加白名单扩大范围）。

回退：把 `mode` 改回 `hint`（或 `auto_enabled: false`）即可，无需改代码。

## P3 开工具面裁剪 + 接第三层小模型

```yaml
plugins:
  entries:
    intent-router:
      settings:
        prune_enabled: true
        prune_keep_tools: [terminal, read_file, write_file]   # 核心工具留好，别裁瘸 agent
        tier3_enabled: true
auxiliary:
  intent_router_classify:
    model: <廉价小模型>
    timeout: 8
```

**裁剪是本插件唯一可能削弱 agent 能力的动作**，所以它的准入是数据而不是直觉：
先看 shadow/P1 的日志里，被判为某意图的那些轮次**实际调用了哪些工具**——
如果清单正好被意图的 `allowed_tools` 覆盖，才把它加进 `prune_enabled` 的适用面。

**第三层**的准入是残差占比：`/intent stats` 里如果 `gate_reasons` 的
`score_below_min / insufficient_evidence / margin_too_small` 合计占比很低
（例如 < 10%），说明词表已经够用，接小模型是浪费；反之再接。

**验收标准**：单轮请求里 `tools` 条数下降且 `chars_saved` 与预期一致；
第三层占比与它的 token 花费可接受（目标：触发率 < 15%、token < 本轮 5%）。

## P4 对外接口（可选）

- `expose_tool: true`：把 `intent_classify` 工具交给模型自己按需咨询
  （会给主提示增加工具 schema，用收益换 token，谨慎开）；
- 消费 `intent-router:classified` 事件：hermes-dag 可据此实现「进图闸门」
  （单点问答 vs 多方向诊断），把 perf 文档里预留的 triage 前置裁决落地。

## 每档的通用回退与观察面

| 现象 | 动作 |
|---|---|
| 回答质量下降 / 答非所问 | `mode: shadow`（一键静默），保日志继续采数据 |
| 日志里出现 `exception` / `tool_failed` 门控原因 | 看 warn 日志；插件是 fail-open，不会影响主流程，但要查根因 |
| 词表漂移告警（`/intent doctor`） | 重跑 `python3 tools/gen_lexicon.py` 后复查语料回归 |
| 想临时关掉但保留命令 | `enabled: false`（`/intent` 仍可用，便于现场诊断） |
