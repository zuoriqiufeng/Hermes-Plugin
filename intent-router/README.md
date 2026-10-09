# intent-router

分层意图识别插件：**能用正则判的不用算法，能用算法判的不问大模型**，并且把识别结果
直接变成动作——高置信时替主模型把只读工具调好、把结果注入本轮上下文，主模型一次请求
就能作答。默认 **shadow 档位**：只记录、不注入、不调工具，行为与不装插件完全一致。

```
用户消息
  │
  ├─ 第 0 步  归一化（NFKC/全角/破折号/零宽字符）+ 实体抽取（错误码族/IP:端口/规则号/库名）
  │
  ├─ 第一层  正则：有序规则表 + 追问护栏                     ← 意图明确的输入，命中即定
  │
  ├─ 第二层  Aho-Corasick 词典扫描 + 字符二元组 BM25         ← 换个说法也能认
  │           + 三重门控（阈值 / margin / 独立证据条数）
  │
  ├─ 第三层  大模型（auxiliary 小模型槽，只吃前两层不确定的残差）
  │
  └─ 动作层  shadow（只记录）| hint（注入提示）| act（直连只读工具 + 注入结果）
```

## 为什么这样设计（两个关键约束）

1. **大模型那一步太贵、太慢**。所以前两层必须**尽量多地**吃掉流量，第三层只吃残差，
   且触发策略默认 `on_ambiguous`（只有「有本地证据但不敢下结论」才问）。
2. **Hermes 对普通文本消息没有零模型调用的回复通道**。插件能做的只有三件事：
   往本轮用户消息里注入上下文、改 provider 请求（含工具面）、走斜杠命令。
   所以本插件的收益不是「不调模型」，而是「**少一轮工具循环 + 少花路由推理 + 少带无关工具**」。
   伪造 provider 响应那条路（`llm_execution` 中间件）**刻意不做**——它依赖内部契约，
   Hermes 升级就可能失效。

## 配置

写入 `~/.hermes/config.yaml` 的 `plugins.entries.intent-router.settings`：

```yaml
plugins:
  entries:
    intent-router:
      settings:
        mode: shadow              # shadow | hint | act（默认 shadow，先量准确率再放开）
        mode_overrides: {cli: shadow, api_server: act}   # 可按面覆盖
        surfaces: []              # 生效面白名单（cli/gateway/api_server 或原始 platform）；留空=全部
        min_score: 0.62           # 第二层接受阈值
        margin: 0.15              # top1 与 top2 的最小分差（歧义就闭嘴）
        min_evidence: 2           # 独立证据条数下限（关键词/BM25/实体 各算一条）
        tier3_enabled: false      # 是否启用第三层大模型
        tier3_policy: on_ambiguous
        auto_enabled: false       # act 总闸
        auto_allow_tools: []      # 允许被自动调用的只读工具白名单（留空=一个都不许）
        prune_enabled: false      # 是否按意图裁剪本轮工具面
        prune_keep_tools: []      # 裁剪时永远保留的工具
        expose_tool: false        # 是否把 intent_classify 工具暴露给模型
        # ── 可读日志（与 decisions.jsonl 双轨，详见 docs/logging.md）──
        log_enabled: true         # 总开关
        log_dir: ""               # 留空 = <基座 home>/plugin-data/intent-router/logs；可给绝对路径
        log_level: info           # debug 才记「未表态/门控细节」；debug 明细只进插件文件
        log_max_bytes: 5242880    # 单文件 5MB，满后新开同日下一个分片
        log_backups_per_day: 3    # 同日分片上限（.1/.2/.3），超出停止写入并告警一次
        log_retention_days: 7     # 按文件名日期保留 7 天
        log_decisions: true       # 是否逐条记决策行
        log_preview_chars: 120    # 输入预览字符数；0 = 完全不落原文（只留哈希）
        log_redact: true          # 落盘前强制脱敏（同时作用于 decisions.jsonl）
        log_include_pid: false    # 行首带进程号，多进程并发排查时开
```

第三层想接**廉价小模型**（强烈建议）：

```yaml
auxiliary:
  intent_router_classify:
    model: <小模型名>
    timeout: 8
```

> 完整键位与默认值见 `plugin.yaml` 的 `config_schema`（`tests/test_manifest_contract.py`
> 会校验清单默认值与代码默认值一致，防止两处漂移）。

## 部署与验证

```bash
ln -s /hdd/demo/public/hermes-plugin/intent-router ~/.hermes/plugins/intent-router
hermes plugins validate intent-router     # 清单+register() 隔离校验，应全绿无告警
hermes plugins enable intent-router
hermes plugins doctor intent-router       # 真实运行时契约校验
# 涉及 gateway 的配置改动需重启 gateway 才生效
```

验证是否真的在跑（shadow 模式零风险）：随便发一句话，然后

```bash
# 人读日志（按日期 + 按大小双轮转，文件名带日期）
tail -n 5 ~/.hermes/plugin-data/intent-router/logs/intent-router-$(date +%F).log
# 机器日志（/intent stats、/intent tune 的数据源）
tail -n 3 ~/.hermes/plugin-data/intent-router/decisions.jsonl
```

会话内可用（斜杠命令，零 LLM 除显式要求）：

