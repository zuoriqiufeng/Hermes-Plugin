"""可读日志测试：双轮转（日期 + 大小）、保留清理、脱敏、预览口径、级别过滤、多进程追加、fail-open。

轮转类测试都通过可注入时钟（``now_fn``）来"穿越日期"，不去改系统时间——
这是 ``DaySizeRotatingWriter`` 特意留的测试缝。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import pytest

from conftest import submodule

LOGGER_NAME = "intent-router"


@pytest.fixture(autouse=True)
def _clean_plugin_logger():
    """每个用例前后清掉挂在插件 logger 上的 handler 并复位级别。

    ``intent-router`` 是全局单例 logger，多个用例各自 install 会互相串味；
    生产里 register() 每进程只跑一次，不存在这个问题。
    """
    logger = logging.getLogger(LOGGER_NAME)
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    yield
    for handler in list(logger.handlers):
        if handler not in saved_handlers:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    logger.setLevel(saved_level)


def _settings(**changes):
    value = submodule("config").Settings()
    value.normalize()
    for key, item in changes.items():
        setattr(value, key, item)
    return value


def _epoch(day: str, hour: int = 12) -> float:
    """把 'YYYY-MM-DD' 变成当天某时刻的本地时间戳。"""
    parsed = dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)
    return time.mktime(parsed.timetuple())


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _writer(tmp_path, **kwargs):
    logbook = submodule("logbook")
    options = {"max_bytes": 1024, "backups_per_day": 3, "retention_days": 7}
    options.update(kwargs)
    return logbook.DaySizeRotatingWriter(tmp_path / "logs", **options)


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _decision_lines(book):
    """取出决策行——同一文件里也可能有模块诊断行（设计如此：插件日志收全部）。
    不记原文的模式下行尾是 ``输入=#hash``，两种都算。"""
    return [line for line in book.tail(50) if " 输入=" in line]


# ── 文件名与轮转 ────────────────────────────────────────────────────────


def test_filename_contains_date(tmp_path):
    writer = _writer(tmp_path, now_fn=lambda: _epoch("2026-10-09"))
    assert writer.write_line("hello")
    expected = tmp_path / "logs" / "intent-router-2026-10-09.log"
    assert expected.exists()
    assert writer.current_path == expected
    assert _lines(expected) == ["hello"]


