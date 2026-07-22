"""全局疲劳度统一管理（bot 级单例）。

value ∈ [0, limit]，随时间指数衰减：
  value(t) = value(t0) * exp(-λ * Δsec)，λ = fatigue_recovery_rate / 60

每次回复按类型消耗疲劳值，高疲劳时提升阈值（A_modifier）并抑制非强制唤醒。
时间可注入（now_fn/now 参数），方便离线测试。

v0.2 模块 T — agent-fatigue
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

# 消耗类型 -> 配置键后缀
_COST_KEYS = {
    "active": "fatigue_cost_active",
    "passive": "fatigue_cost_passive",
    "track": "fatigue_cost_track",
    "glance": "fatigue_cost_glance",
}


class FatigueManager:
    """bot 级全局疲劳单例。value∈[0,limit]，指数衰减 value *= exp(-λ*Δsec)，λ = recovery_rate/60。"""

    def __init__(
        self,
        config_getter: Callable[[], dict],
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        """初始化全局疲劳管理器。

        config_getter: 每次调用返回实时配置 dict（不缓存，保证配置热更新生效）。
        now_fn: 时间源，默认 time.time；测试时注入可控时间。
        初始 value=0.0，last_ts=now_fn()。
        """
        self._config_getter = config_getter
        self._now_fn = now_fn if now_fn is not None else time.time
        self._value = 0.0
        self._last_ts = self._now_fn()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _now(self, now: float | None) -> float:
        """解析时间参数：now 若为 None 则用注入的时间源 self._now_fn()。"""
        if now is None:
            return self._now_fn()
        return now

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def tick(self, now: float | None = None) -> float:
        """把 value 从 last_ts 衰减到 now 时刻。

        衰减公式：value *= exp(-λ * dt)，其中 λ = recovery_rate / 60。
        dt <= 0 时不衰减（同一时刻多次调用不重复扣减）。
        更新 last_ts = now，返回衰减后 value（clamp >= 0）。
        """
        resolved = self._now(now)
        dt = max(resolved - self._last_ts, 0.0)
        if dt > 0.0:
            cfg = self._config_getter()
            rate = float(cfg.get("fatigue_recovery_rate", 0.1))
            lam = rate / 60.0
            self._value *= math.exp(-lam * dt)
        self._last_ts = resolved
        # 浮点累积误差可能导致微小负值，clamp 到 0
        if self._value < 0.0:
            self._value = 0.0
        return self._value

    def consume(self, reply_type: str, now: float | None = None) -> None:
        """先 tick(now) 衰减，再按回复类型增加疲劳消耗，cap 到 limit。

        reply_type 映射到配置键：active/passive/track/glance，
        未知类型回退到 'fatigue_cost_passive'。
        limit 下限 1e-6 防止除零。
        """
        self.tick(now)
        cfg = self._config_getter()
        key = _COST_KEYS.get(reply_type, "fatigue_cost_passive")
        cost = float(cfg.get(key, 0.8))
        limit = max(float(cfg.get("fatigue_limit", 5.0)), 1e-6)
        self._value = min(self._value + cost, limit)

    def level(self, now: float | None = None) -> str:
        """tick(now) 后按 ratio = value / limit 返回疲劳级别。

        ratio >= 1.0  -> 'high'
        ratio >= 0.55 -> 'medium'
        ratio >= 0.2  -> 'low'
        否则           -> 'none'
        """
        self.tick(now)
        cfg = self._config_getter()
        limit = max(float(cfg.get("fatigue_limit", 5.0)), 1e-6)
        ratio = self._value / limit
        if ratio >= 1.0:
            return "high"
        elif ratio >= 0.55:
            return "medium"
        elif ratio >= 0.2:
            return "low"
        else:
            return "none"

    def threshold_modifier(self, now: float | None = None) -> float:
        """根据当前疲劳级别返回阈值修正因子 A_modifier。

        high   -> cfg.get('fatigue_high_modifier', 1.2)
        medium -> cfg.get('fatigue_medium_modifier', 1.1)
        否则   -> 1.0
        """
        lvl = self.level(now)
        cfg = self._config_getter()
        if lvl == "high":
            return float(cfg.get("fatigue_high_modifier", 1.2))
        elif lvl == "medium":
            return float(cfg.get("fatigue_medium_modifier", 1.1))
        else:
            return 1.0

    def should_suppress(self, is_forced: bool, now: float | None = None) -> bool:
        """判断是否因高疲劳而抑制非强制唤醒。

        条件：fatigue_suppress_enabled 为 True 且 level == 'high' 且 not is_forced。
        强制唤醒（@Bot / 强唤醒词 / core 兴趣）不受抑制。
        """
        cfg = self._config_getter()
        if not cfg.get("fatigue_suppress_enabled", True):
            return False
        if is_forced:
            return False
        return self.level(now) == "high"

    def snapshot(self, now: float | None = None) -> dict:
        """tick(now) 后返回当前疲劳快照，供 Dashboard 状态面板展示。

        返回 {'value': round(value,4), 'limit': limit, 'ratio': round(ratio,4), 'level': level}。
        """
        self.tick(now)
        cfg = self._config_getter()
        limit = max(float(cfg.get("fatigue_limit", 5.0)), 1e-6)
        ratio = self._value / limit
        if ratio >= 1.0:
            lvl = "high"
        elif ratio >= 0.55:
            lvl = "medium"
        elif ratio >= 0.2:
            lvl = "low"
        else:
            lvl = "none"
        return {
            "value": round(self._value, 4),
            "limit": limit,
            "ratio": round(ratio, 4),
            "level": lvl,
        }

    def state(self) -> tuple[float, float]:
        """返回 (value, last_ts) 供可选持久化（不触发 tick，原样返回）。"""
        return (self._value, self._last_ts)

    def restore(self, value: float, last_ts: float) -> None:
        """从持久化数据恢复疲劳状态。

        value clamp 到 [0, 当前 limit]；last_ts 非法（非正数/非有限值/类型错误）则用 now_fn()。
        异常输入不抛异常，静默回退到安全初值。
        """
        try:
            cfg = self._config_getter()
            limit = max(float(cfg.get("fatigue_limit", 5.0)), 1e-6)
            v = float(value)
            self._value = max(0.0, min(v, limit))
            # 校验 last_ts：必须为正有限数值
            if (
                isinstance(last_ts, (int, float))
                and float(last_ts) > 0.0
                and math.isfinite(float(last_ts))
            ):
                self._last_ts = float(last_ts)
            else:
                self._last_ts = self._now_fn()
        except (ValueError, TypeError):
            # 无法解析的输入，回退到安全默认值
            self._value = 0.0
            self._last_ts = self._now_fn()
