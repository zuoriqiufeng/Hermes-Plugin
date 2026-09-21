"""reasoning-bridge — 把模型 reasoning 增量落到按会话隔离的 JSONL 文件。

观察者插件（不改 Hermes 本体、不进主 SSE 流）。模型的
``delta.reasoning_content`` 增量经 ``on_stream_delta`` hook（payload
``kind=="reasoning"``）到达本插件，追加写入::

    {HERMES_HOME}/plugins/reasoning-bridge/data/{session_id}.jsonl

外部控制面（i2Agent）tail 该文件，把思考增量转发给前端展示。

JSONL 行协议（每行一个完整 JSON 对象，UTF-8，``\\n`` 结尾）::

    {"t":"start","turn":"<turn_id>","ts":<epoch>}
    {"t":"delta","turn":"<turn_id>","ts":<epoch>,"text":"<思考增量>"}
    {"t":"end","turn":"<turn_id>","ts":<epoch>,"finished":<bool>}

前提配置：``~/.hermes/config.yaml`` 中 ``plugins.stream_reasoning_deltas:
true``（默认 false；为 false 时 reasoning 增量根本不会投递到本插件）。

失败策略：hook 是纯观察者，对话主流程不依赖其返回值；本插件内任何
IO/编码异常只记 warning 后吞掉，绝不影响对话。纯 stdlib，无自定义配置。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

# 单行 delta 的 text 上限：超限切片成多行 delta（读端按到达顺序拼接，
# 切分点无需对齐词边界）。按 UTF-8 字节数计，而非字符数。
MAX_TEXT_BYTES = 64 * 1024
# 单文件写入上限：达到后停止写入并只 warn 一次（防失控；正常思考流远达不到）。
MAX_FILE_BYTES = 5 * 1024 * 1024
# data 目录内超过该龄期的 .jsonl 在 housekeeping 时删除。
FILE_MAX_AGE_SECONDS = 24 * 3600
# housekeeping 最小间隔（借 on_stream_start 事件节流，不另起线程）。
HOUSEKEEPING_INTERVAL_SECONDS = 60.0

# session_id 直接做文件名，白名单校验防路径穿越（hermes 的 session_id
# 是无分隔符的短 id；命不中白名单的 payload 丢弃并 warn 一次）。必须以
# 字母数字开头，排除 "."/".." 之类仅由点构成的 id。
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 句柄/计数缓存。三个 hook 各自在独立 dispatcher 线程执行（见
# agent/plugin_stream_hooks.py），跨线程共享，统一由 _lock 保护。
_lock = threading.Lock()
_handles: Dict[str, Any] = {}   # 文件路径 str -> 追加模式句柄
_sizes: Dict[str, int] = {}     # 文件路径 str -> 已写字节数（含打开时存量）
_capped: set = set()            # 已达 5MB 上限、已 warn 过的路径
_last_housekeeping: float = 0.0

# warn-once 去重（独立锁，避免与写路径的 _lock 嵌套）。
_warn_lock = threading.Lock()
_warned: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message, *args)


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _data_dir() -> Path:
    return _hermes_home() / "plugins" / "reasoning-bridge" / "data"


def _session_path(session_id: str) -> Optional[Path]:
    if not session_id or not _SAFE_SESSION_RE.match(session_id):
        return None
    return _data_dir() / (session_id + ".jsonl")


def _chunk_text(text: str, limit: int = MAX_TEXT_BYTES) -> Iterator[str]:
    if len(text.encode("utf-8", errors="replace")) <= limit:
        yield text
        return
    buf: List[str] = []
    size = 0
    for ch in text:
        piece = len(ch.encode("utf-8", errors="replace"))
        if buf and size + piece > limit:
            yield "".join(buf)
            buf = []
            size = 0
        buf.append(ch)
        size += piece
    if buf:
        yield "".join(buf)


def _write_records(session_id: str, records: List[Dict[str, Any]]) -> None:
    """把若干条记录各写成一行（每行一次 write + 批末 flush）。

    绝不抛出：IO/编码失败只 warn（同路径去重）后吞掉。
    """
    if not records:
        return
    path = _session_path(session_id)
    if path is None:
        _warn_once(
            "session:%r" % (session_id,),
            "reasoning-bridge: 丢弃 payload——session_id 为空或含非法字符: %r",
            session_id,
        )
        return
    key = str(path)
    try:
        with _lock:
            handle = _handles.get(key)
            if handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                size = path.stat().st_size if path.exists() else 0
                handle = open(path, "a", encoding="utf-8", errors="replace")
                _handles[key] = handle
                _sizes[key] = size
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                encoded = len(line.encode("utf-8", errors="replace"))
                current = _sizes[key]
                if current + encoded > MAX_FILE_BYTES:
                    if key not in _capped:
                        _capped.add(key)
                        logger.warning(
                            "reasoning-bridge: %s 已达 %d 字节上限，停止写入",
                            path,
                            MAX_FILE_BYTES,
                        )
                    return
                handle.write(line)
                _sizes[key] = current + encoded
            handle.flush()
    except Exception as exc:
        _warn_once(
            "io:%s" % (key,),
            "reasoning-bridge: 写入 %s 失败: %s",
            path,
            exc,
        )


def _sweep_stale_files() -> None:
    """删除 data 目录下 mtime 超过 24h 的 .jsonl，并释放对应缓存句柄。"""
    try:
        data_dir = _data_dir()
        if not data_dir.is_dir():
            return
        cutoff = time.time() - FILE_MAX_AGE_SECONDS
        for path in data_dir.glob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                key = str(path)
                with _lock:
                    handle = _handles.pop(key, None)
                    _sizes.pop(key, None)
                    _capped.discard(key)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            except OSError:
                logger.debug("reasoning-bridge: 清理 %s 失败", path, exc_info=True)
    except Exception:
        logger.warning("reasoning-bridge: housekeeping 失败", exc_info=True)


def _housekeeping_throttled() -> None:
    global _last_housekeeping
    now = time.time()
    with _lock:
        if now - _last_housekeeping < HOUSEKEEPING_INTERVAL_SECONDS:
            return
        _last_housekeeping = now
    _sweep_stale_files()


# ── hooks ────────────────────────────────────────────────────────────────


def on_start(**payload: Any) -> None:
    try:
        _housekeeping_throttled()
        # touch 文件：让 i2Agent 在首个思考增量到达前就能打开并记录初始 size
        _write_records(
            payload.get("session_id") or "",
            [{"t": "start", "turn": payload.get("turn_id") or "", "ts": time.time()}],
        )
    except Exception:
        logger.warning("reasoning-bridge: on_stream_start 处理失败", exc_info=True)


def on_delta(**payload: Any) -> None:
    try:
        if payload.get("kind") != "reasoning":  # 忽略 kind=="text" 正文增量
            return
        text = payload.get("delta") or ""
        if not text:
            return
        turn = payload.get("turn_id") or ""
        ts = time.time()
        records = [
            {"t": "delta", "turn": turn, "ts": ts, "text": chunk}
            for chunk in _chunk_text(text)
        ]
        _write_records(payload.get("session_id") or "", records)
    except Exception:
        logger.warning("reasoning-bridge: on_stream_delta 处理失败", exc_info=True)


def on_end(**payload: Any) -> None:
    try:
        _write_records(
            payload.get("session_id") or "",
            [
                {
                    "t": "end",
                    "turn": payload.get("turn_id") or "",
                    "ts": time.time(),
                    "finished": bool(payload.get("finished")),
                }
            ],
        )
    except Exception:
        logger.warning("reasoning-bridge: on_stream_end 处理失败", exc_info=True)


def register(ctx: Any) -> None:
    ctx.register_hook("on_stream_start", on_start)
    ctx.register_hook("on_stream_delta", on_delta)
    ctx.register_hook("on_stream_end", on_end)
