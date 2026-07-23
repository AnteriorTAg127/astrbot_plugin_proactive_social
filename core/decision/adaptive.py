"""自适应阈值控制器 + 每群发送频率硬上限（v0.2.8）。

两个独立组件：
- AdaptiveThreshold : 每群自适应阈值倍率控制器，按近期触发率自动收敛
- SendQuota         : 每群滑动窗口发送配额，调参失误的最终兜底

本文件不 import astrbot、不 import numpy，仅依赖标准库，可离线单元测试。
"""

from __future__ import annotations

from collections import deque


class AdaptiveThreshold:
    """每群自适应阈值倍率控制器。

    滚动窗口记录最近 WINDOW 次批次决策的 (final_score, triggered)。
    每累计 EVAL_EVERY 个新样本计算窗口触发率：
    - rate > HI_RATE → mult *= STEP_UP（收紧）
    - rate < LO_RATE → mult *= STEP_DOWN（放宽）
    - 区间内不动
    mult 钳制在 [MULT_MIN, MULT_MAX]。

    效果：无论 embedding 模型余弦尺度如何，控制器把实际触发率
    收敛到 LO_RATE–HI_RATE 的自然区间——真正的「范围自适应」。
    """

    WINDOW: int = 100
    EVAL_EVERY: int = 20
    HI_RATE: float = 0.30
    LO_RATE: float = 0.05
    MULT_MIN: float = 0.5
    MULT_MAX: float = 2.0
    STEP_UP: float = 1.1
    STEP_DOWN: float = 0.9

    def __init__(self) -> None:
        self._scores: deque[float] = deque(maxlen=self.WINDOW)
        self._triggered: deque[bool] = deque(maxlen=self.WINDOW)
        self._since_eval: int = 0
        self._mult: float = 1.0

    def record(self, score: float, triggered: bool) -> bool:
        """记录一次批次决策结果，满 EVAL_EVERY 自动步进。

        返回值：True 当且仅当本次调用触发了 _evaluate()（即 _since_eval 归零）；
        其余情况返回 False。调用方可据此联动 LLM 自动调参等周期性副作用。
        """
        self._scores.append(score)
        self._triggered.append(triggered)
        self._since_eval += 1
        evaluated = False
        if self._since_eval >= self.EVAL_EVERY:
            self._evaluate()
            evaluated = True
        return evaluated

    def window_rate(self) -> float:
        """返回当前窗口触发率（triggered 占比）；空窗口返回 0.0。"""
        if not self._triggered:
            return 0.0
        return sum(1 for t in self._triggered if t) / len(self._triggered)

    def window_size(self) -> int:
        """返回当前窗口样本数。"""
        return len(self._triggered)

    def _evaluate(self) -> None:
        """评估最近 EVAL_EVERY 条的触发率并步进 mult。"""
        recent = list(self._triggered)[-self.EVAL_EVERY :]
        rate = sum(1 for t in recent if t) / len(recent)
        if rate > self.HI_RATE:
            self._mult = min(self.MULT_MAX, self._mult * self.STEP_UP)
        elif rate < self.LO_RATE:
            self._mult = max(self.MULT_MIN, self._mult * self.STEP_DOWN)
        self._since_eval = 0

    def multiplier(self) -> float:
        """返回当前阈值倍率。"""
        return self._mult

    def state(self) -> dict:
        """导出可持久化状态。"""
        return {"mult": self._mult, "since_eval": self._since_eval}

    def restore(self, state: dict) -> None:
        """从持久化状态恢复（容错）。"""
        if not isinstance(state, dict):
            return
        mult = state.get("mult")
        if isinstance(mult, (int, float)) and self.MULT_MIN <= mult <= self.MULT_MAX:
            self._mult = float(mult)
        since = state.get("since_eval")
        if isinstance(since, int) and 0 <= since < self.EVAL_EVERY:
            self._since_eval = since


class SendQuota:
    """每群滑动窗口发送配额。

    check(now, per_hour, per_day) 判断是否超限（0=不限）；
    record(now) 在发送成功后记录时间戳。
    """

    def __init__(self) -> None:
        self._ts: deque[float] = deque()

    def check(self, now: float, per_hour: int, per_day: int) -> bool:
        """检查是否还有配额。True=可发送，False=超限。

        per_hour/per_day 为 0 时表示不限制该维度。
        """
        # 清除超过 24 小时的旧记录
        cutoff_day = now - 86400.0
        while self._ts and self._ts[0] < cutoff_day:
            self._ts.popleft()

        if per_hour > 0:
            cutoff_hour = now - 3600.0
            count_hour = sum(1 for t in self._ts if t >= cutoff_hour)
            if count_hour >= per_hour:
                return False

        if per_day > 0:
            if len(self._ts) >= per_day:
                return False

        return True

    def record(self, now: float) -> None:
        """记录一次成功发送的时间戳。"""
        self._ts.append(now)
