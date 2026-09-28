"""commands.py — /dag 斜杠命令（人用入口；内部复用 DagEngine，与工具不分叉）

用法：
    /dag list                    最近 runs
    /dag status <run_id>         run 实时状态（缺省 = 最近一条）
    /dag run <name> [json 参数]   触发 run，如 /dag run diag-rule-error {"rule_id": "..."}
    /dag explain <name>          静态展示 DAG 结构
"""

from __future__ import annotations

import json
import time


def make_dag_command(engine):
    def dag_command(raw_args: str) -> str:
        try:
            return _dispatch(engine, (raw_args or "").strip())
        except Exception as exc:  # fail-open：命令错误只回文本，不炸会话
            return f"[dag] error: {type(exc).__name__}: {exc}"

    return dag_command


def _dispatch(engine, raw: str) -> str:
    parts = raw.split(maxsplit=2) if raw else []
    if not parts:
        return _usage()
    sub, rest = parts[0], parts[1:]

    if sub == "list":
        runs = engine.store.all_runs(limit=15)
        if not runs:
            return "[dag] no runs yet"
        lines = ["run_id | dag | status | created"]
        for r in runs:
            lines.append(f"{r['run_id']} | {r['dag']} | {r['status']} | "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(r['created_at']))}")
        return "\n".join(lines)

    if sub == "status":
        run_id = rest[0] if rest else None
        if run_id is None:
            runs = engine.store.all_runs(limit=1)
            if not runs:
                return "[dag] no runs yet"
            run_id = runs[0]["run_id"]
        data = engine.dag_status(run_id)
        if not data.get("ok"):
            return f"[dag] {data.get('error')}"
        out = [f"[dag] {data['run_id']} ({data['dag']}) status={data['status']}", "",
               data["graph"], ""]
        for n in data["nodes"]:
            if n.get("summary"):
                ks = ",".join(f"{c['card_id']}:{c.get('kanban') or '?'}"
                              for c in (n.get("cards") or []))
                out.append(f"  {n['id']}: {n['summary']}" + (f"  ({ks})" if ks else ""))
        if data.get("kanban_error"):
            out.append(f"  [warn] kanban query error: {data['kanban_error']}")
        return "\n".join(out)

    if sub == "run":
        if not rest:
            return "[dag] usage: /dag run <name> [json-params]"
        name = rest[0]
        params = {}
        if len(rest) > 1:
            try:
                params = json.loads(rest[1])
            except json.JSONDecodeError as exc:
                return f"[dag] params must be JSON: {exc}"
        data = engine.dag_run(name, params=params)
        if not data.get("ok"):
            return f"[dag] {data.get('error') or data.get('errors')}"
        head = (f"[dag] run {data['run_id']} started"
                if data.get("created") else f"[dag] run {data['run_id']} already exists")
        return head + f" (status={data.get('status')})"

    if sub == "explain":
        if not rest:
            return "[dag] usage: /dag explain <name>"
        data = engine.dag_explain(rest[0])
        if not data.get("ok"):
            return f"[dag] {data.get('error')}"
        out = [f"[dag] {data['dag']} v{data.get('version')} — {len(data['nodes'])} nodes"]
        for n in data["nodes"]:
            extra = []
            if n["parents"]:
                extra.append(f"← {'+'.join(n['parents'])}")
            if n["when"]:
                extra.append(f"when: {n['when']}")
            if n["foreach"]:
                extra.append(f"foreach: {n['foreach']}")
            if n["skills"]:
                extra.append(f"skills: {','.join(n['skills'])}")
            out.append(f"  {n['id']} ({n['kind']})" + ("  " + "; ".join(extra) if extra else ""))
        return "\n".join(out)

    return _usage()


def _usage() -> str:
    return ("[dag] usage:\n"
            "  /dag list\n"
            "  /dag status [run_id]\n"
            "  /dag run <name> [json-params]\n"
            "  /dag explain <name>")
