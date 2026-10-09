"""safety.py — 「落盘 / 注入前」的安全处理集中地。

两件事，方向不同但同源（都是把不可信文本交给别人之前先处理）：

``redact_text``    写给**磁盘**：插件自有的日志文件**不受宿主 ``RedactingFormatter``
                   保护**（那层只覆盖 Hermes 自己的 handler），所以凡是要落盘的文本
                   ——日志行里的输入预览、``decisions.jsonl`` 的 preview——都必须自己脱敏。
                   优先复用宿主实现 ``agent.redact.redact_sensitive_text``（这样
                   ``ctx.register_redaction_patterns`` 注册的自定义模式也一并生效）；
                   取不到时回落到内置模式，保证任何宿主版本下都不会裸写凭据。
``sanitize_data``  写给**模型**：注入块里的同名分隔符要清洗掉，否则工具输出/用户输入
                   可以伪造出 ``</FIELD_TEXT>`` 越界，把「数据」伪装成「指令」
                   （原在 actions.py，迁到这里让两处安全处理不再分散）。

失败策略：两个函数都不抛异常——安全处理本身故障时，宁可保守降级也要让对话继续。
"""
from __future__ import annotations

import logging
import re
from typing import Callable, List, Optional, Tuple, Union

log = logging.getLogger("intent-router")

FIELD_TAG = "FIELD_TEXT"
_FIELD_TAG_RE = re.compile(r"</?%s[^>]*>" % FIELD_TAG, re.IGNORECASE)

REDACTED = "***"
# 赋值式口令保留键名（``password=***`` 比整体抹掉更好排查：能看出这里原本是个口令字段）。
# 值的边界停在空白 / & / ; / , ——否则 ``?password=a&user=b`` 会把后面整串都吞掉。
_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s&;,\"']+)"
)
# scheme://user:pass@host —— 只抹凭据段，**主机与端口保留**（排查链路问题时最需要它们）
_URI_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]*):([^/\s:@]+)@")
_KEYSK_RE = re.compile(r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")
# JDBC thin 形式的内嵌凭据：jdbc:oracle:thin:scott/tiger@host:1521/orcl
# （`jdbc:oracle:thin:@host` 这种无凭据的形态不匹配——`@` 前面要求至少一个字符）
_JDBC_CRED_RE = re.compile(r"(?i)\bjdbc:[a-z0-9]+:[a-z0-9]+:[^@\s]+@")

Replacement = Union[str, Callable[[re.Match], str]]


def _keep_key(match: re.Match) -> str:
    return "%s%s%s" % (match.group(1), match.group(2), REDACTED)


def _keep_scheme(match: re.Match) -> str:
    return "%s%s:%s@" % (match.group(1), REDACTED, REDACTED)


def _jdbc_masked(_match: re.Match) -> str:
    return "jdbc:" + REDACTED + "@"


# 内置兜底规则：宿主脱敏不可用时使用；覆盖运维对话里最常出现的凭据形状。
# 刻意的取舍：**抹凭据、不抹主机**——日志得能回答「连的是哪台机/哪个库」，
# 那是排查链路问题时唯一有用的信息（把整条连接串抹掉会让日志失去价值）。
_BUILTIN_RULES: List[Tuple[re.Pattern, Replacement]] = [
    (_ASSIGN_RE, _keep_key),
    (_URI_CRED_RE, _keep_scheme),
    (_JDBC_CRED_RE, _jdbc_masked),
    (_KEYSK_RE, REDACTED),
    (_BEARER_RE, REDACTED),
]

_host_redactor: Optional[Callable[[str], str]] = None
_host_redactor_probed = False


def _host_redact(text: str) -> Optional[str]:
    """用宿主的脱敏实现处理；不可用/失败返回 None（只探测一次，避免重复 import 开销）。"""
    global _host_redactor, _host_redactor_probed
    if not _host_redactor_probed:
        _host_redactor_probed = True
        try:
            from agent.redact import redact_sensitive_text  # type: ignore
            _host_redactor = redact_sensitive_text
        except Exception:
            _host_redactor = None
            log.debug("intent-router: 宿主脱敏实现不可用，使用内置模式", exc_info=True)
    if _host_redactor is None:
        return None
    try:
        result = _host_redactor(text)
    except Exception:
        return None
    return result if isinstance(result, str) else None


def redact_text(text: str, *, enabled: bool = True) -> str:
    """脱敏任意文本。``enabled=False`` 时原样返回（对应 log_redact=false 的显式选择）。"""
    if not text or not enabled:
        return text or ""
    host_result = _host_redact(text)
    if host_result is not None:
        return host_result
    try:
        cleaned = text
        for pattern, replacement in _BUILTIN_RULES:
            cleaned = pattern.sub(replacement, cleaned)
        return cleaned
    except Exception:
        # 脱敏失败时不返回原文本（宁可整体隐去，也不冒泄露风险）
        return "[redact-failed]"


def sanitize_data(text: str) -> str:
    """清洗要注入模型的数据段：清掉同名分隔符、压掉过长的连续空行。

    这是防提示注入的必要一道：模型看到的分隔符只能由本插件产生。
    """
    if not text:
        return ""
    cleaned = _FIELD_TAG_RE.sub("[field]", str(text))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def safe_preview(text: str, limit: int, *, redact: bool = True) -> str:
    """日志预览统一口径：压平空白 → 脱敏 → 截断（顺序固定，长度语义稳定）。

    ``limit <= 0`` 表示**不落原文**（调用方自行用哈希代替），返回空串。
    """
    if limit <= 0 or not text:
        return ""
    flat = re.sub(r"\s+", " ", str(text)).strip()
    redacted = redact_text(flat, enabled=redact)
    return redacted if len(redacted) <= limit else redacted[:limit] + "…"
