"""LLM 调参速率限制器（v0.2.9）。

对所有 llm_autotune 调用施加冷却（cooldown_hours）+ 日上限（max_per_day）双重限制。
纯标准库（deque），不 import astrbot/numpy，可离线单元测试。
"""

from __future__ import annotations

from collections import deque


class TuneRateLimiter:
    """调参调用速率限制器。

    allow(now, cooldown_hours, max_per_day) -> (bool, reason)
    record(now) 记录一次成功调用；state()/restore() 持久化往返。
    cooldown_hours=0 / max_per_day=0 表示不限该维度。
    """

    DAY_SECONDS: float = 86400.0
    HOUR_SECONDS: float = 3600.0

    def __init__(self) -> None:
        self._history: deque[float] = deque()  # 24h 内调用时间戳
        self._last_call: float | None = None
        self._force_history: deque[float] = deque()  # 强制触发时间戳历史

    def allow(
        self, now: float, cooldown_hours: float, max_per_day: int
    ) -> tuple[bool, str]:
        """检查是否允许调用。True=允许，False=被限（reason='cooldown'/'daily_cap')。

        cooldown_hours=0 不限冷却；max_per_day=0 不限日数。
        先清 _history 中超过 24h 的旧记录再判断。
        _last_call 为 None（从未调用）时跳过冷却检查。
        """
        # 先清超过 24h 的旧记录
        cutoff = now - self.DAY_SECONDS
        while self._history and self._history[0] < cutoff:
            self._history.popleft()
        # 冷却检查
        if cooldown_hours > 0 and self._last_call is not None:
            if now - self._last_call < cooldown_hours * self.HOUR_SECONDS:
                return False, "cooldown"
        # 日上限检查
        if max_per_day > 0 and len(self._history) >= max_per_day:
            return False, "daily_cap"
        return True, ""

    def allow_force(self, now: float, cooldown_hours: float) -> bool:
        """检查是否允许强制触发（独立于普通 allow 的冷却）。

        cooldown_hours=0 不限冷却（不推荐，会导致抖动）。
        先清 _force_history 中超过 cooldown_hours*3600 的旧记录再判断。
        """
        if cooldown_hours <= 0:
            return True
        cutoff = now - cooldown_hours * self.HOUR_SECONDS
        while self._force_history and self._force_history[0] < cutoff:
            self._force_history.popleft()
        return len(self._force_history) == 0

    def record(self, now: float) -> None:
        """记录一次成功调用。"""
        self._history.append(now)
        self._last_call = now

    def record_force(self, now: float) -> None:
        """记录一次强制触发调用（独立于普通 record）。"""
        self._force_history.append(now)

    def state(self) -> dict:
        """导出可持久化状态。"""
        return {
            "history": list(self._history),
            "last_call": self._last_call,
            "force_history": list(self._force_history),
        }

    def restore(self, state: dict) -> None:
        """从持久化状态恢复（容错：非 dict/非法值静默忽略）。"""
        if not isinstance(state, dict):
            return
        history = state.get("history")
        if isinstance(history, list):
            cleaned: list[float] = []
            for t in history:
                if isinstance(t, (int, float)) and not isinstance(t, bool):
                    cleaned.append(float(t))
            self._history = deque(cleaned)
        last_call = state.get("last_call")
        if last_call is None:
            self._last_call = None
        elif isinstance(last_call, (int, float)) and not isinstance(last_call, bool):
            self._last_call = float(last_call)
        force_history = state.get("force_history")
        if isinstance(force_history, list):
            cleaned_force: list[float] = []
            for t in force_history:
                if isinstance(t, (int, float)) and not isinstance(t, bool):
                    cleaned_force.append(float(t))
            self._force_history = deque(cleaned_force)
