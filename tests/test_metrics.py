"""test_metrics.py —— E 决策日志环 + 每日指标跨日重置。

测试对象：core/metrics.py → DecisionLog / MetricsStore
覆盖点：
- DecisionLog：add / recent（新→旧）/ to_list（旧→新）/ load 往返 / maxlen 环形 / 非法条目跳过
- MetricsStore：incr 基本计数 / 非法 key 忽略 / 跨日重置 / snapshot 副本 / load 从 KV / 非法存储忽略

对应 PRD F7（决策日志 + 每日指标）、§8.8（DRY_RUN 决策日志完整）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from core.metrics import DecisionLog, MetricsStore, _deserialize_decision
from core.models import BatchDecision, ScoreFactors


def _decision(ts: float, group_id: str = "g1", score: float = 0.5) -> BatchDecision:
    return BatchDecision(
        ts=ts,
        group_id=group_id,
        batch_summary=f"batch_{ts}",
        factors=ScoreFactors(1.0, 0.5, 0.0, 0.2, 0.1),
        score=score,
        threshold=0.65,
        hit_level="core",
        triggered=True,
        suppressed_reason="",
        dry_run=False,
        message_count=3,
    )


# ---------------------------------------------------------------------- #
# DecisionLog
# ---------------------------------------------------------------------- #

def test_metrics_decision_log_add_and_recent():
    log = DecisionLog(maxlen=10)
    log.add(_decision(1.0))
    log.add(_decision(2.0))
    recent = log.recent(5)
    assert len(recent) == 2
    # 顺序新→旧
    assert recent[0]["ts"] == 2.0
    assert recent[1]["ts"] == 1.0


def test_metrics_decision_log_recent_zero_or_negative():
    log = DecisionLog()
    log.add(_decision(1.0))
    assert log.recent(0) == []
    assert log.recent(-1) == []


def test_metrics_decision_log_recent_more_than_available():
    """n > 当前条数 → 返回全部。"""
    log = DecisionLog()
    log.add(_decision(1.0))
    recent = log.recent(10)
    assert len(recent) == 1


def test_metrics_decision_log_maxlen_ring():
    """超出 maxlen 自动淘汰最旧。"""
    log = DecisionLog(maxlen=3)
    for i in range(5):
        log.add(_decision(float(i)))
    assert len(log) == 3
    recent = log.recent(5)
    # 保留最近 3 条：ts=2,3,4（新→旧：4,3,2）
    assert [r["ts"] for r in recent] == [4.0, 3.0, 2.0]


def test_metrics_decision_log_to_list_old_to_new():
    """to_list 顺序旧→新（便于持久化与 load 往返）。"""
    log = DecisionLog()
    log.add(_decision(1.0))
    log.add(_decision(2.0))
    lst = log.to_list()
    assert [d["ts"] for d in lst] == [1.0, 2.0]


def test_metrics_decision_log_to_list_persist_limit():
    """to_list 限制最近 200 条。"""
    log = DecisionLog(maxlen=500)
    for i in range(250):
        log.add(_decision(float(i)))
    lst = log.to_list()
    assert len(lst) == 200
    # 保留最近 200 条（ts=50..249）
    assert lst[0]["ts"] == 50.0
    assert lst[-1]["ts"] == 249.0


def test_metrics_decision_log_load_roundtrip():
    """to_list → load 往返一致。"""
    log = DecisionLog()
    for i in range(5):
        log.add(_decision(float(i), group_id=f"g{i}"))
    serialized = log.to_list()
    log2 = DecisionLog()
    log2.load(serialized)
    assert len(log2) == 5
    recent = log2.recent(5)
    assert recent[0]["group_id"] == "g4"


def test_metrics_decision_log_load_empty():
    log = DecisionLog()
    log.load([])
    assert len(log) == 0


def test_metrics_decision_log_load_skips_invalid():
    """非法条目（缺字段/类型错）跳过不抛异常。"""
    log = DecisionLog()
    log.load([
        {"ts": 1.0, "group_id": "g1", "batch_summary": "s",
         "factors": {"s_int": 1, "s_topic": 0, "s_resp": 0, "c_cooldown": 0, "p_silence": 0},
         "score": 0.5, "threshold": 0.65, "hit_level": "core",
         "triggered": True, "suppressed_reason": "", "dry_run": False, "message_count": 1},
        "not a dict",  # 非法
        {"ts": "bad"},  # 缺字段
    ])
    assert len(log) == 1


def test_metrics_decision_log_recent_returns_asdict_with_factors():
    """recent 返回 dict 含嵌套 factors dict。"""
    log = DecisionLog()
    log.add(_decision(1.0))
    recent = log.recent(1)
    assert isinstance(recent[0]["factors"], dict)
    assert "s_int" in recent[0]["factors"]


def test_metrics_deserialize_decision_none_interest():
    """_deserialize_decision 对非 dict 返回 None。"""
    assert _deserialize_decision("not dict") is None
    assert _deserialize_decision(None) is None


def test_metrics_deserialize_decision_missing_factors():
    """缺 factors 字段时填零。"""
    d = _deserialize_decision({"ts": 1.0, "group_id": "g1"})
    assert d is not None
    assert d.factors.s_int == 0.0


# ---------------------------------------------------------------------- #
# MetricsStore
# ---------------------------------------------------------------------- #

def test_metrics_incr_basic(mock_kv):
    store = MetricsStore()
    asyncio.run(store.incr("llm_calls", mock_kv.set))
    asyncio.run(store.incr("llm_calls", mock_kv.set))
    asyncio.run(store.incr("embedding_calls", mock_kv.set))
    snap = store.snapshot()
    assert snap["llm_calls"] == 2
    assert snap["embedding_calls"] == 1
    # 已持久化到 KV
    assert mock_kv["metrics"]["llm_calls"] == 2


def test_metrics_incr_invalid_key_ignored(mock_kv):
    """非法 key 静默忽略。"""
    store = MetricsStore()
    asyncio.run(store.incr("not_a_key", mock_kv.set))
    snap = store.snapshot()
    assert all(snap[k] == 0 for k in ("llm_calls", "embedding_calls",
                                       "proactive_sends", "proactive_triggered"))


def test_metrics_incr_kv_failure_does_not_crash():
    """kv_set_fn 抛异常不影响内存计数。"""
    async def bad_set(k, v):
        raise RuntimeError("kv broken")
    store = MetricsStore()
    asyncio.run(store.incr("llm_calls", bad_set))
    assert store.snapshot()["llm_calls"] == 1


def test_metrics_incr_cross_day_reset(mock_kv):
    """跨日自动重置全部计数。"""
    store = MetricsStore()
    # 模拟昨日数据
    store._data["date"] = "2020-01-01"
    store._data["llm_calls"] = 99
    store._data["embedding_calls"] = 50
    # incr 触发跨日重置
    asyncio.run(store.incr("llm_calls", mock_kv.set))
    snap = store.snapshot()
    today = datetime.now().strftime("%Y-%m-%d")
    assert snap["date"] == today
    assert snap["llm_calls"] == 1  # 重置后 +1
    assert snap["embedding_calls"] == 0  # 重置


def test_metrics_snapshot_returns_copy(mock_kv):
    """snapshot 返回副本，修改不影响内部。"""
    store = MetricsStore()
    asyncio.run(store.incr("llm_calls", mock_kv.set))
    snap = store.snapshot()
    snap["llm_calls"] = 999
    assert store.snapshot()["llm_calls"] == 1


def test_metrics_load_from_kv(mock_kv):
    """从 KV 加载历史指标。"""
    mock_kv["metrics"] = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "llm_calls": 10,
        "embedding_calls": 20,
        "proactive_sends": 5,
        "proactive_triggered": 3,
    }
    store = MetricsStore()
    asyncio.run(store.load(mock_kv.get))
    snap = store.snapshot()
    assert snap["llm_calls"] == 10
    assert snap["proactive_triggered"] == 3


def test_metrics_load_invalid_stored_ignored(mock_kv):
    """KV 中非法 metrics（非 dict / 缺 date）→ 不加载，保持默认。"""
    mock_kv["metrics"] = "not a dict"
    store = MetricsStore()
    asyncio.run(store.load(mock_kv.get))
    snap = store.snapshot()
    assert snap["llm_calls"] == 0


def test_metrics_load_missing_date_ignored(mock_kv):
    mock_kv["metrics"] = {"llm_calls": 10}  # 缺 date
    store = MetricsStore()
    asyncio.run(store.load(mock_kv.get))
    assert store.snapshot()["llm_calls"] == 0


def test_metrics_load_kv_failure_no_crash():
    """kv_get_fn 抛异常不抛出。"""
    async def bad_get(k, d=None):
        raise RuntimeError("kv broken")
    store = MetricsStore()
    asyncio.run(store.load(bad_get))
    assert store.snapshot()["llm_calls"] == 0


def test_metrics_snapshot_all_four_keys():
    """snapshot 含四个固定键 + date。"""
    store = MetricsStore()
    snap = store.snapshot()
    for k in ("llm_calls", "embedding_calls",
              "proactive_sends", "proactive_triggered", "date"):
        assert k in snap
