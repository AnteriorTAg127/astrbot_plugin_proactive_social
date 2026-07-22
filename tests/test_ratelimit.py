"""test_ratelimit.py —— A 令牌桶限流器。

测试对象：core/ratelimit.py → TokenBucketRateLimiter
覆盖点：
- try_acquire：初始满桶、耗尽后返回 False、时间流逝后补充
- acquire：阻塞取令牌（asyncio.run 包装，验证耗时）
- set_rate：变更容量、令牌数 trim 到新容量

对应 PRD §8.7（成本/限流：30/min 约束下放行行为）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.ratelimit import TokenBucketRateLimiter


# ---------------------------------------------------------------------- #
# try_acquire
# ---------------------------------------------------------------------- #

def test_ratelimit_try_acquire_initial_full_bucket():
    """初始满桶，可连续取 rate_per_min 个令牌。对应 §8.7。"""
    rl = TokenBucketRateLimiter(30)
    for _ in range(30):
        assert rl.try_acquire() is True
    # 桶空
    assert rl.try_acquire() is False


def test_ratelimit_try_acquire_depletes_then_false():
    rl = TokenBucketRateLimiter(3)
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False


def test_ratelimit_try_acquire_refills_after_time(monkeypatch):
    """时间流逝后令牌补充。"""
    rl = TokenBucketRateLimiter(60)  # 1 个/秒
    # 耗尽
    for _ in range(60):
        rl.try_acquire()
    assert rl.try_acquire() is False
    # 模拟时间前进 5 秒 → 补 5 个令牌
    base = time.monotonic()
    monkeypatch.setattr(
        "core.ratelimit.time.monotonic", lambda: base + 5.0
    )
    assert rl.try_acquire() is True  # 补充后可取


def test_ratelimit_try_acquire_refill_capped_at_capacity(monkeypatch):
    """补充不超过容量上限。"""
    rl = TokenBucketRateLimiter(10)  # capacity=10
    # 耗尽
    for _ in range(10):
        rl.try_acquire()
    assert rl.try_acquire() is False
    # 时间前进 100 秒（远超容量所需）→ 最多补到 10
    base = time.monotonic()
    monkeypatch.setattr(
        "core.ratelimit.time.monotonic", lambda: base + 100.0
    )
    rl._refill()
    assert rl._tokens <= 10.0


# ---------------------------------------------------------------------- #
# acquire（异步阻塞）
# ---------------------------------------------------------------------- #

def test_ratelimit_acquire_returns_immediately_when_tokens_available():
    """有令牌时 acquire 立即返回。"""
    rl = TokenBucketRateLimiter(30)
    start = time.monotonic()
    asyncio.run(rl.acquire())
    elapsed = time.monotonic() - start
    assert elapsed < 0.5


def test_ratelimit_acquire_blocks_when_empty():
    """无令牌时 acquire 阻塞至令牌补充。"""
    rl = TokenBucketRateLimiter(600)  # 10 个/秒 → 补 1 个需 0.1 秒
    rl._tokens = 0.0
    rl._last_refill = time.monotonic()
    start = time.monotonic()
    asyncio.run(rl.acquire())
    elapsed = time.monotonic() - start
    # 应阻塞约 0.1 秒（1/10）
    assert elapsed >= 0.05
    assert elapsed < 0.5


def test_ratelimit_acquire_multiple_after_depletion():
    """耗尽后连续 acquire 两次，第二次阻塞更久。"""
    rl = TokenBucketRateLimiter(120)  # 2 个/秒 → 0.5 秒补 1 个
    rl._tokens = 0.0
    rl._last_refill = time.monotonic()
    start = time.monotonic()
    asyncio.run(rl.acquire())  # 阻塞 ~0.5 秒
    asyncio.run(rl.acquire())  # 再阻塞 ~0.5 秒
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5  # 两次合计至少 0.5 秒


# ---------------------------------------------------------------------- #
# set_rate
# ---------------------------------------------------------------------- #

def test_ratelimit_set_rate_changes_capacity_and_refill():
    rl = TokenBucketRateLimiter(30)
    assert rl._capacity == 30.0
    rl.set_rate(60)
    assert rl._capacity == 60.0
    assert rl._refill_rate == pytest.approx(1.0)  # 60/60


def test_ratelimit_set_rate_trims_tokens_to_new_capacity():
    """set_rate 降低容量时，令牌数 trim 到新容量。"""
    rl = TokenBucketRateLimiter(100)
    # 初始 tokens=100（满）
    assert rl._tokens == 100.0
    rl.set_rate(30)
    assert rl._tokens == 30.0  # trim 到新容量


def test_ratelimit_set_rate_increase_keeps_tokens():
    """set_rate 提升容量时，令牌数不变（不超旧容量）。"""
    rl = TokenBucketRateLimiter(30)
    rl._tokens = 10.0
    rl.set_rate(100)
    assert rl._tokens == 10.0  # 不变
    assert rl._capacity == 100.0


def test_ratelimit_set_rate_allows_more_acquires():
    """set_rate 提升容量后，时间流逝可补充到新容量。"""
    rl = TokenBucketRateLimiter(2)
    rl.try_acquire()
    rl.try_acquire()
    assert rl.try_acquire() is False  # 满 2 已耗尽
    rl.set_rate(120)  # 120/min = 2/sec，容量 120
    # 容量提升后随时间能补到更多
    base = time.monotonic()
    import core.ratelimit as rl_mod
    orig_monotonic = rl_mod.time.monotonic
    rl_mod.time.monotonic = lambda: base + 60.0  # 时间前进 60 秒
    try:
        rl._refill()
        # 2/sec * 60 sec = 120，cap 到 120
        assert rl._tokens == pytest.approx(120.0)
    finally:
        rl_mod.time.monotonic = orig_monotonic
