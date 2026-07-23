"""test_context.py —— C 双窗口上下文（F3）。

测试对象：core/context.py → GroupContext
覆盖点：
- add_message / short_window_text（格式、long_size 溢出丢弃最旧）
- add_bot_message（__bot__ 标识）
- push_batch / topic_embedding（均值、空、max_batches）
- select_long_relevant（top_n 余弦排序、within_sec 过滤、空 anchor/无批次）
- recent_speakers（排除 bot、去重、n 不足）
- last_message_ts（空/有消息）

对应 PRD F3（双窗口、嵌入相关性过滤）。
"""

from __future__ import annotations

import pytest

from core.tracking.context import GroupContext, _BOT_USER_ID
from core.common.models import BatchRecord, LogicalMessage


def _msg(uid: str, nick: str, text: str, ts: float, gid: str = "g1") -> LogicalMessage:
    return LogicalMessage(user_id=uid, nickname=nick, text=text, ts=ts, group_id=gid)


# ---------------------------------------------------------------------- #
# add_message / short_window_text
# ---------------------------------------------------------------------- #

def test_context_short_window_text_format():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.add_message(_msg("u1", "Alice", "hi", 1.0))
    ctx.add_message(_msg("u2", "Bob", "hello", 2.0))
    txt = ctx.short_window_text()
    assert "Alice: hi" in txt
    assert "Bob: hello" in txt
    assert "\n" in txt


def test_context_short_window_text_only_recent_n():
    """short_window_text 只返回最近 short_size 条。"""
    ctx = GroupContext(short_size=2, long_size=10)
    for i in range(5):
        ctx.add_message(_msg("u1", "N", f"m{i}", float(i)))
    txt = ctx.short_window_text()
    assert "m3" in txt and "m4" in txt
    assert "m0" not in txt and "m1" not in txt


def test_context_short_window_zero_size_returns_empty():
    ctx = GroupContext(short_size=0, long_size=10)
    ctx.add_message(_msg("u1", "Alice", "hi", 1.0))
    assert ctx.short_window_text() == ""


def test_context_add_message_long_size_overflow():
    """long_size 溢出丢弃最旧。"""
    ctx = GroupContext(short_size=3, long_size=3)
    for i in range(5):
        ctx.add_message(_msg("u1", "N", f"m{i}", float(i)))
    # 仅保留最近 3 条
    txt = ctx.short_window_text()
    assert "m2" in txt and "m3" in txt and "m4" in txt
    assert "m0" not in txt and "m1" not in txt


# ---------------------------------------------------------------------- #
# add_bot_message
# ---------------------------------------------------------------------- #

def test_context_add_bot_message_marked_bot():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.add_bot_message("我是机器人", 1.0)
    msgs = list(ctx._messages)
    assert msgs[0].user_id == _BOT_USER_ID
    assert msgs[0].nickname == "机器人"


def test_context_recent_speakers_excludes_bot():
    """recent_speakers 排除 bot 消息。"""
    ctx = GroupContext(short_size=5, long_size=10)
    ctx.add_message(_msg("u1", "Alice", "hi", 1.0))
    ctx.add_bot_message("reply", 2.0)
    ctx.add_message(_msg("u2", "Bob", "yo", 3.0))
    speakers = ctx.recent_speakers(5)
    uids = [s[0] for s in speakers]
    assert _BOT_USER_ID not in uids
    assert "u1" in uids and "u2" in uids


# ---------------------------------------------------------------------- #
# push_batch / topic_embedding
# ---------------------------------------------------------------------- #

def test_context_topic_embedding_mean():
    """topic_embedding 返回最近 max_batches 个批次嵌入均值。"""
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t1", embedding=[2.0, 0.0], ts=1.0))
    ctx.push_batch(BatchRecord(text="t2", embedding=[4.0, 0.0], ts=2.0))
    emb = ctx.topic_embedding(max_batches=6)
    assert emb is not None
    # 均值 = [3.0, 0.0]
    assert emb[0] == pytest.approx(3.0)
    assert emb[1] == pytest.approx(0.0)


def test_context_topic_embedding_max_batches_slice():
    """max_batches 限制取最近 N 个。"""
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t1", embedding=[10.0, 0.0], ts=1.0))
    ctx.push_batch(BatchRecord(text="t2", embedding=[0.0, 0.0], ts=2.0))
    ctx.push_batch(BatchRecord(text="t3", embedding=[0.0, 0.0], ts=3.0))
    # max_batches=2 → 取最近 2 个 [0,0],[0,0]，均值 [0,0]
    emb = ctx.topic_embedding(max_batches=2)
    assert emb == [pytest.approx(0.0), pytest.approx(0.0)]


def test_context_topic_embedding_empty_returns_none():
    ctx = GroupContext(short_size=3, long_size=10)
    assert ctx.topic_embedding() is None


def test_context_topic_embedding_max_batches_zero_returns_none():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t1", embedding=[1.0], ts=1.0))
    assert ctx.topic_embedding(max_batches=0) is None