def test_continues_existing_shard_instead_of_overwriting(tmp_path):
    """重启/多进程接着写当天最后一个未满分片，不覆盖已有内容。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / ("intent-router-%s.log" % _today())).write_text("既有内容\n", encoding="utf-8")
    writer = _writer(tmp_path)
    writer.write_line("新增内容")
    content = _lines(log_dir / ("intent-router-%s.log" % _today()))
    assert content == ["既有内容", "新增内容"]


def test_size_rotation_opens_new_shard(tmp_path):
    writer = _writer(tmp_path, max_bytes=1024, backups_per_day=5)
    for index in range(80):
        writer.write_line("行 %03d %s" % (index, "x" * 60))
    day = _today()
    shards = writer.files()          # 语义排序：同日 .log → .1.log
    assert len(shards) >= 2, [p.name for p in shards]
    assert shards[0].name == "intent-router-%s.log" % day
    assert shards[1].name == "intent-router-%s.1.log" % day
    # 每个分片都在阈值附近封顶（允许单行超出阈值的余量），且内容完整无损
    for shard in shards:
        assert shard.stat().st_size <= 1024 + 128, shard.name
    collected = [line for shard in shards for line in _lines(shard)]
    assert collected == ["行 %03d %s" % (i, "x" * 60) for i in range(80)]
    assert writer.current_path == shards[-1]


def test_date_rotation_uses_injected_clock(tmp_path):
    stamp = {"now": _epoch("2026-10-09")}
    writer = _writer(tmp_path, now_fn=lambda: stamp["now"])
    writer.write_line("第一天")
    stamp["now"] = _epoch("2026-10-10")                # 跨日：不改系统时间，只推进时钟
    writer.write_line("第二天")

    first_day = tmp_path / "logs" / "intent-router-2026-10-09.log"
    second_day = tmp_path / "logs" / "intent-router-2026-10-10.log"
    assert _lines(first_day) == ["第一天"]
    assert _lines(second_day) == ["第二天"]


def test_per_day_shard_cap_stops_writing_without_raising(tmp_path, caplog):
    writer = _writer(tmp_path, max_bytes=1024, backups_per_day=1)   # 允许 2 个分片
    written = sum(1 for index in range(200) if writer.write_line("行 %03d %s" % (index, "y" * 80)))
    assert written < 200                               # 达上限后停止写入
    assert writer.write_line("再试一次") is False
    day = _today()
    shards = [p.name for p in writer.files()]
    assert shards == ["intent-router-%s.log" % day, "intent-router-%s.1.log" % day]


def test_retention_prunes_old_dated_files(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    for day in ("2026-09-01", "2026-10-08", "2026-10-09"):
        (log_dir / ("intent-router-%s.log" % day)).write_text("旧\n", encoding="utf-8")
    writer = _writer(tmp_path, retention_days=7, now_fn=lambda: _epoch("2026-10-09"))
    writer.write_line("今天")
    remaining = [p.name for p in writer.files()]
    assert "intent-router-2026-09-01.log" not in remaining      # 超期被清理
    assert "intent-router-2026-10-08.log" in remaining          # 未超期保留
    assert "intent-router-2026-10-09.log" in remaining


def test_retention_zero_keeps_everything(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "intent-router-2020-01-01.log").write_text("很旧\n", encoding="utf-8")
    writer = _writer(tmp_path, retention_days=0)
    writer.write_line("今天")
    assert (log_dir / "intent-router-2020-01-01.log").exists()


# ── fail-open ───────────────────────────────────────────────────────────


def test_unwritable_directory_never_raises(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("我不是目录", encoding="utf-8")            # 目录位置被文件占住
    writer = _writer(tmp_path)
    assert writer.write_line("写不进去") is False                # 静默失败，不抛异常


def test_disabled_log_writes_nothing(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_enabled=False), tmp_path / "plugin-data")
    book.log_decision({"intent": "x", "preview": "hi"})
    assert not (tmp_path / "plugin-data" / "logs").exists()
    assert "关闭" in book.describe()
    book.close()


# ── 多进程追加 ──────────────────────────────────────────────────────────


def test_two_writers_append_to_same_file(tmp_path):
    """两个进程（实例）同写一份：只追加、每行一次 write，行不交错。"""
    first = _writer(tmp_path, max_bytes=10 * 1024 * 1024)
    second = _writer(tmp_path, max_bytes=10 * 1024 * 1024)
    for index in range(50):
        first.write_line("A%03d 甲" % index)
        second.write_line("B%03d 乙" % index)
    path = tmp_path / "logs" / ("intent-router-%s.log" % _today())
    lines = _lines(path)
    assert len(lines) == 100
    assert all(re.fullmatch(r"[AB]\d{3} [甲乙]", line) for line in lines)   # 无残缺/拼接行


def test_long_line_is_capped(tmp_path):
    writer = _writer(tmp_path, max_bytes=64 * 1024)      # 给足空间，确保落在首个分片
    writer.write_line("长" * 20000)
    line = _lines(tmp_path / "logs" / ("intent-router-%s.log" % _today()))[0]
    assert len(line) <= 8000


# ── 行格式与级别 ────────────────────────────────────────────────────────


def test_decision_line_format_and_level_filtering(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_level="info", log_preview_chars=40),
                           tmp_path / "plugin-data")
    book.log_decision({"intent": "diag_error_code", "tier": "regex", "score": 1.0, "sources": 3,
                       "action": "hint", "injected_chars": 12, "elapsed_ms": 0.08,
                       "session": "mdv2s1u0", "mode": "shadow", "preview": "4073 是什么意思"})
    book.log_decision({"intent": "", "gate_reason": "no_evidence", "action": "none",
                       "elapsed_ms": 0.15, "preview": "今天天气不错"})       # 未表态 → DEBUG
    lines = _decision_lines(book)
    book.close()

    assert len(lines) == 1                       # info 级别下未表态不落盘
    line = lines[0]
    for token in ("INFO", "意图=diag_error_code", "层=regex", "分数=1.00", "证据=3",
                  "动作=hint", "注入=12", "用时=0.08ms", "会话=mdv2s1u0", "输入=4073 是什么意思"):
        assert token in line, (token, line)

    book_debug = logbook.install(_settings(log_level="debug"), tmp_path / "plugin-data2")
    book_debug.log_decision({"intent": "", "gate_reason": "score_below_min", "action": "none",
                             "candidates": [["diag_error_code", 0.41]], "elapsed_ms": 0.2,
                             "preview": "这个需求什么时候能上线"})
    debug_lines = _decision_lines(book_debug)
    book_debug.close()
    assert len(debug_lines) == 1
    assert "DEBUG" in debug_lines[0]
    assert "结果=不表态" in debug_lines[0] and "门控=score_below_min" in debug_lines[0]
    assert "候选=diag_error_code=0.41" in debug_lines[0]


def test_log_decisions_can_be_disabled(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_decisions=False), tmp_path / "plugin-data")
    book.log_decision({"intent": "x", "preview": "hi"})
    book.log_event(logging.INFO, "事件仍然记录")
    lines = book.tail(10)
    book.close()
    assert len(lines) == 1 and "事件仍然记录" in lines[0]


def test_survives_global_logging_disable(tmp_path):
    """宿主把 stdlib logging 全局关掉时，插件决策行仍要落盘。

    这是真实事故的回归锁：``hermes -z`` 一次性运行会执行
    ``logging.disable(logging.CRITICAL)``（hermes_cli/oneshot.py:182），而
    ``logging.disable`` 在 **logger 层**就拦掉记录、handler 根本收不到——
    凡是走 logging 的插件日志会整片消失（模块 ``log.warning`` 也确实会消失，
    那是尊重宿主的输出策略）。因此决策行/事件行走**直接落盘**路径。
    """
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_level="debug"), tmp_path / "plugin-data")
    logging.disable(logging.CRITICAL)
    try:
        book.log_decision({"intent": "diag_error_code", "preview": "-4073 是什么意思"})
        book.log_event(logging.WARNING, "工具降级：timeout")
        logging.getLogger(LOGGER_NAME).warning("模块告警（宿主静默时预期丢失）")
    finally:
        logging.disable(logging.NOTSET)
    lines = book.tail(20)
    book.close()
    joined = "\n".join(lines)
    assert "意图=diag_error_code" in joined          # 决策行必须留下
    assert "工具降级：timeout" in joined             # 事件行必须留下
    assert "模块告警" not in joined                   # 模块日志尊重宿主静默策略


def test_include_pid_prefixes_line(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_include_pid=True), tmp_path / "plugin-data")
    book.log_decision({"intent": "chitchat", "preview": "你好"})
    line = _decision_lines(book)[0]
    book.close()
    assert re.match(r"^\S+ \S+ INFO\s+pid=\d+ ", line), line


# ── 脱敏与预览 ──────────────────────────────────────────────────────────


def test_preview_is_redacted_before_writing(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_level="debug"), tmp_path / "plugin-data")
    book.log_decision({"intent": "diag_symptom_connection", "preview":
                       "连不上 mysql://root:Secr3t@10.0.0.5:3306/db password=Abc12345"})
    line = _decision_lines(book)[0]
    book.close()
    assert "Secr3t" not in line and "Abc12345" not in line
    assert "10.0.0.5" in line                      # 非凭据部分保留，仍可排查
    assert "***" in line


def test_redaction_can_be_disabled_explicitly(tmp_path):
    logbook = submodule("logbook")
    book = logbook.install(_settings(log_level="debug", log_redact=False),
                           tmp_path / "plugin-data")
    book.log_decision({"intent": "x", "preview": "password=Abc12345"})
    line = _decision_lines(book)[0]
    book.close()
    assert "Abc12345" in line                      # 显式关掉脱敏才允许原样落盘


def test_preview_length_limit_and_hash_only_mode(tmp_path):
    logbook = submodule("logbook")
    short = logbook.install(_settings(log_level="debug", log_preview_chars=8),
                            tmp_path / "p1")
    short.log_decision({"intent": "x", "preview": "这是一段很长的用户输入内容"})
    line = _decision_lines(short)[0]
    short.close()
    assert "输入=这是一段很长的用…" in line      # 8 字符 + 省略号

    hashed = logbook.install(_settings(log_level="debug", log_preview_chars=0),
                             tmp_path / "p2")
    hashed.log_decision({"intent": "x", "hash": "abcdef1234567890",
                         "preview": "这是一段很长的用户输入内容"})
    line2 = _decision_lines(hashed)[0]
    hashed.close()
    assert "这是一段很长的用户输入内容" not in line2      # 零原文落盘
    assert "输入=#abcdef1234567890" in line2


# ── 与 JSONL / 命令面的协同 ─────────────────────────────────────────────


def test_jsonl_preview_is_also_redacted(router, settings):
    """telemetry 的预览同样脱敏（插件自有文件不受宿主脱敏保护，两个落盘点都要管）。"""
    outcome = router.classify("连不上 mysql://root:Secr3t@10.0.0.5:3306/db password=Abc12345")
    record = outcome.decision.to_record(settings=settings, mode="shadow")
    assert "Secr3t" not in record["preview"] and "Abc12345" not in record["preview"]
    assert "10.0.0.5" in record["preview"]        # 主机保留：排查链路要用


def test_default_log_dir_follows_base_home(monkeypatch, tmp_path):
    telemetry = submodule("telemetry")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "dag-ops"))
    assert telemetry.default_log_dir() == tmp_path / ".hermes" / "plugin-data" / "intent-router" / "logs"


def test_install_honours_custom_dir(tmp_path):
    logbook = submodule("logbook")
    target = tmp_path / "custom" / "ir"
    book = logbook.install(_settings(log_dir=str(target)), tmp_path / "plugin-data")
    book.log_event(logging.INFO, "自定义目录")
    assert (target / ("intent-router-%s.log" % _today())).exists()
    assert not (tmp_path / "plugin-data" / "logs").exists()
    book.close()


def test_command_log_and_doctor_expose_logging(monkeypatch, tmp_path):
    """`/intent log` 能读出刚写的行，`/intent doctor` 报告日志路径与设置。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from conftest import FakeCtx, PLUGIN

    ctx = FakeCtx({"mode": "hint"})
    PLUGIN.register(ctx)
    ctx.hooks["pre_llm_call"](session_id="s1", turn_id="t1", user_message="-4073",
                              conversation_history=[], platform="api_server")
    handler = ctx.commands["intent"]
    log_out = handler("log 20")
    assert "diag_error_code" in log_out and "intent-router-" in log_out
    doctor = handler("doctor")
    assert "插件日志:" in doctor and "日志设置:" in doctor and "log_level" not in doctor
    assert "level=info" in doctor
