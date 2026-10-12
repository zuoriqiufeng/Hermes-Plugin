"""reasoning-bridge 插件级测试（设计文档 §九.1）。

不依赖 hermes 运行时：用 importlib 从文件加载插件模块，伪造 ctx 与
hook payload，直接断言 JSONL 行格式、kind 过滤、切片/上限/清理行为。

运行：python3 -m pytest tests/ -v
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def load_plugin(tmp_path):
    """加载一份全新状态的插件模块，并把写入目录重定向到 tmp_path/data。"""
    spec = importlib.util.spec_from_file_location("reasoning_bridge_under_test", PLUGIN_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data_dir = tmp_path / "data"
    mod._data_dir = lambda: data_dir
    return mod, data_dir


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── 注册与行格式 ─────────────────────────────────────────────────────────


def test_register_registers_three_hooks(tmp_path):
    mod, _ = load_plugin(tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    assert set(ctx.hooks) == {"on_stream_start", "on_stream_delta", "on_stream_end"}
    assert ctx.hooks["on_stream_delta"] is mod.on_delta


def test_start_delta_end_line_format(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    mod.on_start(session_id="s1", turn_id="t1")
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="先查表结构，")
    mod.on_end(session_id="s1", turn_id="t1", finished=True)

    lines = read_lines(data_dir / "s1.jsonl")
    assert [ln["t"] for ln in lines] == ["start", "delta", "end"]
    assert lines[0]["turn"] == "t1" and isinstance(lines[0]["ts"], float)
    assert lines[1]["text"] == "先查表结构，" and lines[1]["turn"] == "t1"
    assert lines[2]["finished"] is True


def test_end_finished_defaults_false(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    mod.on_end(session_id="s1", turn_id="t1")  # 中断/出错路径可能缺省
    assert read_lines(data_dir / "s1.jsonl")[0]["finished"] is False


def test_multiple_turns_append_same_file(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    mod.on_start(session_id="s1", turn_id="t1")
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="第一轮")
    mod.on_end(session_id="s1", turn_id="t1", finished=True)
    mod.on_start(session_id="s1", turn_id="t2")
    mod.on_delta(session_id="s1", turn_id="t2", kind="reasoning", delta="第二轮")
    mod.on_end(session_id="s1", turn_id="t2", finished=False)

    lines = read_lines(data_dir / "s1.jsonl")
    assert [ln["turn"] for ln in lines] == ["t1", "t1", "t1", "t2", "t2", "t2"]


# ── kind 过滤与丢弃 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s1", "turn_id": "t1", "kind": "text", "delta": "正文"},  # 正文增量
        {"session_id": "s1", "turn_id": "t1", "delta": "无 kind 字段"},          # 缺 kind
        {"session_id": "s1", "turn_id": "t1", "kind": "reasoning", "delta": ""}, # 空增量
        {"session_id": "s1", "turn_id": "t1", "kind": "reasoning", "delta": None},
    ],
)
def test_non_reasoning_deltas_write_nothing(tmp_path, payload):
    mod, data_dir = load_plugin(tmp_path)
    mod.on_delta(**payload)
    assert not (data_dir / "s1.jsonl").exists()
    assert not data_dir.exists() or not any(data_dir.iterdir())


def test_empty_or_unsafe_session_id_dropped(tmp_path, caplog):
    mod, data_dir = load_plugin(tmp_path)
    for bad in ("", "a/../b", "x/y", "中文", ".."):
        mod.on_start(session_id=bad, turn_id="t1")
        mod.on_delta(session_id=bad, turn_id="t1", kind="reasoning", delta="x")
        mod.on_end(session_id=bad, turn_id="t1", finished=True)
    assert not data_dir.exists() or not any(data_dir.rglob("*.jsonl"))
    assert any("session_id" in msg for msg in caplog.messages)


# ── JSON 转义与切片 ──────────────────────────────────────────────────────


def test_control_chars_and_unicode_roundtrip(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    tricky = '引号" 反斜杠\\ 换行\n\t制表 emoji🤖 中文'
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta=tricky)
    assert read_lines(data_dir / "s1.jsonl")[0]["text"] == tricky


def test_oversized_text_splits_into_multiple_delta_lines(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    text = "汉" * 30000 + "a" * 100000  # 混合多字节与单字节，跨多个 64KB 块
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta=text)

    lines = read_lines(data_dir / "s1.jsonl")
    assert len(lines) > 1
    assert all(ln["t"] == "delta" for ln in lines)
    assert all(len(ln["text"].encode("utf-8")) <= mod.MAX_TEXT_BYTES for ln in lines)
    assert "".join(ln["text"] for ln in lines) == text


def test_small_text_single_line(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="短增量")
    assert len(read_lines(data_dir / "s1.jsonl")) == 1


# ── 5MB 上限与轮转 ───────────────────────────────────────────────────────


def test_file_size_cap_rotates_and_keeps_writing(tmp_path, caplog):
    caplog.set_level("INFO")
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    target = data_dir / "s1.jsonl"
    original = b"x" * (mod.MAX_FILE_BYTES - 50)
    target.write_bytes(original)  # 预填充到距上限不足一行的位置

    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="y" * 1000)

    # 旧内容被改名挪走，新文件承接写入且不止写一行
    rotated = list(data_dir.glob("s1.capped-*.jsonl"))
    assert len(rotated) == 1
    assert rotated[0].read_bytes() == original
    first = read_lines(target)
    assert first[0]["t"] == "delta" and first[0]["text"] == "y" * 1000
    assert any("轮转" in msg for msg in caplog.messages)

    # 轮转后持续可写（撞顶不再导致会话静默）
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="继续")
    assert read_lines(target)[-1]["text"] == "继续"
    assert target.stat().st_size < mod.MAX_FILE_BYTES


def test_prefilled_oversized_file_self_heals_on_first_write(tmp_path):
    """重启后首个 hook 对已满文件（历史 5MB 撞顶现场）轮转即恢复。"""
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    target = data_dir / "s1.jsonl"
    old_bytes = b"x" * (mod.MAX_FILE_BYTES + 100)
    target.write_bytes(old_bytes)

    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="恢复写入")

    assert [ln["text"] for ln in read_lines(target)] == ["恢复写入"]
    rotated = list(data_dir.glob("s1.capped-*.jsonl"))
    assert len(rotated) == 1 and rotated[0].read_bytes() == old_bytes


def test_rotation_failure_falls_back_to_warn_once_stop(tmp_path, monkeypatch, caplog):
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    target = data_dir / "s1.jsonl"
    target.write_bytes(b"x" * (mod.MAX_FILE_BYTES - 1))

    def boom(self, target_path):
        raise OSError("rename denied")

    monkeypatch.setattr(Path, "rename", boom)

    # 绝不抛出；warn 一次后停写（降级为撞顶旧行为）
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="y" * 100)
    first = [msg for msg in caplog.messages if "停止写入" in msg]
    assert len(first) >= 1
    assert target.stat().st_size == mod.MAX_FILE_BYTES - 1  # 未写入任何字节
    assert not list(data_dir.glob("s1.capped-*.jsonl"))

    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="y" * 100)
    assert [msg for msg in caplog.messages if "停止写入" in msg] == first  # 不重复 warn


def test_rotated_files_swept_after_24h(tmp_path):
    """轮转文件匹配 *.jsonl glob，24h 龄期照常清理；活跃会话文件保留。"""
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    stale_rotated = data_dir / "s1.capped-111.jsonl"
    fresh_rotated = data_dir / "s2.capped-222.jsonl"
    live = data_dir / "s3.jsonl"
    for f in (stale_rotated, fresh_rotated, live):
        f.write_text('{"t":"start"}\n', encoding="utf-8")
    old = time.time() - mod.FILE_MAX_AGE_SECONDS - 3600
    os.utime(stale_rotated, (old, old))

    mod.on_start(session_id="s3", turn_id="t1")  # 触发 housekeeping

    assert not stale_rotated.exists()
    assert fresh_rotated.exists()
    assert live.exists()


# ── housekeeping ─────────────────────────────────────────────────────────


def test_housekeeping_removes_stale_files_only(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    stale = data_dir / "old.jsonl"
    fresh = data_dir / "fresh.jsonl"
    stale.write_text('{"t":"start"}\n', encoding="utf-8")
    fresh.write_text('{"t":"start"}\n', encoding="utf-8")
    old = time.time() - mod.FILE_MAX_AGE_SECONDS - 3600
    os.utime(stale, (old, old))

    mod.on_start(session_id="fresh", turn_id="t1")  # 触发节流后的 housekeeping

    assert not stale.exists()
    assert fresh.exists()
    assert [ln["t"] for ln in read_lines(fresh)] == ["start", "start"]


def test_housekeeping_throttled_within_interval(tmp_path):
    mod, data_dir = load_plugin(tmp_path)
    data_dir.mkdir(parents=True)
    stale = data_dir / "old.jsonl"
    stale.write_text("x", encoding="utf-8")
    old = time.time() - mod.FILE_MAX_AGE_SECONDS - 3600
    os.utime(stale, (old, old))

    mod.on_start(session_id="s1", turn_id="t1")   # 第一次触发清理
    data_dir.mkdir(parents=True, exist_ok=True)
    stale2 = data_dir / "old2.jsonl"
    stale2.write_text("x", encoding="utf-8")
    os.utime(stale2, (old, old))
    mod.on_start(session_id="s2", turn_id="t2")   # 60s 内第二次——不清理

    assert not stale.exists()
    assert stale2.exists()


# ── 故障隔离 ─────────────────────────────────────────────────────────────


def test_write_failure_is_swallowed(tmp_path, monkeypatch, caplog):
    mod, data_dir = load_plugin(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(mod, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(Path, "mkdir", boom)

    # 绝不抛出，对话主流程不受影响
    mod.on_start(session_id="s1", turn_id="t1")
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="x")
    mod.on_end(session_id="s1", turn_id="t1", finished=True)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_hook_swallows_unexpected_payload_errors(tmp_path, monkeypatch):
    mod, data_dir = load_plugin(tmp_path)

    def boom(text, limit=None):
        raise RuntimeError("bug in chunker")

    monkeypatch.setattr(mod, "_chunk_text", boom)
    # 即使内部函数炸了，hook 也不允许把异常抛回 dispatcher
    mod.on_delta(session_id="s1", turn_id="t1", kind="reasoning", delta="x")
