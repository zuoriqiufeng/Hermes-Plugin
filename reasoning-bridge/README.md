# reasoning-bridge

Hermes 观察者插件：把模型的深度思考（reasoning）增量落到按会话隔离的
JSONL 文件，供 i2Agent tail 后经 SSE 转发给 i2Console 展示。

- **零本体改动**：只用 Hermes 官方插件 hook 机制，hermes 升级后插件保留
- **纯观察者**：插件/文件桥任何故障只丢思考展示，不影响对话主流程
- **display-only**：不持久化思考过程（刷新后思考区块消失是预期行为）

设计文档：`/hdd/demo/public/build_doc/integration/chat/reasoning-bridge.md`

## 工作原理

```
模型 delta.reasoning_content
  → agent._fire_reasoning_delta（hermes 本体）
  → on_stream_delta hook（kind=="reasoning"）
  → 本插件追加写 data/{session_id}.jsonl
  → i2Agent tail（100ms 轮询）→ SSE data: {"reasoning":"..."}
  → i2Console 气泡上方「✻ 深度思考」折叠区块
```

前提：`~/.hermes/config.yaml` 设 `plugins.stream_reasoning_deltas: true`
（默认 false；为 false 时 reasoning 增量不会投递到任何插件 hook）。

## JSONL 协议

路径：`{HERMES_HOME}/plugins/reasoning-bridge/data/{session_id}.jsonl`

每行一个完整 JSON 对象（UTF-8，`\n` 结尾）：

```jsonl
{"t":"start","turn":"c393ee7e","ts":1789012345.1}
{"t":"delta","turn":"c393ee7e","ts":1789012345.4,"text":"用户报的是 ALTER TABLE 4046，"}
{"t":"delta","turn":"c393ee7e","ts":1789012345.6,"text":"先查目标端表结构是否漂移……"}
{"t":"end","turn":"c393ee7e","ts":1789012352.0,"finished":true}
```

写入端约束（本插件实现）：

- 每行一次 `write()`（O_APPEND 追加）+ 批末 flush
- `text` 超 64KB 时切片成多行 delta（读端按到达顺序拼接）
- 单文件 5MB 上限：达到后停止写入并只 warn 一次
- `session_id` 白名单校验（`[A-Za-z0-9]` 开头，其后 `[A-Za-z0-9._-]*`），
  非法/空 id 的 payload 丢弃并 warn
- 24h housekeeping：借 `on_stream_start` 事件节流（≥60s 间隔）清理超龄
  `.jsonl` 并释放对应缓存句柄
- 所有异常只 `logger.warning` 后吞掉——绝不影响对话

## 部署

```bash
# 1. 拷贝插件目录
cp -r reasoning-bridge ~/.hermes/plugins/

# 2. 打开 reasoning 增量投递（~/.hermes/config.yaml）
#    plugins:
#      stream_reasoning_deltas: true

# 3. 启用插件
hermes plugins enable reasoning-bridge

# 4. 重启 hermes gateway（进程级配置，必须）
#    重启会中断在途对话，安排在无对话时执行

# 5. 验证：对话一轮后
cat ~/.hermes/plugins/reasoning-bridge/data/*.jsonl
```

i2Agent 侧配套配置（见设计文档 §六）：

```yaml
analyzer:
  hermes:
    reasoning_dir: /root/.hermes/plugins/reasoning-bridge/data   # 空 = 功能关闭
```

## 测试

```bash
cd reasoning-bridge
python3 -m pytest tests/ -v
```

覆盖：hook 注册、行格式、kind 过滤（`text` 不写）、空/非法 session 丢弃、
JSON 转义往返、64KB 切片、5MB 上限（含 warn 去重）、housekeeping（超龄
删除 + 60s 节流）、写失败与内部异常的吞没。

## 故障语义

| 状态 | 行为 |
|------|------|
| 模型不输出 reasoning（如 mimo-v2.5-pro） | jsonl 只有 start/end 或为空 → 前端无区块，对话正常 |
| `stream_reasoning_deltas: false` / 插件未启用 | 零写入，老部署零影响 |
| i2Agent `reasoning_dir` 为空/目录不存在/文件缺失 | 全链路静默 |
| 磁盘/编码故障 | hook 内吞掉并 warn 一次，对话不受影响 |
