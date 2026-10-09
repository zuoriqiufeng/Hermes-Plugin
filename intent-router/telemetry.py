"""telemetry.py — 决策日志（JSONL）与统计聚合。

落点：``<基座 hermes home>/plugin-data/intent-router/decisions.jsonl``
（与 hermes-dag 同构的 plugin-data 惯例；worker 以 profile 运行时 HERMES_HOME 指向
profiles/<name>，上溯到基座才能让 gateway 与 worker 写同一份数据）。

为什么用 JSONL 而不是数据库：这个插件的价值全在**调参与归因**——每一行都是一次
完整判定（层级/意图/分数/证据/门控原因/动作/耗时）。单文件、可 grep、可 tail、
可直接喂给 tune 脚本，比建表更实用。

容量护栏（照 reasoning-bridge 的做法）：单文件超过 ``max_bytes`` 就轮转为 ``.1``
（覆盖上一份），不无限增长；预览字段按 ``preview_chars`` 截断，**不落全文**。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("intent-router")

PLUGIN_KEY = "intent-router"
MAX_TAIL_BYTES = 4 * 1024 * 1024

_warned: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message, *args)


def shared_hermes_home() -> Path:
    """基座 home：从 profiles/<name> 上溯，保证多进程写同一份数据（同 hermes-dag）。"""
    home = os.environ.get("HERMES_HOME") or os.path.join(str(Path.home()), ".hermes")
    parts = Path(home).parts
    if "profiles" in parts:
        idx = parts.index("profiles")
        if idx:
            home = str(Path(*parts[:idx]))
    return Path(home)


def default_data_dir() -> Path:
    return shared_hermes_home() / "plugin-data" / PLUGIN_KEY


def default_log_dir() -> Path:
    """插件可读日志的默认目录（``log_dir`` 留空时的落点）。"""
    return default_data_dir() / "logs"


class Telemetry:
    """追加式 JSONL 写入器；任何 IO 异常只告警一次后吞掉（绝不打扰对话）。"""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        enabled: bool = True,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.enabled = bool(enabled)
        self.path = Path(path) if path else (default_data_dir() / "decisions.jsonl")
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._size: Optional[int] = None

    def _current_size(self) -> int:
        if self._size is None:
            try:
                self._size = self.path.stat().st_size if self.path.exists() else 0
            except OSError:
                self._size = 0
        return self._size

    def record(self, record: Dict[str, Any]) -> None:
        """写一行。``record`` 里的 ``kind`` 缺省为 ``decision``。"""
        if not self.enabled:
            return
        try:
            payload = dict(record)
            payload.setdefault("kind", "decision")
            payload.setdefault("ts", round(time.time(), 3))
            line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            encoded = len(line.encode("utf-8", errors="replace"))
            with self._lock:
                if self._current_size() + encoded > self.max_bytes:
                    self._rotate()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", errors="replace") as handle:
                    handle.write(line)
                self._size = self._current_size() + encoded
        except Exception as exc:
            _warn_once("write", "intent-router: 决策日志写入失败：%s", exc)

    def _rotate(self) -> None:
        """轮转为 .1（覆盖上一份）。调用方持锁。"""
        try:
            if self.path.exists():
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError as exc:
            _warn_once("rotate", "intent-router: 决策日志轮转失败：%s", exc)
        self._size = 0

    def tail(self, limit: int = 500, *, max_bytes: int = MAX_TAIL_BYTES) -> List[Dict[str, Any]]:
        """读最近 ``limit`` 行（最多回读 ``max_bytes``），坏行跳过。"""
        if not self.path.exists() or limit <= 0:
            return []
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as handle:
                start = max(0, size - max_bytes)
                handle.seek(start)
                blob = handle.read()
            lines = blob.decode("utf-8", errors="replace").splitlines()
            if start > 0 and lines:
                lines = lines[1:]  # 首行可能被截断
            out: List[Dict[str, Any]] = []
            for line in lines[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    out.append(parsed)
            return out
        except Exception as exc:
            _warn_once("tail", "intent-router: 决策日志读取失败：%s", exc)
            return []

    def close(self) -> None:
        """本实现逐行开关文件，无需常驻句柄；保留接口以便未来换成句柄缓存。"""
        return None


def percentile(values: Iterable[float], ratio: float) -> float:
    data = sorted(value for value in values if value is not None)
    if not data:
        return 0.0
    index = max(0, min(len(data) - 1, int(round(ratio * (len(data) - 1)))))
    return data[index]


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把若干条记录聚成统计（``/intent stats`` 与阈值调参共用）。"""
    decisions = [item for item in records if item.get("kind", "decision") == "decision"]
    prunes = [item for item in records if item.get("kind") == "prune"]
    tier_counts = Counter(str(item.get("tier") or "none") for item in decisions)
    intent_counts = Counter(str(item.get("intent") or "(none)") for item in decisions)
    action_counts = Counter(str(item.get("action") or "none") for item in decisions)
    gate_counts = Counter(str(item.get("gate_reason") or "") for item in decisions)
    skipped_counts = Counter(
        str(item.get("skipped")) for item in decisions if item.get("skipped")
    )
    tier3_used = sum(1 for item in decisions if item.get("tier3_used"))
    latencies = [float(item.get("elapsed_ms") or 0.0) for item in decisions]
    injected = sum(int(item.get("injected_chars") or 0) for item in decisions)
    # 影子期的关键口径：`action` 在 shadow 下记的是**反事实**（decision.py 的约定），
    # 所以「实际执行过工具」必须把 shadow 记录排除，否则 stats 会报出「触及工具 N 次」
    # 而实际上一次都没调——采数期最怕的就是被自己的读数骗到。
    acted = sum(1 for item in decisions
                if item.get("action") == "act" and str(item.get("mode") or "") != "shadow")
    would_act = sum(1 for item in decisions
                    if item.get("action") == "act" and str(item.get("mode") or "") == "shadow")
    total = len(decisions) or 1
    return {
        "records": len(records),
        "decisions": len(decisions),
        "tiers": dict(tier_counts),
        "intents": dict(intent_counts.most_common(20)),
        "actions": dict(action_counts),
        "gate_reasons": dict(gate_counts.most_common(12)),
        "skipped": dict(skipped_counts),
        "classified_rate": round(
            sum(1 for item in decisions if item.get("intent")) / total, 4
        ),
        "tier3_used": tier3_used,
        "tier3_rate": round(tier3_used / total, 4),
        "acted": acted,
        "act_rate": round(acted / total, 4),
        "would_act": would_act,
        "would_act_rate": round(would_act / total, 4),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.5), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "max": round(max(latencies) if latencies else 0.0, 2),
        },
        "injected_chars": injected,
        "injected_chars_est_tokens": int(injected / 3),  # 粗略估算：中英混排约 3 字符/token
        "prune_events": len(prunes),
        "prune_tools_removed": sum(int(item.get("removed") or 0) for item in prunes),
        "prune_chars_saved": sum(int(item.get("chars_saved") or 0) for item in prunes),
    }
