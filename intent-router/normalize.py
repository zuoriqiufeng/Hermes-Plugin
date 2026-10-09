"""normalize.py — 文本归一化（第 0 步，两层识别共用）。

产出三个视图，各司其职：

``text``    归一化正文：NFKC → 去控制/零宽字符 → 破折号统一 → 空白折叠 → casefold。
            保留标点，因为规则与实体抽取都要看标点（``诊断 -4073``、``IP:1521``）。
``compact`` 去掉所有非字母数字/汉字字符后的紧凑串。用于「整句就是一个实体」
            这类锚定判断（寒暄、追问、纯错误码），避免被空格/标点/全角干扰。
``raw``     原文（截断后的），只在需要回显或落日志预览时用。

归一化是**匹配语义的一部分**：所有规则、关键词、范例句都按归一化后的口径书写
（小写、半角、``-`` 统一）。这一点在 README 里对使用者明示。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# 各种破折号/连字符统一成 ASCII '-'（错误码 4073 / -4073 / －4073 / ORA–01555 要能等价）
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2043\u2212\uff0d\u30fc"
_DASH_TABLE = {ord(ch): "-" for ch in _DASHES}

# 零宽与方向控制字符：直接删除（复制粘贴的常见污染）
_ZERO_WIDTH = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u061c"
_ZERO_WIDTH_TABLE = {ord(ch): None for ch in _ZERO_WIDTH}

# C0/C1 控制字符（保留 \t \n \r 交给空白折叠处理）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Normalized:
    """一次归一化的结果；``text`` 为空表示这条输入不参与识别。"""

    raw: str
    text: str
    compact: str
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def digest(self) -> str:
        """归一化文本的短哈希：缓存键与日志去标识都用它。"""
        return hashlib.sha1(self.text.encode("utf-8", errors="replace")).hexdigest()[:16]


def compact_key(text: str) -> str:
    """去除非字母数字/汉字字符（含空白与标点）。"""
    return "".join(ch for ch in text if ch.isalnum())


def normalize(text: Optional[str], *, max_chars: int = 2000) -> Normalized:
    """归一化入口。任何异常都退化为「空输入」，绝不抛出。"""
    if not isinstance(text, str) or not text:
        return Normalized(raw="", text="", compact="")
    try:
        # 先在原文上截断一次，避免 NFKC 在大输入上做无用功（NFKC 可能膨胀，
        # 所以归一再按字符数截断一次，两次都以 max_chars 为界）。
        head = text[: max_chars * 2]
        normalized = unicodedata.normalize("NFKC", head)
        normalized = _CTRL_RE.sub(" ", normalized)
        normalized = normalized.translate(_ZERO_WIDTH_TABLE).translate(_DASH_TABLE)
        normalized = _WS_RE.sub(" ", normalized).strip().casefold()
        truncated = len(normalized) > max_chars or len(text) > max_chars
        if len(normalized) > max_chars:
            normalized = normalized[:max_chars].rstrip()
        return Normalized(
            raw=text[:max_chars],
            text=normalized,
            compact=compact_key(normalized),
            truncated=truncated,
        )
    except Exception:
        return Normalized(raw="", text="", compact="")


def compact_line(text: str) -> str:
    """压平空白（仅供展示/日志的轻量整理；落盘预览请用 safety.safe_preview，
    那里还会做脱敏——两步顺序不能颠倒，否则截断点上的凭据会残留）。"""
    return _WS_RE.sub(" ", (text or "").strip())
