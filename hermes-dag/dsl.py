"""dsl.py — DAG spec（YAML）解析、校验、环检测

spec 结构（见 data/templates/diag-rule-error.yaml）：
    dag: <name>                 必填，[a-z0-9][a-z0-9._-]*
    version: 1
    description: str
    params: {name: {type, required, description}}
    nodes:
      - id: str                 必填，唯一，[a-z0-9][a-z0-9._-]*
        kind: exec | verdict    默认 exec
        parents: [node_id]      可与 edges 取并集
        when: str               条件表达式（evaluator 语法），false → 子树剪枝
        foreach: str            表达式，求值须为 list；逐元素建卡（shard=元素）
        assignee: str           worker profile（缺省用 kanban 默认派发）
        skills: [str]           盖章进卡（命令唯一来源）
        body_template: str      exec 必填
        judge: {instructions}   verdict 必填
    edges: [{from, to}]         与 parents 取并集

不变式（引擎正确性依赖）：
    - 图无环；所有 parents/edges 引用存在
    - verdict 节点必须有 parents 且 judge.instructions
    - 首层节点（无 parents）不得带 when（无事可求值）
"""

from __future__ import annotations

import re
import yaml

from . import evaluator


class DagSpecError(ValueError):
    """spec 非法。聚合所有错误一次性抛出。"""


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KINDS = ("exec", "verdict")
_PARAM_TYPES = ("str", "int", "float", "bool", "list")


class NodeSpec(dict):
    """dict 便捷封装：node['id'] 等键访问 + 常用属性。"""

    @property
    def id(self) -> str:
        return self["id"]

    @property
    def kind(self) -> str:
        return self.get("kind", "exec")

    @property
    def parents(self) -> list:
        return list(self.get("parents") or [])

    @property
    def when(self):
        return self.get("when")

    @property
    def foreach(self):
        return self.get("foreach")


class DagSpec(dict):
    @property
    def name(self) -> str:
        return self["dag"]

    @property
    def nodes(self) -> list:
        return [NodeSpec(n) for n in self.get("nodes", [])]

    def node(self, node_id: str):
        for n in self.get("nodes", []):
            if n.get("id") == node_id:
                return NodeSpec(n)
        return None

    def node_ids(self) -> list:
        return [n["id"] for n in self.get("nodes", [])]

    def children_map(self) -> dict:
        out = {nid: [] for nid in self.node_ids()}
        for n in self.get("nodes", []):
            for p in (n.get("parents") or []):
                out.setdefault(p, []).append(n["id"])
        return out

    def parents_map(self) -> dict:
        return {n["id"]: list(n.get("parents") or []) for n in self.get("nodes", [])}

    def depth_map(self) -> dict:
        """拓扑深度（环已在 validate 排除）。"""
        depths = {}
        visiting = set()

        def visit(nid: str) -> int:
            if nid in depths:
                return depths[nid]
            if nid in visiting:
                return 0
            visiting.add(nid)
            ps = self.parents_map().get(nid, [])
            d = 0 if not ps else 1 + max(visit(p) for p in ps)
            visiting.discard(nid)
            depths[nid] = d
            return d

        for nid in self.node_ids():
            visit(nid)
        return depths


