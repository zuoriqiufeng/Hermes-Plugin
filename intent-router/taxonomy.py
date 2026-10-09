"""taxonomy.py — 意图表加载、校验与索引构建。

意图表 = 一个 YAML（JSON 亦可，JSON 是 YAML 子集）声明：

    version: 1
    lexicon: [data/lexicon/ops-bkn.yaml]     # 可选：词表分组，供 keywords 里的 "@组名" 引用
    intents:
      - id: diag_error_code
        description: ...
        action: act | hint | none            # 命中后允许的动作档位上限
        examples: ["-4073 是什么意思", ...]   # BM25 范例句
        keywords:                            # 第二层关键词，可带权重或引用词表组
          - 错误码
          - {term: 日志解析异常, weight: 1.5}
          - "@symptom:incremental_stuck"
        expects_entities: [error_code]        # 出现这些实体给加成
        veto_patterns: [怎么改, 帮我写]        # 命中即一票否决
        requires: [error_code]                # act 前置：缺这些实体/槽位就不许直连
        tool_binding:                         # action=act 时的直连目标（只读工具）
          tool: diagnose_error
          args: {error_code: "{entities.error_code}"}
          timeout_ms: 1500
    rules:                                    # 第一层正则（见 regex_tier.compile_rules）
      - id: bare_error_code
        intent: diag_error_code
        pattern: "^-?[0-9]{3,5}$"
        target: compact
        priority: 100
        slots: {code: error_code}             # 命名组 -> 参数名

合并顺序（后者覆盖前者，同 id 覆盖、新 id 追加）：
内置 ``data/taxonomy/ops-diag.yaml`` → 配置 ``taxonomy`` 指定文件 →
``<plugin-data>/intent-router/taxonomy.d/*.yaml``（运维现场覆盖，不必改仓库）。

所有词条与范例句在加载时**统一归一化**（见 normalize.py），保证与用户输入同一口径；
``tools/gen_lexicon.py`` 生成的词表带源文件哈希，``/intent doctor`` 会报漂移。
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import bm25 as bm25_mod
from .aho import Automaton
from .normalize import normalize
from .regex_tier import Rule, compile_rules

log = logging.getLogger("intent-router")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACTIONS = ("none", "hint", "act")
PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_TAXONOMY = "data/taxonomy/ops-diag.yaml"
DEFAULT_LEXICON = "data/lexicon/ops-bkn.yaml"


@dataclass(frozen=True)
class ToolBinding:
    """act 档位的直连目标。

    ``args`` 的值支持 ``{entities.<名>}`` / ``{slots.<名>}`` / 字面量，**全部可解析**才执行；
    ``optional_args`` 同理，但值取不到时**跳过该参数**而不是放弃调用（例如
    ``diagnose_db_link`` 的 db_type：有库名就带上，没有就让工具自己按错误码前缀推断）。
    """

    tool: str
    args: Dict[str, str] = field(default_factory=dict)
    optional_args: Dict[str, str] = field(default_factory=dict)
    timeout_ms: Optional[int] = None
    safe_auto: bool = False


@dataclass(frozen=True)
class Keyword:
    """一条关键词证据。

    ``term`` 以 ``@`` 开头时表示**词表组引用**（如 ``@symptom:incremental_stuck``），
    在加载期展开为组内全部词条。``exclude`` 只在组引用上有意义：用来剔除源词表里
    过于宽泛的说法（例如 crash_loop 组里的「报错」「出错」「失败」——它们几乎出现在
    一切异常描述里，留着会把无害的错误消息也判成崩溃循环）。
    """

    term: str
    weight: float = 1.0
    exclude: Tuple[str, ...] = ()

    @property
    def is_group(self) -> bool:
        return self.term.startswith("@")


@dataclass(frozen=True)
class IntentSpec:
    id: str
    description: str = ""
    action: str = "hint"
    examples: Tuple[str, ...] = ()
    keywords: Tuple[Keyword, ...] = ()
    expects_entities: Tuple[str, ...] = ()
    veto_patterns: Tuple[str, ...] = ()
    veto_entities: Tuple[str, ...] = ()
    requires: Tuple[str, ...] = ()
    allowed_tools: Tuple[str, ...] = ()
    tool_binding: Optional[ToolBinding] = None


@dataclass
class Taxonomy:
    """加载完成的意图表 + 派生索引（AC 自动机、BM25、关键词归属）。"""

    version: int = 1
    intents: Dict[str, IntentSpec] = field(default_factory=dict)
    rules: List[Rule] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    lexicon_files: List[str] = field(default_factory=list)
    lexicon_drift: List[str] = field(default_factory=list)

    # 派生索引（build 后填充）
    keyword_index: Dict[str, List[Tuple[str, float]]] = field(default_factory=dict)
    keyword_terms: List[str] = field(default_factory=list)
    keyword_automaton: Optional[Automaton] = None
    veto_automaton: Optional[Automaton] = None
    veto_patterns: List[str] = field(default_factory=list)
    veto_terms: List[str] = field(default_factory=list)
    bm25_index: Optional[bm25_mod.BM25Index] = None

    @property
    def intent_ids(self) -> List[str]:
        return sorted(self.intents.keys())

    def spec(self, intent_id: str) -> Optional[IntentSpec]:
        return self.intents.get(intent_id)

    def build_indexes(self) -> None:
        """构建 AC 自动机与 BM25 索引（加载后调用一次）。绝不抛出。"""
        try:
            # 空串不进自动机（Automaton 会跳过且不占索引），所以先过滤再建，
            # 保证 keyword_terms[i] 就是「模式索引 i」对应的词条。
            keywords = [term for term in sorted(self.keyword_index.keys()) if term]
            self.keyword_terms = keywords
            self.keyword_automaton = Automaton(keywords) if keywords else None
            veto_terms = [pattern for pattern in sorted(set(self.veto_patterns)) if pattern]
            self.veto_terms = veto_terms
            self.veto_automaton = Automaton(veto_terms) if veto_terms else None
            docs: List[Tuple[str, str]] = []
            for intent_id, spec in sorted(self.intents.items()):
                for example in spec.examples:
                    docs.append((intent_id, example))
            self.bm25_index = bm25_mod.BM25Index.build(docs) if docs else None
        except Exception:
            log.warning("intent-router: 索引构建失败，第二层降级为仅关键词", exc_info=True)


def _load_mapping(path: Path) -> Optional[Mapping[str, Any]]:
    """读 YAML（无 PyYAML 时按 JSON 解析）。任何失败返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml  # PyYAML 是 Hermes 自身的依赖；缺失时退化为 JSON
        data = yaml.safe_load(text)
    except Exception:
        try:
            data = json.loads(text)
        except Exception:
            log.warning("intent-router: 意图表 %s 不是合法 YAML/JSON", path)
            return None
    return data if isinstance(data, Mapping) else None