def test_context_topic_embedding_skips_empty_embedding():
    """embedding 为空的批次被跳过。"""
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t1", embedding=[], ts=1.0))
    ctx.push_batch(BatchRecord(text="t2", embedding=[2.0], ts=2.0))
    emb = ctx.topic_embedding()
    assert emb == [pytest.approx(2.0)]


# ---------------------------------------------------------------------- #
# select_long_relevant
# ---------------------------------------------------------------------- #

def test_context_select_long_relevant_top_n_by_cosine():
    """按余弦相似度降序取 top_n 个批次文本。"""
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="相似", embedding=[1.0, 0.0], ts=1.0))
    ctx.push_batch(BatchRecord(text="无关", embedding=[0.0, 1.0], ts=2.0))
    ctx.push_batch(BatchRecord(text="最相似", embedding=[0.9, 0.4], ts=3.0))
    ctx.add_message(_msg("u1", "A", "x", 3.0))  # anchor_ts = 3.0
    # anchor=[1,0] → 与 [1,0] cosine=1，与 [0.9,0.4] cosine=0.9，与 [0,1] cosine=0
    result = ctx.select_long_relevant([1.0, 0.0], top_n=2, within_sec=300)
    assert len(result) == 2
    assert result[0] == "相似"  # 最相似
    assert result[1] == "最相似"


def test_context_select_long_relevant_empty_anchor():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t", embedding=[1.0], ts=1.0))
    assert ctx.select_long_relevant([], top_n=5) == []


def test_context_select_long_relevant_no_batches():
    ctx = GroupContext(short_size=3, long_size=10)
    assert ctx.select_long_relevant([1.0], top_n=5) == []


def test_context_select_long_relevant_top_n_zero():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="t", embedding=[1.0], ts=1.0))
    assert ctx.select_long_relevant([1.0], top_n=0) == []


def test_context_select_long_relevant_within_sec_filter():
    """within_sec 过滤掉过早的批次。"""
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="旧", embedding=[1.0], ts=1.0))
    ctx.push_batch(BatchRecord(text="新", embedding=[1.0], ts=100.0))
    ctx.add_message(_msg("u1", "A", "x", 100.0))  # anchor_ts=100
    # within_sec=50 → 旧(1.0 < 100-50=50) 被过滤
    result = ctx.select_long_relevant([1.0], top_n=5, within_sec=50.0)
    assert result == ["新"]


def test_context_select_long_relevant_skips_empty_embedding():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.push_batch(BatchRecord(text="empty", embedding=[], ts=1.0))
    ctx.push_batch(BatchRecord(text="valid", embedding=[1.0], ts=1.0))
    ctx.add_message(_msg("u1", "A", "x", 1.0))
    result = ctx.select_long_relevant([1.0], top_n=5)
    assert result == ["valid"]


# ---------------------------------------------------------------------- #
# recent_speakers
# ---------------------------------------------------------------------- #

def test_context_recent_speakers_dedup_by_user():
    """同一用户只保留最新一条。"""
    ctx = GroupContext(short_size=10, long_size=20)
    ctx.add_message(_msg("u1", "Alice", "old", 1.0))
    ctx.add_message(_msg("u1", "Alice", "new", 2.0))
    ctx.add_message(_msg("u2", "Bob", "hi", 3.0))
    speakers = ctx.recent_speakers(5)
    uids = [s[0] for s in speakers]
    assert uids.count("u1") == 1
    # u1 的 last_text 应为 "new"（最新）
    u1_entry = [s for s in speakers if s[0] == "u1"][0]
    assert u1_entry[2] == "new"


def test_context_recent_speakers_order_recent_first():
    """顺序从最近到较远。"""
    ctx = GroupContext(short_size=10, long_size=20)
    ctx.add_message(_msg("u1", "Alice", "a", 1.0))
    ctx.add_message(_msg("u2", "Bob", "b", 2.0))
    ctx.add_message(_msg("u3", "Carol", "c", 3.0))
    speakers = ctx.recent_speakers(3)
    assert [s[0] for s in speakers] == ["u3", "u2", "u1"]


def test_context_recent_speakers_n_zero_returns_empty():
    ctx = GroupContext(short_size=10, long_size=20)
    ctx.add_message(_msg("u1", "Alice", "a", 1.0))
    assert ctx.recent_speakers(0) == []


def test_context_recent_speakers_insufficient_returns_actual():
    ctx = GroupContext(short_size=10, long_size=20)
    ctx.add_message(_msg("u1", "Alice", "a", 1.0))
    speakers = ctx.recent_speakers(5)
    assert len(speakers) == 1


# ---------------------------------------------------------------------- #
# last_message_ts
# ---------------------------------------------------------------------- #

def test_context_last_message_ts_empty_returns_zero():
    ctx = GroupContext(short_size=3, long_size=10)
    assert ctx.last_message_ts() == 0.0


def test_context_last_message_ts_returns_last():
    ctx = GroupContext(short_size=3, long_size=10)
    ctx.add_message(_msg("u1", "A", "a", 1.0))
    ctx.add_message(_msg("u2", "B", "b", 5.0))
    assert ctx.last_message_ts() == 5.0
