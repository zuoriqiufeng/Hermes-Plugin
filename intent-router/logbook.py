"""logbook.py — 插件自有的**人类可读**日志（可配目录 / 按大小+按日期双轮转 / 文件名带日期）。

与 ``telemetry.py`` 的 ``decisions.jsonl`` 是**双轨**关系，各司其职：

- JSONL：机器数据源，供 ``/intent stats``、``/intent tune`` 做聚合与扫参；
- 本模块：人读，供 ``tail``/``grep`` 排查「这条为什么这么判」「刚才到底做了什么」。

文件名与轮转::

    <log_dir>/intent-router-2026-10-09.log       ← 当天第 1 个分片（活动文件）
    <log_dir>/intent-router-2026-10-09.1.log     ← 当天超 log_max_bytes 后的第 2 个分片

- **按日期**：跨本地零点后的第一条日志自动落到新日期文件；
- **按大小**：单文件达 ``log_max_bytes`` 即关闭并新开同日的下一个序号分片；
- **同日分片上限** ``log_backups_per_day``，超出后停止写入并只 warn 一次（防失控）；
- **按日期保留** ``log_retention_days``：文件名带日期，轮转时清理超期文件。

三点刻意的设计选择：

1. **不用 ``logging.handlers``**：``TimedRotatingFileHandler`` 的当前文件不带日期，
   ``RotatingFileHandler`` 又不看日期，「日期在文件名 + 双轮转」这个组合 stdlib 没有现成的；
   自己实现反而更短、更好预测（本机惯例也如此：i2stream-bkn 的 ``log_rotate.py`` 同款语义）。
2. **只追加、从不重命名**：滚动时新开分片而不是把旧文件改名——重命名在**多进程**下会互踩
   （gateway / CLI / profile worker 都在写同一份），而 ``O_APPEND`` 下每行一次 ``write``
   不会交错。最坏情况只是两个进程同时决定进同一分片，一起追加，不丢数据。
3. **自己脱敏**：宿主只给自己的 handler 套 ``RedactingFormatter``，插件自有文件享受不到，
   所以落盘前统一走 ``safety.redact_text``（``decisions.jsonl`` 的 preview 同样如此）。

fail-open：任何 IO 异常只 warn 一次后放弃当行，绝不抛出——日志故障不允许影响对话。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .safety import redact_text, safe_preview

log = logging.getLogger("intent-router")

LOG_PREFIX = "intent-router-"
LOG_SUFFIX = ".log"
# 文件名里的日期段：intent-router-YYYY-MM-DD[.N].log
_NAME_RE = re.compile(r"^%s(\d{4}-\d{2}-\d{2})(?:\.(\d+))?%s$" % (re.escape(LOG_PREFIX),
                                                                 re.escape(LOG_SUFFIX)))
_DATE_FMT = "%Y-%m-%d"
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_LINE_LIMIT = 8000          # 单行硬上限（含预览），防一条超长输入把日志撑爆

_warned: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message, *args)


def _default_now() -> float:
    return time.time()


def date_stamp(now_fn: Callable[[], float] = _default_now) -> str:
    """本地日期串（YYYY-MM-DD）——按本地时区分日，符合运维直觉。"""
    return time.strftime(_DATE_FMT, time.localtime(now_fn()))


def shard_name(day: str, index: int = 0) -> str:
    """分片文件名：index 0 = 当天活动文件，>0 = 同日后续分片。"""
    if index <= 0:
        return "%s%s%s" % (LOG_PREFIX, day, LOG_SUFFIX)
    return "%s%s.%d%s" % (LOG_PREFIX, day, index, LOG_SUFFIX)


class DaySizeRotatingWriter:
    """按「日期 + 大小」滚动、只追加、线程安全的写入器。

    ``now_fn`` / ``days_fn`` 可注入，便于测试跨日与保留清理（不需要改系统时间）。
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backups_per_day: int = 3,
        retention_days: int = 7,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.directory = Path(directory)
        self.max_bytes = max(1024, int(max_bytes))
        self.backups_per_day = max(0, int(backups_per_day))
        self.retention_days = max(0, int(retention_days))
        self._now = now_fn or _default_now
        self._lock = threading.Lock()
        self._day: Optional[str] = None
        self._index = 0
        self._capped = False
        self.current_path: Optional[Path] = None
        # 预扫描一次本进程应续写的分片与需要清理的旧文件
        with self._lock:
            self._sync_locked()

    # ── 对外 ────────────────────────────────────────────────────────────
    def write_line(self, line: str) -> bool:
        """写一行（自动补换行）。返回是否写入成功；任何失败都不抛出。"""
        if not line:
            return False
        text = line if line.endswith("\n") else line + "\n"
        if len(text) > _LINE_LIMIT:
            text = text[:_LINE_LIMIT - 2] + "…\n"
        try:
            encoded = len(text.encode("utf-8", errors="replace"))
            with self._lock:
                self._sync_locked()
                if self._capped:
                    return False
                path = self.current_path
                if path is None:
                    return False
                # 大小判定用实际文件尺寸（多进程共写时本进程计数会漂移，stat 更准）
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if size + encoded > self.max_bytes:
                    if not self._roll_size_locked():
                        return False
                    path = self.current_path
                    if path is None:
                        return False
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", errors="replace") as handle:
                    handle.write(text)
                return True
        except Exception as exc:
            _warn_once("write", "intent-router: 写日志失败（%s）：%s", self.directory, exc)
            return False

    def current_size(self) -> int:
        path = self.current_path
        try:
            return path.stat().st_size if path and path.exists() else 0
        except OSError:
            return 0

    def files(self) -> List[Path]:
        """当前目录下本插件的日志文件，按**（日期, 分片序号）**排序。

        刻意不用 ``sorted()`` 的字典序：``intent-router-2026-10-09.1.log`` 会排在
        ``intent-router-2026-10-09.log`` 前面（'.' 后是 '1' < 'l'），与人的直觉相反。
        """
        try:
            entries: List[Tuple[str, int, Path]] = []
            for path in self.directory.glob("%s*%s" % (LOG_PREFIX, LOG_SUFFIX)):
                match = _NAME_RE.match(path.name)
                if not match:
                    continue
                entries.append((match.group(1), int(match.group(2) or 0), path))
            entries.sort(key=lambda item: (item[0], item[1]))
            return [path for _day, _index, path in entries]
        except OSError:
            return []

    def tail(self, limit: int = 50, *, max_bytes: int = 256 * 1024) -> List[str]:
        """读当前分片最近 ``limit`` 行（最多回读 ``max_bytes``）；坏尾行自动丢弃。"""
        path = self.current_path
        if path is None or limit <= 0 or not path.exists():
            return []
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                start = max(0, size - max_bytes)
                handle.seek(start)
                blob = handle.read()
            lines = blob.decode("utf-8", errors="replace").splitlines()
            if start > 0 and lines:
                lines = lines[1:]              # 首行可能被截断
            return [line for line in lines[-limit:] if line.strip()]
        except Exception:
            return []

    # ── 内部 ────────────────────────────────────────────────────────────
    def _sync_locked(self) -> None:
        """对齐到「今天的正确分片」，并顺带清理超期文件。调用方持锁。"""
        day = date_stamp(self._now)
        if self._day != day:
            self._day = day
            self._index = self._resume_index(day)
            self._capped = False
            self.current_path = self.directory / shard_name(day, self._index)
            self._prune_locked(day)
        elif self.current_path is None:
            self.current_path = self.directory / shard_name(day, self._index)

    def _resume_index(self, day: str) -> int:
        """续写当天最后一个未超限分片（多进程/重启后不覆盖已有内容）。"""
        index = 0
        while index < self.backups_per_day:
            candidate = self.directory / shard_name(day, index)
            try:
                if not candidate.exists() or candidate.stat().st_size < self.max_bytes:
                    return index
            except OSError:
                return index
            index += 1
        return self.backups_per_day          # 已达上限：只剩最后一个分片可用

    def _roll_size_locked(self) -> bool:
        """满容量后新开同日下一个分片。返回是否成功拿到可写分片。调用方持锁。"""
        if self._day is None:
            return False
        next_index = self._index + 1
        if next_index > self.backups_per_day:
            self._capped = True
            _warn_once(
                "capped:%s" % self._day,
                "intent-router: %s 的日志已达 %d 个分片（每个 %d 字节），本进程停止写入；"
                "请调大 log_backups_per_day / log_max_bytes 或缩短 log_retention_days",
                self._day, self.backups_per_day + 1, self.max_bytes,
            )
            return False
        self._index = next_index
        self.current_path = self.directory / shard_name(self._day, self._index)
        return True

    def _prune_locked(self, today: str) -> None:
        """删除文件名日期早于 ``retention_days`` 的日志。调用方持锁。"""
        if self.retention_days <= 0:
            return
        try:
            cutoff = time.strftime(
                _DATE_FMT, time.localtime(self._now() - self.retention_days * 86400)
            )
        except Exception:
            return
        for path in self.files():
            match = _NAME_RE.match(path.name)
            if not match or match.group(1) > cutoff or match.group(1) == today:
                continue
            try:
                path.unlink()
            except OSError:
                _warn_once("prune", "intent-router: 清理旧日志失败：%s", path)


