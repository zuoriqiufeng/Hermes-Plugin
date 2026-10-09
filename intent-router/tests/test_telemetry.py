"""决策日志与统计聚合测试：记录结构、预览截断、轮转、tail、summarize。"""
from __future__ import annotations

import json

from conftest import submodule


def _telemetry(tmp_path, **kwargs):
    telemetry = submodule("telemetry")
    options = {"path": tmp_path / "decisions.jsonl", "enabled": True}
    options.update(kwargs)
    return telemetry, telemetry.Telemetry(**options)


def test_record_shape_and_defaults(tmp_path):
    telemetry, sink = _telemetry(tmp_path)
    sink.record({"kind": "decision", "intent": "x"})
    rows = sink.tail(10)
    assert len(rows) == 1
    assert rows[0]["kind"] == "decision" and rows[0]["intent"] == "x"
    assert "ts" in rows[0]


def test_disabled_telemetry_writes_nothing(tmp_path):
    telemetry, sink = _telemetry(tmp_path, enabled=False)
    sink.record({"kind": "decision"})
    assert sink.tail(10) == []
    assert not (tmp_path / "decisions.jsonl").exists()


def test_rotation_caps_file_size(tmp_path):
    telemetry, sink = _telemetry(tmp_path, max_bytes=1024)
    for index in range(200):
        sink.record({"kind": "decision", "pad": "x" * 40, "i": index})
    path = tmp_path / "decisions.jsonl"
    rotated = path.with_suffix(path.suffix + ".1")
    assert rotated.exists(), "达到上限应轮转为 .1"
    assert path.stat().st_size <= 1024 + 256


def test_tail_tolerates_corrupt_lines_and_limit(tmp_path):
    _telemetry_mod, sink = _telemetry(tmp_path)
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"kind":"decision","i":1}\nnot json\n{"kind":"decision","i":2}\n',
                    encoding="utf-8")
    rows = sink.tail(10)
    assert [row["i"] for row in rows] == [1, 2]
    assert [row["i"] for row in sink.tail(1)] == [2]
    assert sink.tail(0) == []


def test_tail_missing_file_is_empty(tmp_path):
    telemetry, sink = _telemetry(tmp_path)
    assert sink.tail(10) == []


def test_summarize_aggregates_expected_fields():
    telemetry = submodule("telemetry")
    records = [
        {"kind": "decision", "tier": "regex", "intent": "a", "action": "hint",
         "elapsed_ms": 1.0, "injected_chars": 100, "gate_reason": "rule_hit"},
        {"kind": "decision", "tier": "algo", "intent": "b", "action": "act",
         "elapsed_ms": 3.0, "injected_chars": 200, "tier3_used": False},
        {"kind": "decision", "tier": "none", "intent": "", "action": "none",
         "elapsed_ms": 2.0, "gate_reason": "no_evidence", "skipped": "follow_up"},
        {"kind": "decision", "tier": "llm", "intent": "c", "action": "hint",
         "elapsed_ms": 9.0, "tier3_used": True},
        {"kind": "prune", "removed": 3, "chars_saved": 900},
        {"kind": "boot", "intents": 9},
    ]
    summary = telemetry.summarize(records)
    assert summary["decisions"] == 4 and summary["records"] == 6
    assert summary["tiers"]["regex"] == 1 and summary["tiers"]["llm"] == 1
    assert summary["classified_rate"] == 0.75          # 3/4 有意图
    assert summary["tier3_used"] == 1 and summary["tier3_rate"] == 0.25
    assert summary["acted"] == 1 and summary["act_rate"] == 0.25
    assert summary["latency_ms"]["p50"] == 3.0 and summary["latency_ms"]["max"] == 9.0
    assert summary["injected_chars"] == 300
    assert summary["injected_chars_est_tokens"] == 100
    assert summary["prune_events"] == 1 and summary["prune_tools_removed"] == 3
    assert summary["skipped"] == {"follow_up": 1}


def test_summarize_survives_empty_and_garbage():
    telemetry = submodule("telemetry")
    summary = telemetry.summarize([])
    assert summary["decisions"] == 0 and summary["classified_rate"] == 0.0
    summary2 = telemetry.summarize([{"kind": "decision", "elapsed_ms": None, "tier": None}])
    assert summary2["decisions"] == 1
    assert telemetry.percentile([], 0.95) == 0.0
    assert telemetry.percentile([5.0], 0.5) == 5.0


def test_decision_record_is_json_serializable(router, settings):
    """to_record 必须可直接落盘（含 tuple/dict 混排的字段）。"""
    decision = router.classify("-4073").decision
    record = decision.to_record(session_id="s", turn_id="t", platform="cli",
                                mode="shadow", settings=settings)
    blob = json.dumps(record, ensure_ascii=False)
    assert "diag_error_code" in blob
    assert record["tier"] == "regex" and record["action"] in ("none", "hint", "act")
    assert record["candidates"] == []       # 第一层命中即短路，没有候选打分
    assert len(record["preview"]) <= settings.telemetry_preview_chars + 1

    # 第二层才有候选（调参要看的就是这些分数）
    second = router.classify("数据同步不动了").decision.to_record(settings=settings, mode="shadow")
    assert second["tier"] == "algo"
    assert second["candidates"] and isinstance(second["candidates"][0], list)
    assert second["evidence"]


def test_preview_is_truncated_and_flattened(router, settings):
    settings.telemetry_preview_chars = 20
    decision = router.classify("增量同步卡住\n" + "很长的一段现场描述" * 20).decision
    record = decision.to_record(settings=settings, mode="shadow")
    assert "\n" not in record["preview"]
    assert len(record["preview"]) <= 21


def test_default_data_dir_follows_hermes_home(monkeypatch, tmp_path):
    telemetry = submodule("telemetry")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert telemetry.default_data_dir() == tmp_path / ".hermes" / "plugin-data" / "intent-router"
    # worker 以 profile 运行时必须上溯到基座，否则 gateway 与 worker 各写一份
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "dag-ops"))
    assert telemetry.default_data_dir() == tmp_path / ".hermes" / "plugin-data" / "intent-router"