def _resolve(path_text: str) -> Path:
    candidate = Path(os.path.expanduser(str(path_text)))
    return candidate if candidate.is_absolute() else (PLUGIN_DIR / candidate)


def _normalize_group_terms(terms: Iterable[Any]) -> Tuple[Tuple[str, float], ...]:
    """词表组的词条（只接受纯字符串/``{term, weight}``，不支持组套组）。"""
    out: List[Tuple[str, float]] = []
    for item in terms:
        if isinstance(item, str):
            term, weight = item, 1.0
        elif isinstance(item, Mapping):
            term, weight = str(item.get("term") or ""), float(item.get("weight", 1.0) or 1.0)
        else:
            continue
        normalized = normalize(term, max_chars=64)
        if normalized.empty:
            continue
        out.append((normalized.text, weight))
    return tuple(out)


def _normalize_keywords(entries: Iterable[Any]) -> Tuple[Keyword, ...]:
    """归一化关键词条目。四种写法：

    ``"错误码"``
        ``{term: 错误码, weight: 1.5, exclude: [...]}``
        ``"@symptom:incremental_stuck"``            词表组引用
        ``{group: "symptom:crash_loop", exclude: ["报错"], weight: 1.0}``
    """
    out: List[Keyword] = []
    for item in entries or ():
        exclude: Tuple[str, ...] = ()
        weight = 1.0
        if isinstance(item, str):
            term = item
        elif isinstance(item, Mapping):
            group = item.get("group")
            if group:
                # {group: "x"} 与 "@x" 等价：统一补上前缀，交给 _expand_groups 展开
                term = str(group) if str(group).startswith("@") else "@" + str(group)
            else:
                term = str(item.get("term") or "")
            try:
                weight = float(item.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            raw_exclude = item.get("exclude")
            if isinstance(raw_exclude, (list, tuple)):
                exclude = tuple(
                    normalized.text
                    for normalized in (normalize(str(entry), max_chars=64) for entry in raw_exclude)
                    if normalized.text
                )
        else:
            continue
        if not term:
            continue
        if term.startswith("@"):
            # 组引用本身是 id，不做文本归一化（否则大小写/全角会被改动）
            out.append(Keyword(term=term.strip(), weight=weight, exclude=exclude))
            continue
        normalized = normalize(term, max_chars=64)
        if normalized.empty:
            continue
        out.append(Keyword(term=normalized.text, weight=weight, exclude=exclude))
    return tuple(out)


def _normalize_patterns(patterns: Iterable[Any]) -> Tuple[str, ...]:
    out: List[str] = []
    for item in patterns or ():
        normalized = normalize(str(item or ""), max_chars=128)
        if not normalized.empty:
            out.append(normalized.text)
    return tuple(out)


def _parse_binding(raw: Any, intent_id: str, warnings: List[str]) -> Optional[ToolBinding]:
    if not isinstance(raw, Mapping):
        return None
    tool = str(raw.get("tool") or "").strip()
    if not tool:
        warnings.append("intent %r: tool_binding 缺 tool，忽略" % intent_id)
        return None
    args_raw = raw.get("args")
    args: Dict[str, str] = {}
    if isinstance(args_raw, Mapping):
        args = {str(key): str(value) for key, value in args_raw.items()}
    elif args_raw is not None:
        warnings.append("intent %r: tool_binding.args 不是映射，忽略" % intent_id)
    optional_raw = raw.get("optional_args")
    optional_args: Dict[str, str] = {}
    if isinstance(optional_raw, Mapping):
        optional_args = {str(key): str(value) for key, value in optional_raw.items()}
    elif optional_raw is not None:
        warnings.append("intent %r: tool_binding.optional_args 不是映射，忽略" % intent_id)
    timeout_ms: Optional[int] = None
    if raw.get("timeout_ms") is not None:
        try:
            timeout_ms = max(1, int(raw["timeout_ms"]))
        except (TypeError, ValueError):
            warnings.append("intent %r: tool_binding.timeout_ms 不是整数，用全局值" % intent_id)
    return ToolBinding(
        tool=tool,
        args=args,
        optional_args=optional_args,
        timeout_ms=timeout_ms,
        safe_auto=bool(raw.get("safe_auto", False)),
    )


def _parse_intent(raw: Any, warnings: List[str]) -> Optional[IntentSpec]:
    if not isinstance(raw, Mapping):
        warnings.append("intents 里有非映射条目，跳过")
        return None
    intent_id = str(raw.get("id") or "").strip()
    if not intent_id or not _ID_RE.match(intent_id):
        warnings.append("intent id %r 非法，跳过" % intent_id)
        return None
    action = str(raw.get("action") or "hint").strip().lower()
    if action not in _ACTIONS:
        warnings.append("intent %r: action=%r 非法，按 hint 处理" % (intent_id, action))
        action = "hint"
    examples = tuple(
        normalize(str(item), max_chars=128).text
        for item in (raw.get("examples") or ())
        if normalize(str(item), max_chars=128).text
    )
    return IntentSpec(
        id=intent_id,
        description=str(raw.get("description") or ""),
        action=action,
        examples=examples,
        keywords=_normalize_keywords(raw.get("keywords") or ()),
        expects_entities=tuple(str(name) for name in (raw.get("expects_entities") or ())),
        veto_patterns=_normalize_patterns(raw.get("veto_patterns") or ()),
        veto_entities=tuple(str(name) for name in (raw.get("veto_entities") or ())),
        requires=tuple(str(name) for name in (raw.get("requires") or ())),
        allowed_tools=tuple(str(name) for name in (raw.get("allowed_tools") or ())),
        tool_binding=_parse_binding(raw.get("tool_binding"), intent_id, warnings),
    )


def _merge_document(
    doc: Mapping[str, Any],
    *,
    source: str,
    intents: Dict[str, IntentSpec],
    warnings: List[str],
    sources: List[str],
    lexicon_files: List[str],
    rules_raw: List[Any],
) -> None:
    sources.append(source)
    for item in doc.get("intents") or ():
        spec = _parse_intent(item, warnings)
        if spec is None:
            continue
        if spec.id in intents:
            warnings.append("intent %r 被 %s 覆盖" % (spec.id, source))
        intents[spec.id] = spec
    for item in doc.get("lexicon") or ():
        lexicon_files.append(str(item))
    for item in doc.get("rules") or ():
        if isinstance(item, Mapping):
            entry = dict(item)
            entry.setdefault("_source", source)
            rules_raw.append(entry)


def _expand_groups(
    intents: Dict[str, IntentSpec],
    groups: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, IntentSpec]:
    """把 ``@组名`` 引用展开成实际词条；组缺失告警并丢弃该条，``exclude`` 内的词条剔除。"""
    expanded: Dict[str, IntentSpec] = {}
    for intent_id, spec in intents.items():
        keywords: List[Keyword] = []
        for keyword in spec.keywords:
            if not keyword.is_group:
                keywords.append(keyword)
                continue
            group = groups.get(keyword.term[1:])
            if not group:
                warnings.append("intent %r: 词表组 %r 不存在，忽略" % (intent_id, keyword.term[1:]))
                continue
            excluded = set(keyword.exclude)
            present = {group_term for group_term, _weight in group["terms"]}
            missing = sorted(excluded - present)
            if missing:
                # 只有「排除了一个组里根本没有的词」才告警——那是配置笔误；
                # 正常排除不告警（否则内置表的 warnings 会被噪音填满，失去信号价值）
                warnings.append("intent %r: 词表组 %r 的 exclude 含组内不存在的词 %s" % (
                    intent_id, keyword.term[1:], missing))
            for group_term, _group_weight in group["terms"]:
                if group_term in excluded:
                    continue
                keywords.append(
                    Keyword(term=group_term, weight=keyword.weight * float(group["weight"]))
                )
        expanded[intent_id] = IntentSpec(
            id=spec.id,
            description=spec.description,
            action=spec.action,
            examples=spec.examples,
            keywords=tuple(keywords),
            expects_entities=spec.expects_entities,
            veto_patterns=spec.veto_patterns,
            veto_entities=spec.veto_entities,
            requires=spec.requires,
            allowed_tools=spec.allowed_tools,
            tool_binding=spec.tool_binding,
        )
    return expanded


def _check_lexicon_drift(path: Path, doc: Mapping[str, Any]) -> Optional[str]:
    """词表快照里记了源文件哈希；源文件变了就报漂移（提示重新生成）。"""
    source_path = doc.get("generated_from")
    expected = doc.get("source_sha256")
    if not source_path or not expected:
        return None
    try:
        actual = hashlib.sha256(Path(str(source_path)).read_bytes()).hexdigest()
    except OSError:
        return "%s: 源文件 %s 读不到" % (path.name, source_path)
    if actual != expected:
        return "%s: 源 %s 已变化（快照 %s ≠ 当前 %s），建议重跑 tools/gen_lexicon.py" % (
            path.name, source_path, str(expected)[:12], actual[:12]
        )
    return None


def load_taxonomy(
    *,
    extra_paths: Sequence[str] = (),
    override_dir: Optional[str] = None,
    taxonomy_path: str = DEFAULT_TAXONOMY,
) -> Taxonomy:
    """按「内置 → 配置 → plugin-data 覆盖」顺序合并加载。绝不抛出。"""
    tax = Taxonomy()
    intents: Dict[str, IntentSpec] = {}
    rules_raw: List[Any] = []

    bundled = _resolve(taxonomy_path)
    documents: List[Path] = [bundled]
    for extra in extra_paths:
        if extra:
            documents.append(_resolve(extra))
    if override_dir:
        for pattern in sorted(glob.glob(os.path.join(os.path.expanduser(override_dir), "*.yaml"))):
            documents.append(Path(pattern))
        for pattern in sorted(glob.glob(os.path.join(os.path.expanduser(override_dir), "*.yml"))):
            documents.append(Path(pattern))
        for pattern in sorted(glob.glob(os.path.join(os.path.expanduser(override_dir), "*.json"))):
            documents.append(Path(pattern))

    lexicon_files: List[str] = []
    for path in documents:
        doc = _load_mapping(path)
        if doc is None:
            if path == bundled:
                tax.warnings.append("内置意图表 %s 读不到，识别将退化为「不干预」" % path)
            else:
                tax.warnings.append("意图表 %s 读不到，忽略" % path)
            continue
        try:
            tax.version = int(doc.get("version", tax.version))
        except (TypeError, ValueError):
            pass
        _merge_document(
            doc, source=str(path), intents=intents, warnings=tax.warnings,
            sources=tax.sources, lexicon_files=lexicon_files, rules_raw=rules_raw,
        )

    groups: Dict[str, Any] = {}
    for lexicon_ref in lexicon_files or [DEFAULT_LEXICON]:
        lexicon_path = _resolve(lexicon_ref)
        doc = _load_mapping(lexicon_path)
        if doc is None:
            tax.warnings.append("词表 %s 读不到，忽略" % lexicon_path)
            continue
        tax.lexicon_files.append(str(lexicon_path))
        drift = _check_lexicon_drift(lexicon_path, doc)
        if drift:
            tax.lexicon_drift.append(drift)
        for name, body in (doc.get("groups") or {}).items():
            if not isinstance(body, Mapping):
                continue
            try:
                weight = float(body.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            groups[str(name)] = {"terms": _normalize_group_terms(body.get("terms") or ()), "weight": weight}

    intents = _expand_groups(intents, groups, tax.warnings)

    # 关键词 / 否定模式 → 索引；同时记录「词条 → 哪些意图声明了它」
    keyword_index: Dict[str, List[Tuple[str, float]]] = {}
    veto_patterns: List[str] = []
    for intent_id, spec in intents.items():
        for keyword in spec.keywords:
            keyword_index.setdefault(keyword.term, []).append((intent_id, keyword.weight))
        veto_patterns.extend(spec.veto_patterns)
    tax.keyword_index = keyword_index
    tax.veto_patterns = sorted(set(veto_patterns))

    rules, rule_warnings = compile_rules(rules_raw)
    known_rules: List[Rule] = []
    for rule in rules:
        if rule.intent not in intents:
            rule_warnings.append("rule %r: 引用了不存在的 intent %r，跳过" % (rule.id, rule.intent))
            continue
        known_rules.append(rule)
    tax.rules = known_rules
    tax.warnings.extend(rule_warnings)

    tax.intents = intents
    tax.build_indexes()
    return tax
