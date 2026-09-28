"""render.py — body 模板渲染（mini，零依赖）

语法：{{ path.to.value }} —— 支持点路径取值，上下文形如：
    {"run": {"id": ...}, "params": {...}, "nodes": {id: {"outputs": {...}, "status": ...}}}
list/dict 值渲染为紧凑 JSON（ensure_ascii=False）；缺失路径渲染为空串。
不实现循环/条件——流程逻辑归 spec/advancer，模板只做取值。
"""

from __future__ import annotations

import json
import re

_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _resolve(ctx: dict, path: str):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif cur is None:
            return None
        else:
            try:
                cur = getattr(cur, part)
            except AttributeError:
                return None
    return cur


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render(template: str, ctx: dict) -> str:
    if not template:
        return ""
    return _TOKEN.sub(lambda m: _stringify(_resolve(ctx, m.group(1))), template)
