"""schemas.py — dag_* 四工具的 JSON Schema（LLM 所见）"""

SCHEMAS = [
    {
        "name": "dag_define",
        "description": "定义/校验一个 DAG 工作流 spec（YAML）。解析、环检测、条件表达式静态检查；"
                       "validate_only=false 时落库供 dag_run 引用。返回节点数/边数/错误列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "spec_yaml": {"type": "string", "description": "DAG spec 全文（YAML）"},
                "validate_only": {"type": "boolean", "description": "true=只校验不落库",
                                  "default": False},
            },
            "required": ["spec_yaml"],
        },
    },
    {
        "name": "dag_explain",
        "description": "静态展示一个已定义 DAG 的结构（节点/依赖/条件/foreach），不执行。"
                       "供人和模型审阅流程。",
        "parameters": {
            "type": "object",
            "properties": {
                "dag": {"type": "string", "description": "DAG 名称（dag_define 时的 dag 字段）"},
            },
            "required": ["dag"],
        },
    },
    {
        "name": "dag_run",
        "description": "触发一次 DAG run：编译首层节点为 Kanban 卡并启动推进器（条件边/verdict 由插件推进）。"
                       "幂等：相同 idempotency_key 重复调用返回既有 run_id。"
                       "进图口诀（调用前自问）：多方向并行取证 / 需要审计留痕 / 高频稳定工序，才值得进图；"
                       "简单单点问题直接在会话里查证回答即可，不要为一次 grep 建图。",
        "parameters": {
            "type": "object",
            "properties": {
                "dag": {"type": "string", "description": "DAG 名称（须已 dag_define 或为内置模板名）"},
                "params": {"type": "object", "description": "run 参数（spec.params 声明的键值）"},
                "idempotency_key": {"type": "string",
                                    "description": "防重复触发键，如 rule-error:<规则UUID>:<日期>"},
            },
            "required": ["dag"],
        },
    },
    {
        "name": "dag_status",
        "description": "查看一次 DAG run 的实时状态：每节点卡状态、数据流摘要（outputs）、依赖视图、事件时间线。",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "run id（dag_run 返回）。省略则列出最近 runs"},
            },
            "required": [],
        },
    },
]
