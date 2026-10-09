"""regex_tier.py — 第一层：有序正则规则 + 前置护栏。

适用面：**意图非常明确**的输入——整句就是一个错误码、命令式前缀（``诊断 -4073``）、
寒暄、追问。这一层追求的是**精度**而非召回：宁可放过（交给第二/三层），不可错判。
因此规则默认 ``fullmatch`` 且命中即锁定（首个命中生效），规则按 priority 降序执行。

安全护栏（hook 在对话关键路径上，正则必须可控）：
- 编译期校验：编译失败/非法 id/未知匹配模式的规则**跳过并告警**，不影响其他规则；
- 规则数上限：超出部分丢弃并告警（默认 200，见 config.max_input_chars 的同类限流思路）；
- ReDoS lint：对「嵌套量词」「反向引用」这类高危形态告警（告警不拒载——真实工程里
  也有安全的高危形态，交给人判断；但把风险显式摆到日志里）；
- 输入截断在 normalize 阶段完成，任何正则面对的都是 ≤ max_input_chars 的短文本。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .normalize import Normalized

log = logging.getLogger("intent-router")

MAX_RULES = 200
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MATCH_MODES = ("fullmatch", "search")
_TARGETS = ("text", "compact")

# ReDoS 形态启发式：嵌套量词 / 反向引用 / 连续无界通配
_REDOS_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("嵌套量词", re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")),
    ("反向引用", re.compile(r"\\[1-9]")),
    ("连续无界通配", re.compile(r"(\.[*+]){2,}")),
)

# ── 追问护栏（继承上文的输入不值得识别，更不该直连工具）────────────────────
# 命中即整条跳过（不分类、不注入、不调工具）：这些输入的真实意图在上一轮里。
_FOLLOW_UP_EXACT = frozenset(
    {
        "继续", "接着", "然后", "然后呢", "还有", "还有呢", "再来", "往下",
        "详细点", "再详细点", "具体点", "展开说说", "说说看", "为什么", "为啥",
        "咋回事", "怎么回事", "为何", "好的", "好", "嗯", "嗯嗯", "收到", "明白",
        "谢谢", "多谢", "感谢", "辛苦了", "是", "对", "对的", "是的", "没错",
        "确实", "可以", "行", "没问题", "ok", "okay", "yes", "no", "continue",
        "go on", "next", "thanks", "thank you", "thx", "别的呢", "其他的呢",
    }
)
_FOLLOW_UP_RE = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(继续|接着|然后|往下|再来)(说|讲|写|查|看|分析|做|执行)(一下|下)?$",
        r"^(继续|接着|然后|往下)(一下|下)?$",
        r"^(再)?(详细|具体|展开|解释|补充)(一点|点|一下|下|些)?(说|讲|说说|讲讲)?$",
        r"^(为什么|为啥|为何|咋回事|怎么回事|什么原因)(呢|啊|呀)?$",
        r"^(还有|还有别的|其他|其他呢|别的|别的呢|然后)(呢)?$",
        r"^(好的|行|可以|收到|明白|懂了|了解)(的)?$",
        r"^(谢谢|多谢|感谢|辛苦了)(你|您)?(了)?$",
    )
)
_FOLLOW_UP_MAX_CHARS = 12


@dataclass(frozen=True)
class Rule:
    """一条已编译的规则。``slots`` 把正则命名组映射到参数名（可做常量映射）。"""

    id: str
    intent: str
    pattern: re.Pattern
    match_mode: str = "fullmatch"
    priority: int = 0
    target: str = "text"
    slots: Dict[str, str] = field(default_factory=dict)
    order: int = 0


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    intent: str
    slots: Dict[str, str] = field(default_factory=dict)
    priority: int = 0


def lint_pattern(pattern: str) -> List[str]:
    """返回该正则的 ReDoS 风险提示（空列表表示没发现已知高危形态）。"""
    findings: List[str] = []
    for label, probe in _REDOS_PATTERNS:
        if probe.search(pattern):
            findings.append(label)
    return findings


def compile_rules(
    raw_rules: Optional[Iterable[Mapping[str, Any]]],
    *,
    max_rules: int = MAX_RULES,
) -> Tuple[List[Rule], List[str]]:
    """校验并编译规则表；返回 (已排序规则, 告警列表)。绝不抛出。"""
    warnings: List[str] = []
    compiled: List[Rule] = []
    seen_ids: set = set()
    count = 0
    for index, raw in enumerate(raw_rules or []):
        if not isinstance(raw, Mapping):
            warnings.append("rules[%d]: 不是映射，跳过" % index)
            continue
        rule_id = str(raw.get("id") or "").strip()
        intent = str(raw.get("intent") or "").strip()
        pattern_text = raw.get("pattern")
        if not rule_id or not intent or not isinstance(pattern_text, str) or not pattern_text:
            warnings.append("rules[%d]: 缺少 id/intent/pattern，跳过" % index)
            continue
        if not _ID_RE.match(rule_id):
            warnings.append("rule %r: id 非法（^[a-z0-9][a-z0-9._-]{0,63}$），跳过" % rule_id)
            continue
        if rule_id in seen_ids:
            warnings.append("rule %r: id 重复，后者生效" % rule_id)
            compiled = [rule for rule in compiled if rule.id != rule_id]
        seen_ids.add(rule_id)

        match_mode = str(raw.get("match") or "fullmatch").strip().lower()
        if match_mode not in _MATCH_MODES:
            warnings.append("rule %r: match=%r 非法，按 fullmatch 处理" % (rule_id, match_mode))
            match_mode = "fullmatch"
        target = str(raw.get("target") or "text").strip().lower()
        if target not in _TARGETS:
            warnings.append("rule %r: target=%r 非法，按 text 处理" % (rule_id, target))
            target = "text"

        for finding in lint_pattern(pattern_text):
            warnings.append("rule %r: 正则含%s形态，注意回溯风险" % (rule_id, finding))

        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            warnings.append("rule %r: 编译失败（%s），跳过" % (rule_id, exc))
            continue

        slots_raw = raw.get("slots")
        slots: Dict[str, str] = {}
        if isinstance(slots_raw, Mapping):
            for group_name, arg_name in slots_raw.items():
                group = str(group_name)
                if group not in pattern.groupindex:
                    warnings.append("rule %r: slots 引用了不存在的命名组 %r，忽略" % (rule_id, group))
                    continue
                if not str(arg_name).strip():
                    continue
                slots[group] = str(arg_name).strip()
        elif slots_raw is not None:
            warnings.append("rule %r: slots 不是映射，忽略" % rule_id)

        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            warnings.append("rule %r: priority 不是整数，按 0 处理" % rule_id)
            priority = 0

        if count >= max_rules:
            warnings.append("规则数超过上限 %d，rule %r 及之后被丢弃" % (max_rules, rule_id))
            break
        count += 1
        compiled.append(
            Rule(
                id=rule_id,
                intent=intent,
                pattern=pattern,
                match_mode=match_mode,
                priority=priority,
                target=target,
                slots=slots,
                order=index,
            )
        )

    # priority 降序、同优先级按声明顺序（稳定）
    compiled.sort(key=lambda rule: (-rule.priority, rule.order))
    return compiled, warnings


def match_guard(text: Normalized, *, continuation_guard: bool = True) -> Optional[str]:
    """前置护栏：返回跳过原因（``empty``/``follow_up``），None 表示继续识别。"""
    if text.empty:
        return "empty"
    if not continuation_guard:
        return None
    if len(text.compact) > _FOLLOW_UP_MAX_CHARS:
        return None
    if text.compact in _FOLLOW_UP_EXACT:
        return "follow_up"
    for pattern in _FOLLOW_UP_RE:
        if pattern.match(text.text):
            return "follow_up"
    return None


def match_rules(rules: Sequence[Rule], text: Normalized) -> Optional[RuleHit]:
    """按序执行规则；首个命中生效。命中即返回，绝不抛出。"""
    if text.empty:
        return None
    for rule in rules:
        haystack = text.compact if rule.target == "compact" else text.text
        if not haystack:
            continue
        try:
            if rule.match_mode == "fullmatch":
                match = rule.pattern.fullmatch(haystack)
            else:
                match = rule.pattern.search(haystack)
        except Exception:
            log.warning("intent-router: 规则 %s 执行异常，跳过本规则", rule.id, exc_info=True)
            continue
        if not match:
            continue
        slots: Dict[str, str] = {}
        try:
            groups = match.groupdict()
        except Exception:
            groups = {}
        for group_name, arg_name in rule.slots.items():
            value = groups.get(group_name)
            if value:
                slots[arg_name] = value
        return RuleHit(rule_id=rule.id, intent=rule.intent, slots=slots, priority=rule.priority)
    return None
