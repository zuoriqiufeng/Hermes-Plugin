# thinking-language

让模型的**深度思考（reasoning）用中文输出**的指令式 Hermes 插件。

## 背景

reasoning 全链路原样透传（模型 → hermes → reasoning-bridge JSONL → i2Agent → 前端），没有任何语言处理层；hermes 主系统提示词是英文，模型的深度思考因此大多吐英文。

本插件用 hermes 官方 `pre_llm_call` 钩子，每轮 LLM 调用前返回一条中文语言规则。该规则在 **API 调用时**注入本轮 user message——不写入会话历史、不污染持久化（契约见 hermes `agent/turn_context.py` 的 `_collect_pre_llm_call_context`）；因为不进历史，所以每轮都注入。

## 机制

```
每轮 LLM 调用 → pre_llm_call hook → {"context": LANGUAGE_RULE}
              → 注入本轮 user message（API-time only）→ 模型按中文思考
```

- **生效时机**：gateway 重启加载后，下一个 turn 立即生效（含已有会话）；已落库的旧英文 reasoning 不追溯。
- **失败安全**：hook 内任何异常只记 warning 并返回 `None`，本轮等同未装插件，绝不打断对话。
- **效果边界**：指令是强影响不是硬保证，模型可能仍偶发英文短语（术语/代码保留英文属正常）。每轮约 +40 token。

## 配置

无自定义配置。启用/禁用走 `~/.hermes/config.yaml`：

```yaml
plugins:
  enabled:
    - thinking-language   # 删掉这行即禁用
```

改指令措辞：直接编辑 `__init__.py` 的 `LANGUAGE_RULE` 常量（改后重启 gateway）。

## 部署

软链到 hermes 插件目录（不要拷贝）：

```bash
ln -s /hdd/demo/public/hermes-plugin/thinking-language /root/.hermes/plugins/thinking-language
```

然后在 config.yaml `plugins.enabled` 登记、重启 gateway。

## 测试

```bash
python3 -m pytest tests/ -v
```

覆盖：返回结构（`context` 含中文规则）、`register` 挂钩正确、异常降级为 `None`。

## 验收方法

跑一轮容易引发思考的中文复杂提问，检查 reasoning 语言：

```bash
curl -sN --max-time 420 -X POST http://127.0.0.1:8090/api/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"chat_session_id":0,"messages":[{"role":"user","content":"<复杂提问>"}]}' \
  -o /tmp/sse.txt
# 看本轮 JSONL（/root/.hermes/plugins/reasoning-bridge/data/*.jsonl 新文件）里
# delta 文本的中文占比；基线（未装本插件）同题为纯英文。
```
