# 日志与排查

本插件有两份**落盘**数据，职责不同、互不影响（双轨）：

| | 文件 | 谁看 | 内容 |
|---|---|---|---|
| 可读日志 | `<log_dir>/intent-router-YYYY-MM-DD[.N].log` | **人**（`tail`/`grep`/`/intent log`） | 每轮一行中文摘要 + 模块告警/错误 |
| 机器日志 | `<基座 home>/plugin-data/intent-router/decisions.jsonl` | **程序**（`/intent stats`、`/intent tune`） | 一行一个 JSON，含分数/候选/门控/耗时等全字段 |

两者由同一个 ``Decision`` 记录字典产出，所以**字段口径永远一致**（不会出现「JSONL 里有、文本日志里没有」）。

## 一、文件名与轮转

```
<log_dir>/intent-router-2026-10-09.log        ← 当天第 1 个分片（活动文件）
<log_dir>/intent-router-2026-10-09.1.log      ← 当天超 5MB 后的第 2 个分片
<log_dir>/intent-router-2026-10-09.2.log
```

- **按日期轮转**：跨本地零点后的第一条日志自动落到新日期文件；
- **按大小轮转**：单文件达 `log_max_bytes`（默认 5MB，与宿主 `agent.log` 同量级）即新开同日下一个序号分片；
- **同日分片上限** `log_backups_per_day`（默认 3）：超出后**停止写入并只告警一次**，不会无限增长；
- **保留** `log_retention_days`（默认 7 天）：文件名带日期，轮转时清理更早的分片；
- **文件排序**：按（日期, 分片序号），所以 `...09.log` 排在 `...09.1.log` 之前，符合直觉。

默认目录 `<基座 home>/plugin-data/intent-router/logs`（与 `decisions.jsonl` 同地）。
`log_dir` 设成绝对路径即可改到任意位置，例如：

```yaml
plugins:
  entries:
    intent-router:
      settings:
        log_dir: /var/log/i2stream/intent-router
```

> **多进程**：gateway / CLI / profile worker 会**写同一份**（目录上溯到基座 home，避免 profiles 把日志劈成几份）。
> 实现上只追加、**从不重命名**，每行一次 `write`（O_APPEND 下单行不会交错），
> 所以两个进程最多同时追加到同一个分片，不丢数据。要区分是哪个进程写的，开 `log_include_pid: true`。

## 二、行格式

```
2026-10-09 10:23:45 INFO    决策 意图=diag_error_code 层=regex 分数=1.00 证据=3 动作=hint 注入=0 用时=0.08ms 会话=mdv2s1u0 模式=shadow | 输入=4073 是什么意思
2026-10-09 10:23:47 INFO    决策 意图=diag_symptom_incremental_stuck 层=algo 分数=0.70 证据=2 实体=error_code=-4073 动作=act 工具=diagnose_db_link 工具用时=42ms 注入=1100 用时=1.2ms 会话=mdv2s1u0 模式=act | 输入=增量同步卡住了
2026-10-09 10:24:05 DEBUG   决策 结果=不表态 门控=score_below_min 候选=diag_error_code=0.41,ask_knowledge=0.39 动作=none 用时=0.15ms 模式=shadow | 输入=这个需求什么时候能上线
2026-10-09 10:24:11 WARNING intent-router: 直连工具 diagnose_error 降级为提示：tool_failed:timeout
2026-10-09 10:31:02 INFO    启动 版本=0.1.0 意图=9 规则=7 词条=105 模式=shadow 第三层=off 裁剪=off 级=info 目录=...
```

分级（对应「排查时到底要看什么」）：

| 级别 | 记什么 |
|---|---|
| **INFO** | 识别出意图的决策行（含 `shadow:` 反事实动作）、启动行、事件行 |
| **DEBUG** | 未表态、被追问护栏跳过、门控失败细节（候选分数、门控原因）——调词表时最需要 |
| **WARNING / ERROR** | 工具失败降级、意图表告警、词表漂移、日志写入失败等 |

可选字段只在有值时出现（`工具=`、`第三层=`、`注入=`、`实体=`），行尾统一 `| 输入=<预览>`。

**和宿主日志的关系（两条写入路径，刻意分开）**：

| 路径 | 内容 | 去向 | 受谁约束 |
|---|---|---|---|
| stdlib logging | 模块诊断（`log.info/warning/...`：工具失败、词表告警、识别异常、装配信息） | 本文件 + `~/.hermes/logs/agent.log`（INFO+，宿主脱敏） | **宿主的日志策略**：`logging.level`、`hermes -z` 的全局静默 |
| 直接落盘 | 决策行、启动行、事件行（工具降级、命令、裁剪） | 只进本文件 | 只有 `log_level` / `log_decisions` |

