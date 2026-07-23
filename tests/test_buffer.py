"""test_buffer.py —— C 消息缓冲与动态批处理。

测试对象：core/buffer.py → GroupBuffer
覆盖点：
- append：同用户短消息拼接、结束标点阻止拼接、长消息不拼接、不同用户不拼接
- flush / pending_text / pending_count
- 溢出丢弃最旧 + warning（PRD §6.6 消息风暴）
- dynamic_interval：rate>=1→min, rate<=0.05→max, 负值→max, 中间线性
- contains_turn_keyword：命中/空列表

对应 PRD F2（短消息拼接、动态间隔、紧急转弯）。
"""

from __future__ import annotations

import pytest

from core.tracking.buffer import GroupBuffer, _is_short_text


# ---------------------------------------------------------------------- #
# _is_short_text
# ---------------------------------------------------------------------- #

def test_buffer_is_short_text_short_no_punct():
    assert _is_short_text("嗯") is True
    assert _is_short_text("12345") is True  # 正好 5 字


def test_buffer_is_short_text_too_long():
    assert _is_short_text("123456") is False  # 6 字


def test_buffer_is_short_text_with_ending_punct():
    """含结束标点 → 不是短消息（不拼接）。"""
    for punct in "。！？!?~":
        assert _is_short_text(f"好{punct}") is False


# ---------------------------------------------------------------------- #
# append 拼接逻辑
# ---------------------------------------------------------------------- #

def test_buffer_append_short_merge_same_user():
    """同用户连续短消息拼接为一条逻辑消息。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "嗯", 1.0, "g1")
    buf.append("u1", "Alice", "啊", 2.0, "g1")
    assert buf.pending_count() == 1
    msgs = buf.flush()
    assert len(msgs) == 1
    assert msgs[0].text == "嗯 啊"
    assert msgs[0].ts == 2.0  # ts 更新为最新


def test_buffer_append_no_merge_when_punct():
    """上一条含结束标点 → 不拼接。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "好。", 1.0, "g1")
    buf.append("u1", "Alice", "啊", 2.0, "g1")
    assert buf.pending_count() == 2


def test_buffer_append_no_merge_when_long():
    """上一条为长消息（>5字）→ 不拼接。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "今天天气真不错", 1.0, "g1")  # 7 字
    buf.append("u1", "Alice", "啊", 2.0, "g1")
    assert buf.pending_count() == 2


def test_buffer_append_no_merge_different_user():
    """不同用户 → 不拼接。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "嗯", 1.0, "g1")
    buf.append("u2", "Bob", "啊", 2.0, "g1")
    assert buf.pending_count() == 2


def test_buffer_append_no_merge_when_current_long():
    """当前条为长消息 → 不拼接（即使上一条短）。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "嗯", 1.0, "g1")
    buf.append("u1", "Alice", "今天天气真不错", 2.0, "g1")
    assert buf.pending_count() == 2


def test_buffer_append_updates_nickname_on_merge():
    """拼接时 nickname 同步更新为本条昵称。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "嗯", 1.0, "g1")
    buf.append("u1", "Alice2", "啊", 2.0, "g1")
    msgs = buf.flush()
    assert msgs[0].nickname == "Alice2"


# ---------------------------------------------------------------------- #
# flush / pending_text / pending_count
# ---------------------------------------------------------------------- #

def test_buffer_flush_returns_all_and_clears():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "hello", 1.0, "g1")
    buf.append("u2", "Bob", "world", 2.0, "g1")
    msgs = buf.flush()
    assert len(msgs) == 2
    assert buf.pending_count() == 0
    assert buf.flush() == []


def test_buffer_pending_text_preview():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "hello", 1.0, "g1")
    buf.append("u2", "Bob", "world", 2.0, "g1")
    assert buf.pending_text() == "hello world"


def test_buffer_pending_count_empty():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    assert buf.pending_count() == 0
    assert buf.pending_text() == ""


# ---------------------------------------------------------------------- #
# 溢出丢弃（消息风暴保护，PRD §6.6）
# ---------------------------------------------------------------------- #

