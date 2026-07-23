"""消息缓冲与动态批处理（模块 C 产出）。

GroupBuffer：每群一个，收集 LogicalMessage，同用户连续短消息自动拼接；
溢出按 max_size 丢弃最旧并告警（PRD §6.6 消息风暴）；提供动态批次间隔映射
与紧急转弯关键词检测（PRD F2）。

本文件仅依赖 .models，不 import astrbot / numpy，纯逻辑可离线测试。
"""

from __future__ import annotations

from collections.abc import Callable

from ..common.models import LogicalMessage

# 短消息判定阈值：≤5 字
_SHORT_TEXT_MAX_LEN = 5
# 结束标点：任一出现则该条不再视为短消息
_ENDING_PUNCTS = "。！？!?~"


def _is_short_text(text: str) -> bool:
    """短消息判定：len(text) <= 5 且不含任何结束标点 。！？!?~。"""
    if len(text) > _SHORT_TEXT_MAX_LEN:
        return False
    for ch in _ENDING_PUNCTS:
        if ch in text:
            return False
    return True


class GroupBuffer:
    """消息缓冲与动态批处理"""

    def __init__(self, max_size: int, log_fn: Callable[[str, str], None]):
        self._max_size = max_size
        self._log = log_fn
        self._items: list[LogicalMessage] = []

    def append(
        self,
        user_id: str,
        nickname: str,
        text: str,
        ts: float,
        group_id: str,
        is_wake: bool = False,
    ) -> None:
        """追加消息。同用户连续短消息自动拼接为一条逻辑消息（PRD F2）。

        拼接条件：上一条存在、user_id 相同、上一条与本条均为短消息
        （≤5 字且无结束标点）。拼接时：text 用空格连接，ts 更新为本条 ts
        （取最新时间），nickname 同步更新，is_wake 取 OR（任一条 @Bot 即整条视为 @）。

        否则：append 新 LogicalMessage。

        缓冲超 max_size：丢弃最旧并 warning（PRD §6.6 消息风暴）。
        """
        merged = False
        if self._items:
            last = self._items[-1]
            if (
                last.user_id == user_id
                and _is_short_text(last.text)
                and _is_short_text(text)
            ):
                last.text = f"{last.text} {text}"
                last.ts = ts
                last.nickname = nickname
                # 拼接后 is_wake 取 OR：任一条 @Bot 即整条视为 @（v0.2 批次级判定）
                if is_wake:
                    last.is_wake = True
                merged = True
        if not merged:
            self._items.append(
                LogicalMessage(
                    user_id=user_id,
                    nickname=nickname,
                    text=text,
                    ts=ts,
                    group_id=group_id,
                    is_wake=is_wake,
                )
            )

        # 超限丢弃最旧（消息风暴保护）
        while len(self._items) > self._max_size:
            dropped = self._items.pop(0)
            try:
                self._log(
                    "warning",
                    f"[ProSocial] 缓冲区溢出，丢弃最旧消息: "
                    f"group={dropped.group_id} user={dropped.user_id} ts={dropped.ts}",
                )
            except Exception:
                # 日志失败不应影响缓冲主流程
                pass

    def flush(self) -> list[LogicalMessage]:
        """取出全部并清空。"""
        items = self._items
        self._items = []
        return items

    def pending_text(self) -> str:
        """当前缓冲拼接预览：所有 items 的 text 用空格拼接。"""
        return " ".join(m.text for m in self._items)

    def pending_count(self) -> int:
        """当前缓冲逻辑消息条数。"""
        return len(self._items)

    @staticmethod
    def dynamic_interval(
        recent_rate_per_sec: float,
        min_iv: float,
        max_iv: float,
    ) -> float:
        """速率高 → 接近 min_iv；速率低 → 接近 max_iv（线性映射）。

        - rate ≥ 1.0/s → min_iv
        - rate ≤ 0.05/s → max_iv
        - 中间线性插值：ratio = (rate - 0.05) / (1.0 - 0.05)，clamp 到 [0,1]
          return max_iv - ratio * (max_iv - min_iv)
        - rate < 0 当 0 处理（直接落到 max_iv）
        """
        rate = float(recent_rate_per_sec)
        if rate < 0.0:
            rate = 0.0
        lo, hi = 0.05, 1.0
        if rate <= lo:
            return max_iv
        if rate >= hi:
            return min_iv
        ratio = (rate - lo) / (hi - lo)
        return max_iv - ratio * (max_iv - min_iv)

    def contains_turn_keyword(self, keywords: list[str]) -> bool:
        """检查当前缓冲文本是否包含 keywords 中任一词（子串匹配）。

        对 pending_text 整体匹配（符合 PRD F2 "缓冲区文本命中转折词表" 语义）。
        """
        if not keywords:
            return False
        text = self.pending_text()
        return any(kw in text for kw in keywords)
