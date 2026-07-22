"""令牌桶限流器（模块 A 产出）。

用于约束嵌入调用速率（embedding_rate_limit_per_min）。算法：
- 桶容量 = rate_per_min（最多累积这么多令牌）
- 填充速率 = rate_per_min / 60 个/秒（按时间流逝补充令牌，不超容量上限）
- acquire() 取一个令牌；不足时按缺令牌数 sleep，允许被 cancel
- try_acquire() 同步非阻塞，有则取走返回 True，否则返回 False
- set_rate() 实时变更速率并调整容量

不 import astrbot，保证可离线单测。线程安全假设：本插件运行在单 asyncio 线程中，
set_rate / acquire / try_acquire 均在同一事件循环里调用，无需加锁；若未来跨线程
使用，需自行加 threading.Lock。
"""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """异步令牌桶限流器。"""

    def __init__(self, rate_per_min: int):
        # 容量与填充速率同步初始化
        self._rate_per_min: int = rate_per_min
        self._capacity: float = float(rate_per_min)
        # 填充速率：个/秒
        self._refill_rate: float = rate_per_min / 60.0
        # 当前令牌数（初始满桶）
        self._tokens: float = float(rate_per_min)
        # 上次令牌补充时间戳
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        """按时间流逝补充令牌（不超过容量上限）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self._capacity, self._tokens + elapsed * self._refill_rate
            )
            self._last_refill = now

    async def acquire(self) -> None:
        """异步取一个令牌；不足时按缺令牌数 sleep 到可用。

        sleep 期间允许被 cancel（不持有任何外部资源，安全）。
        """
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # 缺令牌数 / 填充速率 = 需等待秒数
            deficit = 1.0 - self._tokens
            wait = deficit / self._refill_rate if self._refill_rate > 0 else 1.0
            await asyncio.sleep(wait)

    def try_acquire(self) -> bool:
        """同步非阻塞：有令牌则取走返回 True，否则返回 False。"""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def set_rate(self, rate_per_min: int) -> None:
        """实时变更速率并调整容量。

        容量调整为新速率；当前令牌数取 min(旧令牌, 新容量) 避免超容。
        单线程异步假设下直接赋值即可（见模块 docstring）。
        """
        self._rate_per_min = rate_per_min
        self._capacity = float(rate_per_min)
        self._refill_rate = rate_per_min / 60.0
        self._tokens = min(self._tokens, self._capacity)
        self._last_refill = time.monotonic()
