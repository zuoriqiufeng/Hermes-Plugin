#!/usr/bin/env python3
"""tools/gen_lexicon.py — 从 i2stream-bkn 的 constants.py 生成词表快照。

**开发期脚本，不进运行时**：插件运行时只读 ``data/lexicon/*.yaml``，不跨仓 import。
这样做的两个理由：

1. i2stream-bkn 不是本插件的依赖，跨仓 import 会在对方重构时直接打断本插件加载；
2. 快照带**源文件 sha256**，源词表改了能立刻发现（``/intent doctor`` 报漂移并提示重跑）。

用法::

    python3 tools/gen_lexicon.py                      # 默认路径，写 data/lexicon/ops-bkn.yaml
    python3 tools/gen_lexicon.py --check              # 只检查漂移，不写文件（CI/巡检可用）

生成的组：
    error:phrase          ERROR_ALIASES 的全部说法（都表示「这是个错误/要诊断」）
    symptom:<规范id>      SYMPTOM_ALIASES 按规范症状 id 分组
    dimension:<规范id>    DIMENSION_ALIASES 按规范维度 id 分组
    db:mention            DB_ALIASES 的别名与显示名（库名提及）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_BKN = "/hdd/demo/public/i2stream-bkn/plugin/constants.py"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PLUGIN_DIR / "data" / "lexicon" / "ops-bkn.yaml"

HEADER = """# 由 tools/gen_lexicon.py 生成，请勿手工编辑。
# 源: {source}
# 源文件 sha256: {sha}
# 重跑: python3 tools/gen_lexicon.py
# 漂移检查: python3 tools/gen_lexicon.py --check
"""


def _load_constants(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("bkn_constants_snapshot", str(path))
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _group_by_value(mapping: Dict[str, str]) -> Dict[str, List[str]]:
    """把 {说法: 规范id} 折叠成 {规范id: [说法...]}（保序、去重）。"""
    grouped: Dict[str, List[str]] = {}
    for alias, canonical in mapping.items():
        bucket = grouped.setdefault(str(canonical), [])
        if alias not in bucket:
            bucket.append(alias)
    return grouped


def build_groups(constants: Any) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}

    error_aliases = dict(getattr(constants, "ERROR_ALIASES", {}) or {})
    if error_aliases:
        groups["error:phrase"] = {"weight": 1.0, "terms": sorted(error_aliases.keys())}

    for canonical, aliases in sorted(_group_by_value(dict(getattr(constants, "SYMPTOM_ALIASES", {}) or {})).items()):
        groups["symptom:%s" % canonical] = {"weight": 1.0, "terms": sorted(aliases)}

    for canonical, aliases in sorted(_group_by_value(dict(getattr(constants, "DIMENSION_ALIASES", {}) or {})).items()):
        groups["dimension:%s" % canonical] = {"weight": 1.0, "terms": sorted(aliases)}

    db_aliases = dict(getattr(constants, "DB_ALIASES", {}) or {})
    if db_aliases:
        terms = set(db_aliases.keys()) | {str(value) for value in db_aliases.values()}
        groups["db:mention"] = {"weight": 1.0, "terms": sorted(term for term in terms if term)}

    return groups


def dump_yaml(payload: Dict[str, Any]) -> str:
    try:
        import yaml
    except Exception:
        # 没有 PyYAML 也不阻塞：JSON 是 YAML 的子集，加载端同样能读
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=1000)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 i2stream-bkn constants 生成词表快照")
    parser.add_argument("--bkn-constants", default=DEFAULT_BKN)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true", help="只检查漂移，不写文件")
    args = parser.parse_args(argv)

    source = Path(args.bkn_constants)
    if not source.is_file():
        print("源文件不存在: %s" % source, file=sys.stderr)
        return 2
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    constants = _load_constants(source)
    groups = build_groups(constants)
    if not groups:
        print("源文件里没找到任何别名表（ERROR_ALIASES/SYMPTOM_ALIASES/...）", file=sys.stderr)
        return 2

    out = Path(args.out)
    if args.check:
        if not out.is_file():
            print("快照不存在: %s（需先运行生成）" % out)
            return 1
        existing = out.read_text(encoding="utf-8")
        if digest in existing:
            print("OK: 快照与源文件一致（%s，%d 组）" % (source.name, len(groups)))
            return 0
        print("DRIFT: %s 已变化，快照过期——请重跑 tools/gen_lexicon.py" % source)
        return 1

    payload = {
        "version": 1,
        "generated_from": str(source),
        "source_sha256": digest,
        "note": "快照：插件运行时只读本文件，不跨仓 import",
        "groups": groups,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER.format(source=source, sha=digest) + dump_yaml(payload), encoding="utf-8")
    term_total = sum(len(body.get("terms") or ()) for body in groups.values())
    print("已生成 %s：%d 组 / %d 词条（源 %s）" % (out, len(groups), term_total, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
