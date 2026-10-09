"""config.py — 插件设置读取（全部键来自 plugin.yaml 的 config_schema）。

只读插件自有 settings（``plugins.entries.intent-router.settings.*``）。任何读取
失败/类型不符一律回落到默认值并 warn 一次：插件绝不能因为配置问题拒绝加载。

阈值三件套（min_score / margin / min_evidence）不是魔法常数——先用 shadow 模式
在真实流量上采样，再用 ``/intent tune`` 在标注语料上扫参，按「覆盖率 vs 精度」
曲线定稿，见 docs/thresholds-tuning.md。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

log = logging.getLogger("intent-router")

VALID_MODES = ("shadow", "hint", "act")
VALID_TIER3_POLICIES = ("off", "on_ambiguous", "always")
VALID_LOG_LEVELS = ("debug", "info", "warning", "error")

# 面（surface）分组：把入口名收敛成三组，便于 mode_overrides / surfaces 书写。
# 各入口的实际 platform 取值见 Hermes 源码（cli / tui / api_server / 各 IM 平台名）。
SURFACE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cli": ("cli", "tui"),
    "gateway": (
        "telegram", "discord", "slack", "whatsapp", "signal", "imessage",
        "matrix", "email", "sms", "teams", "gateway",
    ),
    "api_server": ("api_server", "api", "openai", "openai_compatible"),
}

_warned: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message, *args)


def surface_group(platform: str) -> str:
    """把入口名归到 cli / gateway / api_server 三组之一。

    CLI 会话的 platform 常为空串，按 cli 计；未知平台原样返回（既不是分组名也
    不是已知入口时，surfaces 白名单只能靠原始名匹配）。
    """
    name = (platform or "").strip().lower()
    if not name:
        return "cli"
    for group, names in SURFACE_ALIASES.items():
        if name in names:
            return group
    return name


def _coerce(value: Any, kind: str, default: Any, key: str) -> Any:
    """按 config_schema 声明的类型做宽松转换；失败即回落到默认值。"""
    if value is None:
        return default
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if kind == "int":
            if isinstance(value, bool):
                return default
            return int(value)
        if kind == "float":
            if isinstance(value, bool):
                return default
            return float(value)
        if kind == "str":
            return value if isinstance(value, str) else str(value)
        if kind == "list":
            if isinstance(value, (list, tuple)):
                return [str(item) for item in value]
            if isinstance(value, str):
                return [part.strip() for part in value.split(",") if part.strip()]
            return default
        if kind == "dict":
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, str):
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, Mapping) else default
            return default
    except Exception as exc:
        _warn_once("coerce:%s" % key, "intent-router: 配置 %s=%r 无法转换为 %s（%s），回落默认 %r",
                   key, value, kind, exc, default)
        return default
    return default


@dataclass
class Settings:
    """插件运行设置（默认值与 plugin.yaml 的 config_schema 一一对应）。"""

    enabled: bool = True
    mode: str = "shadow"
    mode_overrides: Dict[str, str] = field(default_factory=dict)
    surfaces: List[str] = field(default_factory=list)
    taxonomy: str = ""

    min_score: float = 0.62
    margin: float = 0.15
    min_evidence: int = 2
    weight_keyword: float = 0.6
    weight_bm25: float = 0.4
    kw_saturation: float = 2.0
    bm25_evidence_min: float = 0.35
    veto_penalty: float = 0.5

    tier3_enabled: bool = False
    tier3_policy: str = "on_ambiguous"
    tier3_task: str = "intent_router_classify"
    tier3_timeout: int = 8
    tier3_allow_act: bool = False
    tier3_cache_size: int = 256

    auto_enabled: bool = False
    auto_allow_tools: List[str] = field(default_factory=list)
    auto_timeout_ms: int = 1500
    auto_result_chars: int = 4000

    prune_enabled: bool = False
    prune_keep_tools: List[str] = field(default_factory=list)

    continuation_guard: bool = True
    max_input_chars: int = 2000
    expose_tool: bool = False

    telemetry_enabled: bool = True
    telemetry_preview_chars: int = 120
    telemetry_max_bytes: int = 8388608
    emit_events: bool = True

    # 插件自有可读日志（logbook.py）；与上面的 JSONL 遥测是双轨关系
    log_enabled: bool = True
    log_dir: str = ""
    log_level: str = "info"
    log_max_bytes: int = 5242880
    log_backups_per_day: int = 3
    log_retention_days: int = 7
    log_decisions: bool = True
    log_preview_chars: int = 120
    log_redact: bool = True
    log_include_pid: bool = False

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_ctx(cls, ctx: Any) -> "Settings":
        """从 Hermes 的 PluginContext 读取；ctx 不可用时返回全默认值。"""
        getter = getattr(ctx, "get_config", None)
        if not callable(getter):
            return cls()
        settings = cls()
        for key, kind in _SCHEMA_TYPES.items():
            default = getattr(settings, key)
            try:
                raw = getter(key, default)
            except Exception as exc:
                _warn_once("get:%s" % key, "intent-router: 读取配置 %s 失败：%s", key, exc)
                continue
            setattr(settings, key, _coerce(raw, kind, default, key))
        settings.normalize()
        return settings

    def normalize(self) -> None:
        """把越界取值收敛到合法域（只改自身，绝不抛错）。"""
        if self.mode not in VALID_MODES:
            _warn_once("mode", "intent-router: mode=%r 非法，按 shadow 处理", self.mode)
            self.mode = "shadow"
        if self.tier3_policy not in VALID_TIER3_POLICIES:
            _warn_once("tier3_policy", "intent-router: tier3_policy=%r 非法，按 on_ambiguous 处理",
                       self.tier3_policy)
            self.tier3_policy = "on_ambiguous"
        self.mode_overrides = {
            str(k): str(v) for k, v in (self.mode_overrides or {}).items()
            if str(v) in VALID_MODES
        }
        self.min_score = min(max(self.min_score, 0.0), 1.0)
        self.margin = min(max(self.margin, 0.0), 1.0)
        self.min_evidence = max(0, min(int(self.min_evidence), 3))
        self.max_input_chars = max(32, int(self.max_input_chars))
        self.auto_timeout_ms = max(1, int(self.auto_timeout_ms))
        self.auto_result_chars = max(64, int(self.auto_result_chars))
        self.telemetry_preview_chars = max(0, int(self.telemetry_preview_chars))
        self.telemetry_max_bytes = max(64 * 1024, int(self.telemetry_max_bytes))
        self.tier3_cache_size = max(0, int(self.tier3_cache_size))
        if self.log_level not in VALID_LOG_LEVELS:
            _warn_once("log_level", "intent-router: log_level=%r 非法，按 info 处理", self.log_level)
            self.log_level = "info"
        self.log_max_bytes = max(64 * 1024, int(self.log_max_bytes))
        self.log_backups_per_day = max(0, min(int(self.log_backups_per_day), 50))
        self.log_retention_days = max(0, min(int(self.log_retention_days), 365))
        self.log_preview_chars = max(0, int(self.log_preview_chars))

    # ── 按面取档 ────────────────────────────────────────────────────────
    def enabled_for(self, platform: str) -> bool:
        if not self.enabled:
            return False
        allow = [str(item).strip().lower() for item in self.surfaces if str(item).strip()]
        if not allow:
            return True
        group = surface_group(platform)
        raw = (platform or "").strip().lower()
        return group in allow or raw in allow

    def mode_for(self, platform: str) -> str:
        """面级档位：原始 platform 键优先于分组键；缺省回落全局 mode。"""
        raw = (platform or "").strip().lower()
        group = surface_group(platform)
        for key in (raw, group):
            mode = self.mode_overrides.get(key)
            if mode in VALID_MODES:
                return mode
        return self.mode


_SCHEMA_TYPES: Dict[str, str] = {
    "enabled": "bool",
    "mode": "str",
    "mode_overrides": "dict",
    "surfaces": "list",
    "taxonomy": "str",
    "min_score": "float",
    "margin": "float",
    "min_evidence": "int",
    "weight_keyword": "float",
    "weight_bm25": "float",
    "kw_saturation": "float",
    "bm25_evidence_min": "float",
    "veto_penalty": "float",
    "tier3_enabled": "bool",
    "tier3_policy": "str",
    "tier3_task": "str",
    "tier3_timeout": "int",
    "tier3_allow_act": "bool",
    "tier3_cache_size": "int",
    "auto_enabled": "bool",
    "auto_allow_tools": "list",
    "auto_timeout_ms": "int",
    "auto_result_chars": "int",
    "prune_enabled": "bool",
    "prune_keep_tools": "list",
    "continuation_guard": "bool",
    "max_input_chars": "int",
    "expose_tool": "bool",
    "telemetry_enabled": "bool",
    "telemetry_preview_chars": "int",
    "telemetry_max_bytes": "int",
    "emit_events": "bool",
    "log_enabled": "bool",
    "log_dir": "str",
    "log_level": "str",
    "log_max_bytes": "int",
    "log_backups_per_day": "int",
    "log_retention_days": "int",
    "log_decisions": "bool",
    "log_preview_chars": "int",
    "log_redact": "bool",
    "log_include_pid": "bool",
}