def test_buffer_overflow_drops_oldest_with_warning():
    """超 max_size 丢弃最旧并 log warning。"""
    logs: list[tuple[str, str]] = []
    buf = GroupBuffer(max_size=3, log_fn=lambda lv, m: logs.append((lv, m)))
    for i in range(5):
        buf.append(f"u{i}", f"N{i}", f"msg{i}。", float(i), "g1")
    # 每条带句号不拼接，5 条入缓冲，max=3 → 丢弃 2 条最旧
    assert buf.pending_count() == 3
    msgs = buf.flush()
    assert msgs[0].text == "msg2。"  # 最旧保留的是 msg2
    assert msgs[2].text == "msg4。"
    # 应有 2 条 warning 日志
    warnings = [m for lv, m in logs if lv == "warning"]
    assert len(warnings) == 2
    assert any("缓冲区溢出" in w for w in warnings)


def test_buffer_overflow_log_failure_does_not_crash():
    """log_fn 抛异常不影响缓冲主流程。"""
    def bad_log(lv, m):
        raise RuntimeError("log broken")
    buf = GroupBuffer(max_size=2, log_fn=bad_log)
    for i in range(4):
        buf.append(f"u{i}", f"N{i}", f"m{i}。", float(i), "g1")
    assert buf.pending_count() == 2


# ---------------------------------------------------------------------- #
# dynamic_interval
# ---------------------------------------------------------------------- #

def test_buffer_dynamic_interval_high_rate_returns_min():
    """rate>=1.0/s → min_iv。"""
    assert GroupBuffer.dynamic_interval(1.0, 2.0, 5.0) == pytest.approx(2.0)
    assert GroupBuffer.dynamic_interval(2.0, 2.0, 5.0) == pytest.approx(2.0)


def test_buffer_dynamic_interval_low_rate_returns_max():
    """rate<=0.05/s → max_iv。"""
    assert GroupBuffer.dynamic_interval(0.05, 2.0, 5.0) == pytest.approx(5.0)
    assert GroupBuffer.dynamic_interval(0.01, 2.0, 5.0) == pytest.approx(5.0)


def test_buffer_dynamic_interval_zero_rate_returns_max():
    """rate=0 → max_iv。"""
    assert GroupBuffer.dynamic_interval(0.0, 2.0, 5.0) == pytest.approx(5.0)


def test_buffer_dynamic_interval_negative_rate_returns_max():
    """rate<0 当 0 处理 → max_iv。"""
    assert GroupBuffer.dynamic_interval(-1.0, 2.0, 5.0) == pytest.approx(5.0)


def test_buffer_dynamic_interval_mid_linear():
    """中间速率线性插值。rate=0.5 → ratio=(0.5-0.05)/0.95≈0.4737 → 5-0.4737*3≈3.579。"""
    val = GroupBuffer.dynamic_interval(0.5, 2.0, 5.0)
    ratio = (0.5 - 0.05) / (1.0 - 0.05)
    expected = 5.0 - ratio * (5.0 - 2.0)
    assert val == pytest.approx(expected)


def test_buffer_dynamic_interval_higher_rate_lower_interval():
    """速率越高间隔越短（单调性）。"""
    low = GroupBuffer.dynamic_interval(0.1, 2.0, 5.0)
    high = GroupBuffer.dynamic_interval(0.9, 2.0, 5.0)
    assert high < low


# ---------------------------------------------------------------------- #
# contains_turn_keyword
# ---------------------------------------------------------------------- #

def test_buffer_contains_turn_keyword_hit():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "说正事", 1.0, "g1")
    assert buf.contains_turn_keyword(["说正事", "别聊了"]) is True


def test_buffer_contains_turn_keyword_miss():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "天气不错", 1.0, "g1")
    assert buf.contains_turn_keyword(["说正事"]) is False


def test_buffer_contains_turn_keyword_empty_list():
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "说正事", 1.0, "g1")
    assert buf.contains_turn_keyword([]) is False


def test_buffer_contains_turn_keyword_matches_pending_text():
    """对 pending_text 整体匹配（跨消息拼接后命中）。"""
    buf = GroupBuffer(max_size=10, log_fn=lambda lv, m: None)
    buf.append("u1", "Alice", "天气不错", 1.0, "g1")
    buf.append("u2", "Bob", "换个话题", 2.0, "g1")
    # pending_text = "天气不错 换个话题"，含 "换个话题"
    assert buf.contains_turn_keyword(["换个话题"]) is True
