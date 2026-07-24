"""决策日志环 + 每日指标（模块 E 产出）。

两个独立组件：
- DecisionLog  : 内存环形缓冲（最近 500 条 BatchDecision）+ 持久化支持（最近 200 条）
- MetricsStore : 每日指标计数器（LLM/嵌入/主动发送/触发），跨日自动重置并持久化到 KV

本文件不 import astrbot，仅依赖标准库 + .models。KV 与日志能力通过注入回调使用，
保证可离线单元测试。
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ..common.models import BatchDecision, ScoreFactors


class DecisionLog:
    """决策日志环形缓冲。

    - add        : 追加一条 BatchDecision（超出 maxlen 自动淘汰最旧）
    - recent(n)  : 返回最近 n 条的 dict（新→旧，便于 Dashboard 展示）
    - to_list()  : 返回最近 200 条 dict（旧→新，便于持久化与回放重放）
    - load(data) : 从 list[dict] 还原（与 to_list 互逆，支持往返一致）
    """

    # 持久化时保留的最近条数（与 PRD §4 KV decision_log "最近 200 条" 对齐）
    _PERSIST_LIMIT: int = 200

    def __init__(self, maxlen: int = 500):
        self._deque: deque[BatchDecision] = deque(maxlen=maxlen)

    def add(self, d: BatchDecision) -> None:
        """追加一条决策记录。"""
        self._deque.append(d)

    def recent(self, n: int) -> list[dict]:
        """返回最近 n 条的 asdict 列表，顺序：新→旧。

        - n ≤ 0 → 返回空列表
        - n > 当前条数 → 返回全部（新→旧）
        """
        if n <= 0:
            return []
        items = list(self._deque)  # 旧→新
        recent_items = items[-n:]  # 取最新 n 条（仍旧→新）
        # 反转为 新→旧
        recent_items.reverse()
        return [dataclasses.asdict(d) for d in recent_items]

    def to_list(self) -> list[dict]:
        """返回最近 200 条 asdict，顺序：旧→新（便于持久化与 load 往返）。"""
        items = list(self._deque)
        tail = items[-self._PERSIST_LIMIT :] if self._PERSIST_LIMIT > 0 else items
        return [dataclasses.asdict(d) for d in tail]

    def load(self, data: list[dict]) -> None:
        """从 list[dict] 还原日志（清空当前缓冲后逐条重建）。

        容错：非法条目（缺字段/类型错）跳过不抛异常。factors 字段为嵌套 dict，
        需还原为 ScoreFactors。
        """
        self._deque.clear()
        if not data:
            return
        for raw in data:
            d = _deserialize_decision(raw)
            if d is not None:
                self._deque.append(d)

    def __len__(self) -> int:
        return len(self._deque)


def _deserialize_decision(raw: Any) -> BatchDecision | None:
    """从 dict 还原 BatchDecision；非法或缺字段返回 None（不抛异常）。

    factors 字段需还原为 ScoreFactors 嵌套结构。
    """
    if not isinstance(raw, dict):
        return None
    try:
        factors_raw = raw.get("factors")
        if isinstance(factors_raw, dict):
            factors = ScoreFactors(
                s_int=float(factors_raw.get("s_int", 0.0)),
                s_topic=float(factors_raw.get("s_topic", 0.0)),
                s_resp=float(factors_raw.get("s_resp", 0.0)),
                c_cooldown=float(factors_raw.get("c_cooldown", 0.0)),
                p_silence=float(factors_raw.get("p_silence", 0.0)),
            )
        else:
            factors = ScoreFactors(0.0, 0.0, 0.0, 0.0, 0.0)
        return BatchDecision(
            ts=float(raw.get("ts", 0.0)),
            group_id=str(raw.get("group_id", "")),
            batch_summary=str(raw.get("batch_summary", "")),
            factors=factors,
            score=float(raw.get("score", 0.0)),
            threshold=float(raw.get("threshold", 0.0)),
            hit_level=str(raw.get("hit_level", "none")),
            triggered=bool(raw.get("triggered", False)),
            suppressed_reason=str(raw.get("suppressed_reason", "")),
            dry_run=bool(raw.get("dry_run", False)),
            message_count=int(raw.get("message_count", 0)),
            # v0.2 双通道增量字段：缺失时用默认值（兼容 v0.1 持久化日志）
            score_a=float(raw.get("score_a", 0.0)),
            score_b=float(raw.get("score_b", 0.0)),
            alpha=float(raw.get("alpha", 0.0)),
            fatigue_level=str(raw.get("fatigue_level", "none")),
            fatigue_value=float(raw.get("fatigue_value", 0.0)),
            channel=str(raw.get("channel", "vector")),
            # v0.2.5 回复关键词增量字段
            keyword_match_score=float(raw.get("keyword_match_score", 0.0)),
            keyword_added_score=float(raw.get("keyword_added_score", 0.0)),
            # v0.2.6 Embedding 降级标记
            embedding_degraded=bool(raw.get("embedding_degraded", False)),
            # v0.2.8 自适应阈值倍率
            adaptive_mult=float(raw.get("adaptive_mult", 1.0)),
            # v0.3.5 对话状态模块阈值修正倍率
            conversation_state_mod=float(raw.get("conversation_state_mod", 1.0)),
        )
    except (TypeError, ValueError):
        return None


class MetricsStore:
    """每日指标计数器。

    四个固定键：llm_calls / embedding_calls / proactive_sends / proactive_triggered。
    - incr(key, kv_set_fn)  : 跨日自动重置全部计数；对应 key +1；写回 KV "metrics"
    - snapshot()            : 返回当前计数 dict 副本（不持久化）
    - load(kv_get_fn)       : 从 KV "metrics" 还原（缺失用默认零值）

    跨日判定：每次 incr 时比较 datetime.now().strftime("%Y-%m-%d") 与存储 date，
    不同则重置全部计数并更新 date。
    """

    _KEYS: tuple[str, ...] = (
        "llm_calls",
        "embedding_calls",
        "proactive_sends",
        "proactive_triggered",
    )

    def __init__(self):
        self._data: dict[str, Any] = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "llm_calls": 0,
            "embedding_calls": 0,
            "proactive_sends": 0,
            "proactive_triggered": 0,
        }

    async def incr(
        self, key: str, kv_set_fn: Callable[[str, Any], Awaitable[None]]
    ) -> None:
        """对应 key +1，跨日自动重置，写回 KV "metrics"。

        非法 key（不在 _KEYS 中）静默忽略不抛异常。
        """
        if key not in self._KEYS:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._data.get("date") != today:
            # 跨日重置全部计数
            self._data = {
                "date": today,
                "llm_calls": 0,
                "embedding_calls": 0,
                "proactive_sends": 0,
                "proactive_triggered": 0,
            }
        self._data[key] = int(self._data.get(key, 0)) + 1
        try:
            await kv_set_fn("metrics", self._data)
        except Exception:
            # KV 写失败不影响内存计数（下次 incr 会再次尝试写）
            pass

    def snapshot(self) -> dict:
        """返回当前计数 dict 副本。"""
        return {
            "date": self._data.get("date", ""),
            "llm_calls": int(self._data.get("llm_calls", 0)),
            "embedding_calls": int(self._data.get("embedding_calls", 0)),
            "proactive_sends": int(self._data.get("proactive_sends", 0)),
            "proactive_triggered": int(self._data.get("proactive_triggered", 0)),
        }

    async def load(self, kv_get_fn: Callable[[str, Any], Awaitable[Any]]) -> None:
        """从 KV "metrics" 还原；缺失或非法用默认零值。

        还原时若发现存储的 date 与今天不同，保持存储的 date 与计数不变
        （下一次 incr 会自然触发跨日重置），不在此处提前清零。
        """
        try:
            stored = await kv_get_fn("metrics", None)
        except Exception:
            stored = None
        if not isinstance(stored, dict):
            return
        date = stored.get("date")
        if not isinstance(date, str) or not date:
            return
        self._data = {
            "date": date,
            "llm_calls": int(stored.get("llm_calls", 0)),
            "embedding_calls": int(stored.get("embedding_calls", 0)),
            "proactive_sends": int(stored.get("proactive_sends", 0)),
            "proactive_triggered": int(stored.get("proactive_triggered", 0)),
        }