| 命令 | 作用 |
|---|---|
| `/intent <文本>` | 跑完整阶梯，打印层级/意图/分数/证据/门控原因/耗时 |
| `/intent why` | 回看最近一次识别与动作决策（含 shadow 下的「假如放开会做什么」） |
| `/intent stats [n]` | 聚合最近 n 条日志：层分布、第三层占比、延迟分位、注入字符量、裁剪节省 |
| `/intent log [n]` | 看插件日志最近 n 行（默认 40）——不用 shell 也能排查 |
| `/intent tune` | 在标注语料上扫阈值网格，输出「无错判 + 覆盖率」最优组合 |
| `/intent doctor` | 自检：意图/规则/词条计数、词表漂移、告警、日志与配置、当前档位 |
| `/intent wrong <意图id>` | 记录一次误判到 `corrections.jsonl`，供补语料 |

日志的形态、脱敏口径、轮转与保留、多进程语义、失败处置，以及三个典型排查动作
（「为什么这么判」「为什么没直连工具」「日志怎么不写了」）见 **[docs/logging.md](./docs/logging.md)**。

## 意图表与词表

- 内置业务表：`data/taxonomy/ops-diag.yaml`（i2Stream 运维域 9 个意图，与 i2stream-bkn 的
  12 工具对齐）。
- 覆盖顺序（后者覆盖前者，同 id 覆盖、新 id 追加）：
  内置表 → 配置 `taxonomy` 指定文件 → `<plugin-data>/intent-router/taxonomy.d/*.yaml`。
  现场调整走第三种，不必改仓库。
- 词表快照：`data/lexicon/ops-bkn.yaml`，由 `tools/gen_lexicon.py` 从 i2stream-bkn 的
  `constants.py` 生成，**带源文件 sha256**。源词表变了 `/intent doctor` 会报漂移，
  重跑脚本即可；插件运行时只读快照，**不跨仓 import**（对方重构不会打断本插件加载）。
- 词表组里的宽泛说法可用 `exclude` 剔除，例如内置表把 crash_loop 组里的
  `报错/出错/失败` 摘掉——它们几乎出现在一切异常描述里，留着会把任何报错消息拉向「崩溃循环」。

## 动作层的五重准入（act 档位）

直连工具是**唯一会产生副作用**的能力，所以要求**全部满足**才执行，缺任何一条降级为 hint：

1. 意图声明 `action: act` 且 `tool_binding.safe_auto: true`（词表作者显式授权只读自动执行）；
2. 工具名在 `auto_allow_tools` 白名单内（运维显式开启，默认空表）；
3. `auto_enabled: true`（再一道总闸）；
4. 判定来自第 1/2 层（本地确定性证据）——除非显式打开 `tier3_allow_act`，**模型判断不允许触发自动执行**；
5. `requires` 声明的实体/槽位齐全，且最近历史里没有同工具的调用痕迹（去重护栏）。

执行本身还有两道护栏：**硬超时**（意图级 `timeout_ms` 优先，默认 1500ms；用 daemon 线程
+ Event 实现，超时即放弃，绝不拖住对话）与**结果截断**（默认 4000 字符）。工具输出按
「数据非指令」注入，分隔符 `<FIELD_TEXT>` 会先做清洗，防止工具内容越界伪造指令段。

## 失败语义（fail-open）

- 任何一层异常 → 记日志、本轮等同未装插件（`pre_llm_call` 返回 None）；
- 第三层超时/异常/schema 不符 → 判「无意见」，绝不阻断；
- 直连工具失败/超时/空结果 → 降级为 hint；
- 工具面裁剪任何异常 → 放行原请求；
- 遥测写失败 → warn 一次后吞掉。

## 文件地图

| 文件 | 职责 |
|---|---|
| `__init__.py` | `register(ctx)`：装配 + 注册 hook/中间件/命令/工具 |
| `config.py` | 设置读取与默认值（`config_schema` 的代码侧） |
| `normalize.py` / `entities.py` | 第 0 步：归一化、实体抽取 |
| `regex_tier.py` | 第一层：规则编译/校验/ReDoS lint、追问护栏 |
| `aho.py` / `bm25.py` / `scorer.py` | 第二层：多模式匹配、相似度、打分与三重门控 |
| `taxonomy.py` | 意图表加载/合并/校验、词表组展开、索引构建 |
| `router.py` | 三层阶梯编排（纯识别，不做动作） |
| `llm_tier.py` | 第三层：枚举 schema、prompt、缓存、fail-open |
| `decision.py` / `state.py` | 决策记录模型；轮次决策暂存 |
| `actions.py` / `prune.py` | 动作层：直连工具与注入块；工具面裁剪中间件 |
| `safety.py` | 落盘前脱敏（`redact_text`）与注入前清洗（`sanitize_data`） |
| `logbook.py` | 插件可读日志：日期+大小双轮转、保留清理、单行格式化 |
| `telemetry.py` / `corpus.py` | 决策日志（JSONL）与统计；语料加载与阈值扫参 |
| `tools.py` / `commands.py` | `intent_classify` 工具；`/intent` 命令族 |
| `tools/gen_lexicon.py` | 开发期：从 i2stream-bkn constants 生成词表快照 |

运行时状态（决策日志、可读日志、现场覆盖的意图表、误判修正）都在
`~/.hermes/plugin-data/intent-router/`（可读日志在 `logs/` 子目录，也可用 `log_dir` 指到别处），
插件目录只放只读资产。

## 测试

```bash
python3 -m pytest tests/ -q          # 138 项：算法对账、门控边界、越权守卫、语料回归、清单契约、日志轮转/脱敏
python3 tools/gen_lexicon.py --check # 词表快照漂移检查（源不在本机时跳过）
```

语料回归的硬约束：**本地两层表态了就必须正确**（precision 100%），标注为「不该表态」
的样本必须闭嘴；覆盖率有下限（0.90）但不追求 100%——剩下的残差本来就该交给第三层。