# ── 行格式 ──────────────────────────────────────────────────────────────


def format_decision_line(record: Dict[str, Any], *, keyword: str = "决策") -> str:
    """把 ``telemetry`` 同款记录字典渲染成一行人类可读文本。

    刻意直接吃记录字典（而不是再走一遍 ``Decision``）：两条轨道的字段口径永远一致，
    不会出现「JSONL 里有、文本日志里没有」这种对不上号的排查体验。
    """
    parts: List[str] = [keyword]
    intent = str(record.get("intent") or "")
    if intent:
        parts.append("意图=%s" % intent)
        parts.append("层=%s" % (record.get("tier") or "-"))
        parts.append("分数=%.2f" % float(record.get("score") or 0.0))
        parts.append("证据=%s" % int(record.get("sources") or 0))
    else:
        parts.append("结果=不表态")
        reason = str(record.get("skipped") or record.get("gate_reason") or "")
        if reason:
            parts.append("门控=%s" % reason)
    entities = record.get("entities") or {}
    if entities:
        parts.append("实体=" + ",".join("%s=%s" % (key, value)
                                       for key, value in sorted(entities.items())))
    candidates = record.get("candidates") or []
    if not intent and candidates:
        parts.append("候选=" + ",".join("%s=%.2f" % (name, float(score))
                                       for name, score in candidates[:3]))
    action = str(record.get("action") or "none")
    parts.append("动作=%s" % action)
    if record.get("tool"):
        parts.append("工具=%s" % record["tool"])
        if record.get("tool_ms"):
            parts.append("工具用时=%.0fms" % float(record["tool_ms"]))
    if record.get("tier3_used"):
        parts.append("第三层=%.0fms" % float(record.get("tier3_ms") or 0.0))
    if record.get("injected_chars"):
        parts.append("注入=%d" % int(record["injected_chars"]))
    parts.append("用时=%.2fms" % float(record.get("elapsed_ms") or 0.0))
    if record.get("session"):
        parts.append("会话=%s" % str(record["session"])[:16])
    parts.append("模式=%s" % (record.get("mode") or "-"))
    line = " ".join(parts)
    preview = str(record.get("preview") or "")
    if preview:
        line += " | 输入=" + preview
    elif record.get("hash"):
        line += " | 输入=#%s" % record["hash"]           # 不记原文时只留哈希
    return line


