"""test_tracker.py —— C 个人跟踪列表（F4 无 @ 接话）。

测试对象：core/tracker.py → PersonalTracker
覆盖点：add / get / remove / all / bump_irrelevant / cleanup（超时 + 连续不相关）。
"""

from __future__ import annotations

from core.common.models import TrackerEntry
from core.tracking.tracker import PersonalTracker


def _entry(uid: str, created_ts: float = 0.0, streak: int = 0) -> TrackerEntry:
    return TrackerEntry(
        user_id=uid,
        nickname=f"N{uid}",
        bot_last_emb=[1.0, 0.0],
        last_own_text="hi",
        created_ts=created_ts,
        irrelevant_streak=streak,
    )


def test_tracker_add_get():
    t = PersonalTracker()
    e = _entry("u1")
    t.add(e)
    assert t.get("u1") is e
    assert t.get("u2") is None


def test_tracker_add_overwrite_same_user():
    """同 user_id 重复 add 以新为准。"""
    t = PersonalTracker()
    t.add(_entry("u1", created_ts=1.0))
    t.add(_entry("u1", created_ts=5.0))
    assert t.get("u1").created_ts == 5.0
    assert len(t.all()) == 1


def test_tracker_remove_existing():
    t = PersonalTracker()
    t.add(_entry("u1"))
    t.remove("u1")
    assert t.get("u1") is None
    assert t.all() == []


def test_tracker_remove_nonexistent_silent():
    """移除不存在条目静默无操作。"""
    t = PersonalTracker()
    t.remove("u_not_exist")  # 不抛
    assert t.all() == []


def test_tracker_all_returns_copy():
    """all() 返回列表副本，修改不影响内部。"""
    t = PersonalTracker()
    t.add(_entry("u1"))
    lst = t.all()
    lst.clear()
    assert t.get("u1") is not None


def test_tracker_bump_irrelevant_increments():
    t = PersonalTracker()
    t.add(_entry("u1"))
    assert t.bump_irrelevant("u1") == 1
    assert t.bump_irrelevant("u1") == 2
    assert t.get("u1").irrelevant_streak == 2


def test_tracker_bump_irrelevant_nonexistent_returns_zero():
    t = PersonalTracker()
    assert t.bump_irrelevant("u_x") == 0


def test_tracker_cleanup_timeout_removes():
    """超时移除。"""
    t = PersonalTracker()
    t.add(_entry("u1", created_ts=0.0))
    t.add(_entry("u2", created_ts=100.0))
    # now=50, timeout=30 → u1(0+30<50 超时) 移除, u2(100+30>50 保留)
    removed = t.cleanup(now=50.0, timeout_sec=30.0, max_irrelevant=3)
    assert "u1" in removed
    assert "u2" not in removed
    assert t.get("u1") is None
    assert t.get("u2") is not None


def test_tracker_cleanup_max_irrelevant_removes():
    """连续不相关达上限移除。"""
    t = PersonalTracker()
    t.add(_entry("u1", created_ts=0.0, streak=3))
    t.add(_entry("u2", created_ts=0.0, streak=2))
    # max_irrelevant=3 → u1(3>=3) 移除, u2(2<3) 保留
    removed = t.cleanup(now=10.0, timeout_sec=100.0, max_irrelevant=3)
    assert removed == ["u1"]
    assert t.get("u2") is not None


def test_tracker_cleanup_keeps_valid_entries():
    """未超时且未达上限的保留。"""
    t = PersonalTracker()
    t.add(_entry("u1", created_ts=10.0, streak=0))
    removed = t.cleanup(now=20.0, timeout_sec=100.0, max_irrelevant=3)
    assert removed == []
    assert t.get("u1") is not None


def test_tracker_cleanup_empty_returns_empty():
    t = PersonalTracker()
    assert t.cleanup(now=100.0, timeout_sec=30.0, max_irrelevant=3) == []
