"""个人跟踪列表（模块 C 产出，对应 PRD F4 无 @ 接话）。

PersonalTracker：每群一个，维护 user_id -> TrackerEntry 字典，提供超时与
连续不相关清理。纯数据结构，不 import astrbot / numpy，可离线测试。
"""

from __future__ import annotations

from .models import TrackerEntry


class PersonalTracker:
    """个人跟踪列表（F4 无 @ 接话）"""

    def __init__(self):
        self._entries: dict[str, TrackerEntry] = {}

    def add(self, entry: TrackerEntry) -> None:
        """添加/覆盖跟踪条目（同 user_id 重复 add 以新为准）。"""
        self._entries[entry.user_id] = entry

    def get(self, user_id: str) -> TrackerEntry | None:
        """获取指定用户跟踪条目，不存在返回 None。"""
        return self._entries.get(user_id)

    def remove(self, user_id: str) -> None:
        """移除指定用户跟踪条目，不存在则静默无操作。"""
        self._entries.pop(user_id, None)

    def all(self) -> list[TrackerEntry]:
        """返回全部跟踪条目列表。"""
        return list(self._entries.values())

    def bump_irrelevant(self, user_id: str) -> int:
        """对应 entry 的连续不相关计数 +1，返回新值；entry 不存在返回 0。"""
        entry = self._entries.get(user_id)
        if entry is None:
            return 0
        entry.irrelevant_streak += 1
        return entry.irrelevant_streak

    def cleanup(
        self,
        now: float,
        timeout_sec: float,
        max_irrelevant: int,
    ) -> list[str]:
        """超时或连续不相关达上限者移除，返回被移除的 user_id 列表。

        - now - entry.created_ts > timeout_sec → 超时移除
        - entry.irrelevant_streak >= max_irrelevant → 连续不相关移除
        """
        removed: list[str] = []
        for uid, entry in list(self._entries.items()):
            if (now - entry.created_ts > timeout_sec) or (
                entry.irrelevant_streak >= max_irrelevant
            ):
                del self._entries[uid]
                removed.append(uid)
        return removed