# ── 装配 ────────────────────────────────────────────────────────────────

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}


def level_of(name: str) -> int:
    return _LEVELS.get(str(name or "").strip().lower(), logging.INFO)


def format_line(
    created: float,
    level: int,
    message: str,
    *,
    include_pid: bool = False,
    redact: bool = True,
    pid: Optional[int] = None,
) -> str:
    """统一的行格式化（直接落盘路径与 logging handler 路径共用，保证格式一致）。"""
    stamp = time.strftime(_TS_FMT, time.localtime(created))
    prefix = ""
    if include_pid:
        prefix = " pid=%d" % (pid if pid is not None else os.getpid())
    body = redact_text(message) if redact else message
    return "%s %-7s%s %s" % (stamp, logging.getLevelName(level), prefix, body)


class Logbook:
    """插件日志门面：装配 handler、按级别产出决策行、提供 tail/describe。

    **两条写入路径，刻意分开**：

    - ``log_decision`` / ``log_event``：**直接写文件**，不经 stdlib logging。
      原因是踩过的坑——``hermes -z`` 一次性运行会在 ``hermes_cli/oneshot.py`` 里执行
      ``logging.disable(logging.CRITICAL)``（为的是不往 stderr 刷日志），而
      ``logging.disable`` 是在 **logger 层**拦记录，handler 根本收不到，于是插件日志
      在一路 ``-z`` 冒烟里整片消失。插件自己的审计文件不该被宿主的输出策略关掉。
    - ``log.warning/info`` 等模块诊断：走 stdlib logging（挂在本模块的 handler 上），
      **尊重宿主的日志策略**——宿主静默时它们也静默，这是对的（那些是控制台噪音策略）。
      为此，最需要留痕的诊断（启动摘要、意图表告警、工具降级）都额外走 ``log_event``。
    """

    def __init__(
        self,
        settings: Any,
        directory: Path,
        *,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.enabled = bool(getattr(settings, "log_enabled", True))
        self.level = level_of(getattr(settings, "log_level", "info"))
        self.decisions = bool(getattr(settings, "log_decisions", True))
        self.preview_chars = int(getattr(settings, "log_preview_chars", 120))
        self.redact = bool(getattr(settings, "log_redact", True))
        self.include_pid = bool(getattr(settings, "log_include_pid", False))
        self.max_bytes = int(getattr(settings, "log_max_bytes", 5 * 1024 * 1024))
        self.backups_per_day = int(getattr(settings, "log_backups_per_day", 3))
        self.retention_days = int(getattr(settings, "log_retention_days", 7))
        self.directory = Path(directory)
        self._now = now_fn or _default_now
        self._logger = logging.getLogger("intent-router")
        self._handler: Optional[logging.Handler] = None
        self._writer: Optional[DaySizeRotatingWriter] = None
        if self.enabled:
            self._install()

    # ── 装配 ────────────────────────────────────────────────────────────
    def _install(self) -> None:
        try:
            writer = DaySizeRotatingWriter(
                self.directory,
                max_bytes=self.max_bytes,
                backups_per_day=self.backups_per_day,
                retention_days=self.retention_days,
                now_fn=self._now,
            )
        except Exception:
            log.warning("intent-router: 日志写入器初始化失败，插件日志停用", exc_info=True)
            self.enabled = False
            return
        self._writer = writer
        try:
            handler = _WriterHandler(writer, self)
            handler.setLevel(self.level)
            self._logger.addHandler(handler)
            # 只调插件 logger 的级别：进不进宿主的 agent.log 仍由宿主的 logging.level 决定，
            # 所以 log_level=debug 的细节只出现在插件文件里，不会污染宿主日志。
            self._logger.setLevel(self.level)
            self._handler = handler
        except Exception:
            log.warning("intent-router: 日志 handler 装配失败，插件日志停用", exc_info=True)
            self.enabled = False

    # ── 产出 ────────────────────────────────────────────────────────────
    def _emit_direct(self, level: int, message: str) -> None:
        """直接落盘（绕过 stdlib logging 的全局 disable）。失败静默。"""
        if not (self.enabled and self._writer is not None):
            return
        if level < self.level:
            return
        try:
            self._writer.write_line(format_line(
                time.time(), level, message,
                include_pid=self.include_pid, redact=self.redact,
            ))
        except Exception:
            log.debug("intent-router: 直接写日志失败", exc_info=True)

    def log_decision(self, record: Dict[str, Any]) -> None:
        """按级别记一条决策（有意图→INFO，未表态/跳过→DEBUG）。失败静默。"""
        if not (self.enabled and self.decisions and self._writer is not None):
            return
        try:
            payload = dict(record)
            payload["preview"] = safe_preview(
                str(record.get("preview") or ""), self.preview_chars, redact=self.redact
            )
            level = logging.INFO if payload.get("intent") else logging.DEBUG
            self._emit_direct(level, format_decision_line(payload))
        except Exception:
            log.debug("intent-router: 写决策日志失败", exc_info=True)

    def log_event(self, level: int, message: str, *args: Any) -> None:
        """记一条非决策事件（启动/裁剪/命令/降级等）。失败静默。"""
        try:
            text = message % args if args else message
        except Exception:
            text = str(message)
        self._emit_direct(level, text)

    # ── 观测 ────────────────────────────────────────────────────────────
    def tail(self, limit: int = 50) -> List[str]:
        if self._writer is None:
            return []
        return self._writer.tail(limit)

    def current_path(self) -> Optional[Path]:
        return self._writer.current_path if self._writer else None

    def current_size(self) -> int:
        return self._writer.current_size() if self._writer else 0

    def file_count(self) -> int:
        return len(self._writer.files()) if self._writer else 0

    def describe(self) -> str:
        if not self.enabled:
            return "插件日志: 关闭（log_enabled=false）"
        path = self.current_path()
        return "插件日志: %s（级别 %s，当前分片 %d 字节，目录内 %d 个文件）" % (
            path or str(self.directory), logging.getLevelName(self.level),
            self.current_size(), self.file_count(),
        )

    def close(self) -> None:
        try:
            if self._handler is not None:
                self._logger.removeHandler(self._handler)
                self._handler.close()
        except Exception:
            log.debug("intent-router: 日志 handler 关闭失败", exc_info=True)


class _WriterHandler(logging.Handler):
    """把**模块诊断**（``log.info/warning/...``）落盘。

    ``emit`` 内的任何异常都被吞掉（logging 默认会把 handler 异常打到 stderr，
    对插件来说那属于噪音）：日志故障不允许影响对话。
    """

    def __init__(self, writer: DaySizeRotatingWriter, config: Logbook) -> None:
        super().__init__()
        self.writer = writer
        self._config = config

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.writer.write_line(format_line(
                record.created, record.levelno, record.getMessage(),
                include_pid=self._config.include_pid,
                redact=self._config.redact,
                pid=record.process,
            ))
        except Exception:
            pass

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: D102
        """覆盖默认实现：不往 stderr 打 traceback（日志故障静默，避免刷屏）。"""
        return None


def install(
    settings: Any,
    data_dir: Path,
    *,
    now_fn: Optional[Callable[[], float]] = None,
) -> Logbook:
    """按配置装配插件日志。

    ``log_dir`` 为空时落到 ``<data_dir>/logs``（即 plugin-data/intent-router/logs）；
    给绝对路径可指向任意位置（如 /var/log/i2stream/intent-router）。
    """
    configured = str(getattr(settings, "log_dir", "") or "").strip()
    directory = Path(os.path.expanduser(configured)) if configured else Path(data_dir) / "logs"
    return Logbook(settings, directory, now_fn=now_fn)
