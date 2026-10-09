"""entities.py — 实体抽取（第 0 步）。

实体在三层里有两份工作：

1. **证据**：消息里出现 ``-4073`` 或 ``ORA-01555`` 本身就是强意图信号（第二层给加成）；
2. **入参**：命中意图后直连工具时，实体直接填进工具参数（``error_code=...``）。

错误码族表与归一化口径**对齐 i2stream-bkn**（``plugin/constants.py`` 的
``DB_ERROR_PREFIXES`` 与 ``normalize_error_code``）：``ORA01555 → ORA-01555``、
裸 ``4073 → -4073``（i2Stream 内部错误码为负数）。此处是等价实现而非跨仓 import，
词表快照由 ``tools/gen_lexicon.py`` 生成并带源哈希，便于漂移检测。

两个容易踩的细节，本模块显式处理：

- **不能用 ``\\b``**：中文是 Python 正则里的词字符，``报错ORA-01555`` 里 ``\\bORA``
  永远匹配不上。统一改用「前/后不是 ASCII 字母数字」的 lookaround。
- **裸数字要打分**：``日志第 100 行报错 -4073`` 里的 ``100`` 也是 3 位数字。带负号
  优先、紧邻错误码关键词次之，都没沾上的裸数字不当错误码；``1900-2099`` 年份形状
  直接排除；八位组做了范围校验，避免把版本号当成 IP。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 「前/后不是 ASCII 字母数字」——中文旁的实体也能匹配（\b 在这里会失效）
_L = r"(?<![a-z0-9])"
_R = r"(?![a-z0-9])"

# ── 错误码族（对齐 i2stream-bkn DB_ERROR_PREFIXES 的口径）─────────────────
# (正则, db_type, 归一化模板)；模板里 {0} 是捕获到的数字/编码部分。
_FAMILY_PATTERNS: Tuple[Tuple[str, re.Pattern, str, str], ...] = (
    ("oracle", re.compile(_L + r"ora[-_]?\s?([0-9]{3,6})" + _R), "oracle", "ORA-{0}"),
    ("yashandb", re.compile(_L + r"yas[-_]?\s?([0-9]{3,6})" + _R), "yashandb", "YAS-{0}"),
    ("postgresql", re.compile(_L + r"pg[-_]?\s?([0-9]{3,6})" + _R), "postgresql", "PG-{0}"),
    ("mysql", re.compile(_L + r"mysql[-_]?\s?([0-9]{3,6})" + _R), "mysql", "MYSQL{0}"),
    ("sqlserver", re.compile(_L + r"mssql[-_]?\s?([0-9]{3,6})" + _R), "sqlserver", "MSSQL{0}"),
    ("db2", re.compile(_L + r"db2[-_]?\s?([a-z]?[0-9]{3,6}[a-z]?)" + _R), "db2", "DB2{0}"),
)

# 裸错误码：3-5 位数字（可带负号）。i2Stream 内部错误码是负数。
_BARE_CODE_RE = re.compile(_L + r"(-?)([0-9]{3,5})" + _R)
# 年份形状（1900-2099）不是错误码；端口/行号之类靠「无符号且无上下文」过滤
_YEAR_RE = re.compile(r"^(?:19|20)[0-9]{2}$")
# 错误码上下文关键词（裸数字只有紧邻这些词才算错误码）
_CODE_CONTEXT_RE = re.compile(
    r"(错误码|错误号|报错|异常码|返回码|errno|error\s*code|err\s*no|code)"
)
# 上下文窗口：命中位置前后各这么多字符内找关键词
_CONTEXT_WINDOW = 10

# 数量词/单位：数字紧跟这些词时是「行号/条数/台数」，不是错误码（"100 行"、"3 台"）
_UNIT_AFTER_RE = re.compile(
    r"\s*(行|个|号|条|次|台|页|天|秒|分钟|小时|版|路|块|张|位|件|兆|%|gb|mb|kb)"
)

_IP_RE = re.compile(
    r"(?<![0-9.])((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?::([0-9]{1,5}))?(?![0-9.])"
)
# 规则号：LP-0001 / LP0001（log-mode 目录的日志模式编号）
_RULE_ID_RE = re.compile(_L + r"lp[-_]?\s?([0-9]{3,5})" + _R)

# 时间窗：最近 30 分钟 / 近 3 天 / 今天 / 昨天 …
_TIME_REL_RE = re.compile(r"(?:最近|近|过去)\s?([0-9]{1,3})\s?(分钟|分|小时|钟头|天|日)")
_TIME_ABS = (
    (re.compile(r"今天|今日|今早|今儿"), "today"),
    (re.compile(r"昨天|昨日|昨晚"), "yesterday"),
    (re.compile(r"上周"), "last_week"),
    (re.compile(r"本周|这周"), "this_week"),
)
_TIME_UNIT = {"分钟": "m", "分": "m", "小时": "h", "钟头": "h", "天": "d", "日": "d"}

# 只提到库名（没有具体错误码）也能定 db_type；同样用 lookaround 而非 \b
_DB_MENTIONS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("oracle", re.compile(_L + r"(?:oracle|ora)" + _R)),
    ("yashandb", re.compile(_L + r"(?:yashandb|yas)" + _R)),
    ("postgresql", re.compile(_L + r"(?:postgresql|postgres|pgsql|pg)" + _R)),
    ("mysql", re.compile(_L + r"mysql" + _R)),
    ("sqlserver", re.compile(_L + r"(?:sqlserver|mssql|sql\s?server)" + _R)),
    ("db2", re.compile(_L + r"db2" + _R)),
    ("oceanbase", re.compile(_L + r"oceanbase" + _R)),
)

# 实体名 → 抽取结果键（入参映射与证据统计都用这组名字）
ENTITY_NAMES = ("error_code", "db_type", "ip", "port", "rule_id", "time_window")


@dataclass
class Entities:
    """一次抽取的结果。``values`` 只放最终归一化后的值。"""

    values: Dict[str, str] = field(default_factory=dict)
    spans: List[Tuple[int, int]] = field(default_factory=list)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.values

    def names(self) -> List[str]:
        return sorted(self.values.keys())

    def __bool__(self) -> bool:
        return bool(self.values)


def normalize_error_code(value: str) -> str:
    """对齐 i2stream-bkn 的口径：家族码补/规范分隔符，裸数字补负号。"""
    token = (value or "").strip().upper().replace(" ", "")
    if not token:
        return ""
    token = token.replace("_", "-")
    for _name, pattern, _db, template in _FAMILY_PATTERNS:
        match = pattern.search(token.casefold())
        if match:
            return template.format(match.group(1).upper())
    digits = token.lstrip("-")
    if digits.isdigit() and not _YEAR_RE.match(digits):
        return "-" + digits
    return token


def _looks_like_ipv4(candidate: str) -> bool:
    parts = candidate.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def _has_code_context(text: str, start: int, end: int) -> bool:
    """命中位置附近（±窗口）是否有错误码关键词。"""
    left = max(0, start - _CONTEXT_WINDOW)
    right = min(len(text), end + _CONTEXT_WINDOW)
    return bool(_CODE_CONTEXT_RE.search(text[left:right]))


def _pick_bare_code(text: str, compact: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    """从裸数字候选里挑错误码。

    打分：带负号 3 分 / 顶在句首 2 分 / 紧邻错误码关键词 2 分 / 其他 1 分；
    最高分并列取先出现者。除「整句就是它」外，得分 <2 的候选不认（避免把端口、行号、
    版本号当错误码）；年份形状一律排除。

    「顶在句首」单独给分是有来历的：``4073 是什么意思`` 是最常见的问法，但句子里
    并没有「错误码」这类关键词——不给句首权重，这类输入就抽不到实体，
    act 档位会因为 ``requires: [error_code]`` 缺值而退化（实测踩过）。
    """
    best_score = 0
    best: Optional[Tuple[str, Tuple[int, int]]] = None
    for match in _BARE_CODE_RE.finditer(text):
        sign, digits = match.group(1), match.group(2)
        if _YEAR_RE.match(digits):
            continue
        whole_message = compact in (digits, "-" + digits)
        if whole_message:
            return "-" + digits, match.span()
        score = 1
        if sign == "-":
            score += 2
        elif match.start() == 0 and not _UNIT_AFTER_RE.match(text[match.end():]):
            # 句首裸数字优先当错误码——但后面紧跟数量词时是行号/条数，不给这个分
            score += 1
        if _has_code_context(text, match.start(), match.end()):
            score += 1
        if score > best_score:
            best_score, best = score, ("-" + digits, match.span())
    if best is not None and best_score >= 2:
        return best
    return None


def extract(text: str, compact: str = "") -> Entities:
    """从**归一化**文本抽取实体。任何异常退化为空结果，绝不抛出。

    内部再 casefold 一次：契约上文本已归一化，但抽取是纯函数、且 ``ORA-01555`` 与
    ``ora-01555`` 必须同形，自己兜住大小写能免掉一整类调用顺序错误。
    """
    result = Entities()
    if not text:
        return result
    text = text.casefold()
    compact = (compact or text).casefold()
    try:
        for db_name, pattern, db_type, template in _FAMILY_PATTERNS:
            match = pattern.search(text)
            if match:
                result.values["error_code"] = template.format(match.group(1).upper())
                result.values["db_type"] = db_type
                result.spans.append(match.span())
                break
        else:
            bare = _pick_bare_code(text, compact or "")
            if bare is not None:
                result.values["error_code"] = bare[0]
                result.spans.append(bare[1])

        ip_match = _IP_RE.search(text)
        if ip_match and _looks_like_ipv4(ip_match.group(1)):
            # 点分版本号（如 1.2.3.4 形态但八位组合法）无法与 IP 区分，按 IP 认；
            # 端口只收 1..65535。
            result.values["ip"] = ip_match.group(1)
            if ip_match.group(2):
                port = int(ip_match.group(2))
                if 0 < port <= 65535:
                    result.values["port"] = ip_match.group(2)
            result.spans.append(ip_match.span())

        rule_match = _RULE_ID_RE.search(text)
        if rule_match:
            result.values["rule_id"] = "LP-" + rule_match.group(1)
            result.spans.append(rule_match.span())

        rel = _TIME_REL_RE.search(text)
        if rel:
            result.values["time_window"] = "%s%s" % (rel.group(1), _TIME_UNIT.get(rel.group(2), "m"))
            result.spans.append(rel.span())
        else:
            for pattern, label in _TIME_ABS:
                if pattern.search(text):
                    result.values["time_window"] = label
                    break

        if "db_type" not in result.values:
            for db_type, pattern in _DB_MENTIONS:
                if pattern.search(text):
                    result.values["db_type"] = db_type
                    break
        return result
    except Exception:
        return Entities()
