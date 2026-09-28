"""evaluator.py — `when` 条件表达式的受限求值器

语法白名单（防注入、可静态检查，非任意 Python）：
    比较:  ==  !=  in  not in
    逻辑:  and  or  not  括号
    字面量: 字符串 / 数字 / True / False / None / [list]
    路径:  nodes.<node_id>.outputs.<key>   nodes.<node_id>.status   params.<name>

实现基于 ast 模块：先 validate（白名单节点类型，拒一切其他语法），
再由 _walk 解释执行——全程不调用 eval()。
"""

from __future__ import annotations

import ast


class ExpressionError(ValueError):
    """表达式解析/校验失败。"""


_ALLOWED_CMP = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
_ALLOWED_BOOL = (ast.And, ast.Or)


def validate_expression(expr: str) -> list:
    """静态校验表达式，返回错误列表（空列表 = 合法）。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    errors: list = []
    _check(tree.body, errors)
    return errors


def _check(node: ast.AST, errors: list) -> None:
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, _ALLOWED_BOOL):
            errors.append(f"operator {type(node.op).__name__} not allowed")
        for v in node.values:
            _check(v, errors)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            errors.append(f"unary {type(node.op).__name__} not allowed")
        _check(node.operand, errors)
    elif isinstance(node, ast.Compare):
        if len(node.ops) != len(node.comparators):
            errors.append("chained comparison not supported")
        for op, comp in zip(node.ops, node.comparators):
            if not isinstance(op, _ALLOWED_CMP):
                errors.append(f"comparison {type(op).__name__} not allowed")
            _check(node.left, errors)
            _check(comp, errors)
    elif isinstance(node, ast.Attribute):
        path, root = _attr_path(node)
        if root == "nodes":
            # nodes.<id>.outputs.<key> 或 nodes.<id>.status
            if len(path) < 2:
                errors.append(f"attribute path '{'.'.join([root] + path)}' too short")
        elif root == "params":
            if len(path) != 1:
                errors.append("params paths must be params.<name>")
        else:
            errors.append(f"attribute root '{root}' not allowed (use nodes.* / params.*)")
    elif isinstance(node, ast.Subscript):
        errors.append("subscript not allowed; use dotted attribute paths")
    elif isinstance(node, ast.Name):
        if node.id not in ("nodes", "params", "True", "False", "None"):
            errors.append(f"name '{node.id}' not allowed (use nodes.* / params.*)")
    elif isinstance(node, ast.Constant):
        if not (node.value is None or isinstance(node.value, (str, int, float, bool))):
            errors.append(f"constant type {type(node.value).__name__} not allowed")
    elif isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _check(elt, errors)
    elif isinstance(node, ast.Call):
        errors.append("function calls not allowed")
    else:
        errors.append(f"syntax {type(node).__name__} not allowed")


def _attr_path(node: ast.AST) -> tuple:
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    parts.reverse()
    return parts, (cur.id if isinstance(cur, ast.Name) else None)


def _lookup(env: dict, node: ast.AST):
    """解析路径/名字到值；查不到返回 None。"""
    if isinstance(node, ast.Name):
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        return env.get(node.id)
    if isinstance(node, ast.Attribute):
        parts, root = _attr_path(node)
        if root is None:
            return None
        cur = env.get(root)
        for p in parts:
            if isinstance(cur, dict):
                cur = cur.get(p)
            elif cur is None:
                return None
            else:
                try:
                    cur = getattr(cur, p)
                except AttributeError:
                    return None
        return cur
    return None


def _walk(node: ast.AST, env: dict):
    if isinstance(node, ast.BoolOp):
        vals = (_walk(v, env) for v in node.values)
        if isinstance(node.op, ast.And):
            return all(bool(v) for v in vals)
        return any(bool(v) for v in vals)
    if isinstance(node, ast.UnaryOp):  # Not
        return not bool(_walk(node.operand, env))
    if isinstance(node, ast.Compare):
        left = _lookup(env, node.left) if not isinstance(node.left, (ast.Constant, ast.List, ast.Tuple)) else _literal(node.left)
        results = []
        for op, comp in zip(node.ops, node.comparators):
            right = _lookup(env, comp) if not isinstance(comp, (ast.Constant, ast.List, ast.Tuple)) else _literal(comp)
            if isinstance(op, ast.Eq):
                results.append(left == right)
            elif isinstance(op, ast.NotEq):
                results.append(left != right)
            elif isinstance(op, ast.In):
                seq = right if isinstance(right, (list, tuple, str)) else ([] if right is None else [right])
                results.append(left in seq)
            elif isinstance(op, ast.NotIn):
                seq = right if isinstance(right, (list, tuple, str)) else ([] if right is None else [right])
                results.append(left not in seq)
            left = right
        return all(results)
    if isinstance(node, (ast.Constant, ast.List, ast.Tuple)):
        return _literal(node)
    return _lookup(env, node)


def _literal(node: ast.AST):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(e) for e in node.elts]
    return None


def evaluate(expr: str, env: dict) -> bool:
    """求值。env 期望形如 {"nodes": {id: {"outputs": {...}, "status": str}}, "params": {...}}。
    解析/校验失败抛 ExpressionError（调用方应已先 validate）。"""
    try:
        return bool(_evaluate_raw(expr, env))
    except ExpressionError:
        raise
    except Exception as exc:  # 求值期防御：任何意外按 False 处理
        raise ExpressionError(f"{expr!r}: evaluation error: {exc}") from exc


def evaluate_value(expr: str, env: dict):
    """foreach 用：返回表达式原始值（不强制 bool）。非法表达式抛 ExpressionError。"""
    try:
        return _evaluate_raw(expr, env)
    except ExpressionError:
        raise
    except Exception as exc:
        raise ExpressionError(f"{expr!r}: evaluation error: {exc}") from exc


def _evaluate_raw(expr: str, env: dict):
    errors = validate_expression(expr)
    if errors:
        raise ExpressionError(f"{expr!r}: {'; '.join(errors)}")
    tree = ast.parse(expr, mode="eval")
    return _walk(tree.body, env)