分开的原因是踩过坑：`hermes -z` 一次性运行会在 `hermes_cli/oneshot.py:182` 执行
`logging.disable(logging.CRITICAL)`（本意是不往 stderr 刷日志），而 `logging.disable`
是在 **logger 层**拦记录、handler 根本收不到——凡走 logging 的插件日志会整片消失。
插件自己的审计文件不该被宿主的输出策略关掉，所以决策行改走直接落盘；
而模块诊断保持走 logging，**尊重**宿主的静默策略（那些是控制台噪音）。
代价是：决策行不会再出现在 `agent.log` 里（要看决策就用本文件或 `/intent log`）。

由此，`log_level: debug` 的 DEBUG 明细只出现在本文件：宿主的 file handler 级别
（`logging.level`，默认 INFO）会把它过滤掉，不污染宿主日志。

## 三、脱敏（重要）

插件自有的日志文件**不受宿主脱敏保护**（`RedactingFormatter` 只套在 Hermes 自己的 handler 上），
所以本插件在落盘前自己做：`safety.redact_text`，优先复用宿主的
`agent.redact.redact_sensitive_text`（这样 `ctx.register_redaction_patterns` 注册的自定义模式也生效），
取不到时用内置模式：

| 形态 | 处理 |
|---|---|
| `password=xxx` / `token: xxx` / `api_key="xxx"` | 保留键名 → `password=***` |
| `mysql://user:pass@10.0.0.5:3306/db` | 只抹凭据 → `mysql://***:***@10.0.0.5:3306/db` |
| `jdbc:oracle:thin:scott/tiger@host` | → `jdbc:***@host` |
| `sk-…` / `Bearer …` | 整体抹掉 |

**刻意的取舍：抹凭据、不抹主机。** 日志要能回答「连的是哪台机/哪个库」——那是排查链路问题时
唯一有用的信息，把整条连接串抹掉会让日志失去价值。

顺序是固定的：**压平 → 脱敏 → 截断**。不能反过来——先截断的话，落在截断点上的密钥会被截成
半截从而绕过脱敏模式。

`log_redact: false` 可显式关掉（本地临时排查用），此时 JSONL 与文本日志都不脱敏。
`log_preview_chars: 0` 则**完全不落原文**，行尾只剩 `| 输入=#<哈希>`。

## 四、三个典型排查动作

**1. 「这句话为什么被这么判？」**

```
/intent log 50          # 会话里直接看最近 50 行（不用 shell）
/intent why             # 最近一次识别的完整决策（含动作原因与门控）
```

行里的 `门控=` 就是答案：`score_below_min`（分数不够）、`margin_too_small`（两个意图太接近）、
`insufficient_evidence`（只有一路证据）、`vetoed`（被否决模式取消）、`follow_up`（追问护栏跳过）、
`no_evidence`（词表完全没命中）。

**2. 「为什么没走直连工具，只给了提示？」**

搜 `降级为提示` 那行，原因码对应动作层五重闸：

| 原因码 | 含义 |
|---|---|
| `auto_disabled` | `auto_enabled: false`（总闸没开） |
| `tool_not_allowlisted` | 工具不在 `auto_allow_tools` 白名单 |
| `missing_required:xxx` | `requires` 声明的实体没抽到 |
| `already_called_in_history` | 最近历史里调过同一工具（去重护栏） |
| `tier3_not_allowed_to_act` | 判定来自第三层，默认不许它触发自动执行 |
| `tool_failed:*` / `timeout` | 工具执行失败或超时（已降级） |

**3. 「日志怎么突然不写了？」**

```
/intent doctor         # 看插件日志路径、级别、当前分片字节数、目录内文件数
```

常见原因与处置：

| 现象 | 原因 | 处置 |
|---|---|---|
| 目录里一个文件都没有 | `log_enabled: false`，或目录不可建/不可写（首次失败会 warn 一次，进 agent.log） | 建目录 / 改 `log_dir` / 检查权限 |
| 只写到 `.N.log` 就停了 | 同日分片数达 `log_backups_per_day`（已 warn 一次，防失控） | 调大该值或 `log_max_bytes`，或缩短保留天数 |
| 只有 INFO 没有 DEBUG | `log_level: info`（默认） | 排查期临时设 `debug` |
| 旧文件被删了 | `log_retention_days`（默认 7 天）在轮转时清理 | 需要留档就调大，或把 `log_dir` 配到归档盘 |

## 五、失败语义

日志是**旁路**，任何故障都不允许影响对话：

- 目录不可建/不可写、磁盘满 → 该行放弃写、只 warn 一次；
- handler 内部异常 **不往 stderr 打 traceback**（覆盖了 `handleError`），避免刷屏；
- 轮转/清理失败 → 继续写当前分片；
- 单行超长 → 截断到 8000 字符（防止一条超长输入撑爆日志）；
- 装配失败 → 插件其余功能照常（识别、注入、工具面裁剪都不依赖日志）；
- **`hermes -z` 等一次性运行**：宿主会全局静默 stdlib logging，决策行仍照写（直接落盘），
  模块诊断行会缺（尊重宿主静默），这是预期行为；要完整的模块诊断就在 gateway/CLI 会话里跑。
