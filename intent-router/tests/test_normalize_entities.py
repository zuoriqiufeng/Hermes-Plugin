"""归一化与实体抽取的测试（第 0 步：两层共用的地基）。

这两块最容易被忽略，也最容易出「静默错」：全角没归一 → 词典匹配不上；中文旁边
用 ``\\b`` → 错误码永远匹配不到；裸数字不设上下文 → 行号/年份被当成错误码。
"""
from __future__ import annotations

from conftest import submodule


def test_normalize_basic():
    normalize = submodule("normalize").normalize
    text = normalize("  ORA-01555  快照过期  ")
    assert text.text == "ora-01555 快照过期"
    assert text.compact == "ora01555快照过期"
    assert not text.truncated


def test_normalize_fullwidth_and_unicode():
    """全角/兼容字符走 NFKC；破折号统一为 '-'；零宽字符剔除。"""
    normalize = submodule("normalize").normalize

    assert normalize("ＯＲＡ－０１５５５").text == "ora-01555"
    assert normalize("ora\u201401555").text == "ora-01555"   # en dash
    assert normalize("ora\u221201555").text == "ora-01555"   # minus sign
    assert normalize("ora\u200b01555").text == "ora01555"    # zero-width space
    assert normalize("\ufeffORA-01555").text == "ora-01555"


def test_normalize_truncation_and_empty():
    normalize = submodule("normalize").normalize

    long_text = normalize("x" * 5000, max_chars=100)
    assert long_text.truncated and len(long_text.text) == 100
    assert normalize("").empty
    assert normalize(None).empty
    assert normalize("   \n\t ").empty


def test_normalize_digest_stable_across_whitespace():
    normalize = submodule("normalize").normalize

    assert normalize("数据同步 不动了").digest == normalize("  数据同步   不动了 ").digest


# ── 实体 ────────────────────────────────────────────────────────────────


def test_error_code_families():
    entities = submodule("entities")

    assert entities.extract("ora01555 报错").get("error_code") == "ORA-01555"
    assert entities.extract("ORA-01555 快照过期").get("error_code") == "ORA-01555"
    assert entities.extract("yas-02276").get("error_code") == "YAS-02276"
    assert entities.extract("pg1234").get("error_code") == "PG-1234"
    assert entities.extract("db21024n").get("error_code") == "DB21024N"
    assert entities.extract("ora-01555").get("db_type") == "oracle"


def test_error_code_chinese_adjacency():
    """中文紧邻实体：不能用 \\b（中文是词字符，\\b 会失效）。"""
    entities = submodule("entities")

    assert entities.extract("报错ora-01555怎么办").get("error_code") == "ORA-01555"
    assert entities.extract("连不上oracle").get("db_type") == "oracle"
    assert entities.extract("日志解析异常").get("db_type") is None


def test_bare_code_needs_whole_message_or_context():
    entities = submodule("entities")

    # 整句就是它 → 认（i2Stream 内部错误码是负数）
    assert entities.extract("4073", "4073").get("error_code") == "-4073"
    assert entities.extract("-4073", "-4073").get("error_code") == "-4073"
    # 贴着错误码关键词 → 认
    assert entities.extract("错误码 4073 什么意思", "错误码4073什么意思").get("error_code") == "-4073"
    # 无符号、无上下文 → 不认（行号 / 数量）
    assert entities.extract("第 100 行日志", "第100行日志").get("error_code") is None
    assert entities.extract("这个表有 3 个字段", "这个表有3个字段").get("error_code") is None
    # 年份形状 → 永远不认
    assert entities.extract("2024", "2024").get("error_code") is None
    assert entities.extract("1999 年的数据", "1999年的数据").get("error_code") is None
    # 带符号的候选优先于无符号候选
    assert entities.extract("第 100 行 报错 -4073", "第100行报错-4073").get("error_code") == "-4073"


def test_ip_and_rule_id_and_time():
    entities = submodule("entities")

    ip = entities.extract("192.168.1.10:1521 连不上")
    assert ip.get("ip") == "192.168.1.10" and ip.get("port") == "1521"
    assert entities.extract("999.1.1.1 不是 IP").get("ip") is None       # 八位组越界
    assert entities.extract("lp-0007 规则不生效").get("rule_id") == "LP-0007"
    assert entities.extract("lp0007").get("rule_id") == "LP-0007"
    assert entities.extract("最近30分钟延迟大").get("time_window") == "30m"
    assert entities.extract("最近1小时").get("time_window") == "1h"
    assert entities.extract("昨天同步的数据").get("time_window") == "yesterday"


def test_normalize_error_code_helper():
    entities = submodule("entities")

    assert entities.normalize_error_code("4073") == "-4073"
    assert entities.normalize_error_code("ora01555") == "ORA-01555"
    assert entities.normalize_error_code("ORA_01555") == "ORA-01555"
    assert entities.normalize_error_code("2024") == "2024"      # 年份形状原样返回