def parse_spec(yaml_text: str) -> DagSpec:
    """解析 + 校验。失败抛 DagSpecError（含全部错误行）。"""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise DagSpecError(f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise DagSpecError("spec root must be a mapping")
    return validate_spec(data)


def validate_spec(data: dict) -> DagSpec:
    errors: list = []

    name = data.get("dag")
    if not name or not isinstance(name, str) or not _ID_RE.match(name):
        errors.append(f"dag name invalid: {name!r} (want [a-z0-9][a-z0-9._-]*)")

    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes must be a non-empty list")
        raise DagSpecError("\n".join(errors))

    ids: list = []
    node_list: list = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            errors.append(f"nodes[{i}] must be a mapping")
            continue
        nid = n.get("id")
        if not nid or not isinstance(nid, str) or not _ID_RE.match(nid):
            errors.append(f"nodes[{i}].id invalid: {nid!r}")
            continue
        if nid in ids:
            errors.append(f"duplicate node id: {nid}")
        ids.append(nid)
        node_list.append(n)

    # edges 先并入 parents（后续校验基于合并后的图）
    id_set = set(ids)
    for i, e in enumerate(data.get("edges") or []):
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            errors.append(f"edges[{i}] must be {{from, to}}")
            continue
        if e["from"] not in id_set or e["to"] not in id_set:
            errors.append(f"edges[{i}]: unknown endpoint {e}")
            continue
        target = next((n for n in node_list if n.get("id") == e["to"]), None)
        if target is not None:
            target.setdefault("parents", [])
            if e["from"] not in target["parents"]:
                target["parents"].append(e["from"])

    for n in node_list:
        nid = n["id"]
        kind = n.get("kind", "exec")
        if kind not in _KINDS:
            errors.append(f"node {nid}: kind must be one of {_KINDS}, got {kind!r}")

        for key in ("assignee",):
            if n.get(key) is not None and not isinstance(n.get(key), str):
                errors.append(f"node {nid}: {key} must be a string")
        skills = n.get("skills")
        if skills is not None and not (isinstance(skills, list) and all(isinstance(s, str) for s in skills)):
            errors.append(f"node {nid}: skills must be a list of strings")

        synthesis = n.get("synthesis")
        if synthesis is not None:
            if synthesis != "report":
                errors.append(f"node {nid}: synthesis must be 'report' (only supported kind)")
            if n.get("foreach"):
                errors.append(f"node {nid}: synthesis cannot combine with foreach")

        if kind == "exec" and not synthesis and not (n.get("body_template") or "").strip():
            errors.append(f"node {nid}: kind=exec requires body_template (or synthesis)")
        if kind == "verdict":
            if not (n.get("parents") or []):
                errors.append(f"node {nid}: kind=verdict requires parents (fan-in)")
            judge = n.get("judge")
            if not isinstance(judge, dict) or not (judge.get("instructions") or "").strip():
                errors.append(f"node {nid}: kind=verdict requires judge.instructions")

        when = n.get("when")
        if when is not None:
            if not isinstance(when, str):
                errors.append(f"node {nid}: when must be a string expression")
            else:
                for err in evaluator.validate_expression(when):
                    errors.append(f"node {nid}: when: {err}")
        foreach = n.get("foreach")
        if foreach is not None:
            if not isinstance(foreach, str):
                errors.append(f"node {nid}: foreach must be a string expression")
            else:
                for err in evaluator.validate_expression(foreach):
                    errors.append(f"node {nid}: foreach: {err}")

    # 引用存在性 + 首层约束
    for n in node_list:
        nid = n["id"]
        for p in (n.get("parents") or []):
            if p not in id_set:
                errors.append(f"node {nid}: unknown parent {p!r}")
        if n.get("kind", "exec") == "exec" and n.get("when") and not (n.get("parents") or []):
            errors.append(f"node {nid}: entry node (no parents) must not have `when`")

    params = data.get("params") or {}
    if not isinstance(params, dict):
        errors.append("params must be a mapping")
    else:
        for pname, pdef in params.items():
            if not isinstance(pdef, dict):
                errors.append(f"params.{pname} must be a mapping")
                continue
            if pdef.get("type", "str") not in _PARAM_TYPES:
                errors.append(f"params.{pname}: type must be one of {_PARAM_TYPES}")

    # 环检测（DFS）
    if not errors:
        cycle = _find_cycle({n["id"]: list(n.get("parents") or []) for n in node_list})
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))

    if errors:
        raise DagSpecError("\n".join(errors))
    return DagSpec(data)


def _find_cycle(parents_map: dict):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in parents_map}
    stack_path: list = []

    def visit(nid: str):
        color[nid] = GRAY
        stack_path.append(nid)
        for p in parents_map.get(nid, []):
            if p not in color:
                continue
            if color[p] == GRAY:
                idx = stack_path.index(p)
                return stack_path[idx:] + [p]
            if color[p] == WHITE:
                found = visit(p)
                if found:
                    return found
        stack_path.pop()
        color[nid] = BLACK
        return None

    for nid in parents_map:
        if color[nid] == WHITE:
            found = visit(nid)
            if found:
                return found
    return None


def descendants_map(spec: DagSpec) -> dict:
    """node_id -> 直接后继列表（含 edges 并集后的 parents 反推）。"""
    return spec.children_map()
