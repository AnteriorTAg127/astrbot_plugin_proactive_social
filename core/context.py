"""双窗口上下文（模块 C 产出）。

每群一个 GroupContext：
- 原始消息窗口（展示用，按时间顺序保留最近 long_size 条 LogicalMessage）
- 批次嵌入历史（计算用，保留最近 50 条 BatchRecord，供 topic_embedding / select_long_relevant 复用）

本文件仅依赖 numpy 与 .models，不 import astrbot / engine，保证离线可测。
余弦相似度工具 `_cosine` 在本模块内独立实现，避免与 engine.py 形成循环依赖
（模块 C 与 D 并行开发）。
"""

from __future__ import annotations

import numpy as np

from .models import BatchRecord, LogicalMessage

# 机器人发言者在 _messages 中的标识（recent_speakers 排除）
_BOT_USER_ID = "__bot__"
# 批次历史保留上限：足够覆盖 topic_embedding(max_batches=6) 与长窗口相关性查询
_MAX_BATCHES = 50


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（numpy 实现）。

    边界：空向量、长度不一致、零向量均返回 0.0，不抛异常。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class GroupContext:
    """每群一个：原始消息窗口（展示用）+ 批次嵌入历史（计算用）"""

    def __init__(self, short_size: int, long_size: int):
        self._short_size = short_size
        self._long_size = long_size
        self._messages: list[LogicalMessage] = []
        self._batches: list[BatchRecord] = []

    def add_message(self, msg: LogicalMessage) -> None:
        """追加一条消息到窗口，超出 long_size 时丢弃最旧。"""
        self._messages.append(msg)
        if len(self._messages) > self._long_size:
            # 丢弃最旧的（保持 list 长度 ≤ long_size）
            del self._messages[: len(self._messages) - self._long_size]

    def add_bot_message(self, text: str, ts: float) -> None:
        """记录机器人发言（sender 标记为 "__bot__"），加入 _messages。

        GroupContext 不持有 group_id，bot 消息的 group_id 填空字符串。
        """
        self.add_message(
            LogicalMessage(
                user_id=_BOT_USER_ID,
                nickname="机器人",
                text=text,
                ts=ts,
                group_id="",
            )
        )

    def push_batch(self, record: BatchRecord) -> None:
        """追加批次记录，超出 _MAX_BATCHES 时丢弃最旧。"""
        self._batches.append(record)
        if len(self._batches) > _MAX_BATCHES:
            del self._batches[: len(self._batches) - _MAX_BATCHES]

    def short_window_text(self) -> str:
        """最近 short_size 条消息，"昵称: 内容" 格式，换行拼接。"""
        if self._short_size <= 0:
            return ""
        msgs = self._messages[-self._short_size :]
        return "\n".join(f"{m.nickname}: {m.text}" for m in msgs)

    def topic_embedding(self, max_batches: int = 6) -> list[float] | None:
        """最近 max_batches 个 BatchRecord 嵌入均值（s_topic 用）。

        无 batch / max_batches<=0 / 所有 embedding 为空 → 返回 None。
        """
        if not self._batches or max_batches <= 0:
            return None
        recent = self._batches[-max_batches:]
        embs = [b.embedding for b in recent if b.embedding]
        if not embs:
            return None
        mean = np.mean(np.asarray(embs, dtype=float), axis=0)
        return mean.tolist()

    def select_long_relevant(
        self,
        anchor_emb: list[float],
        top_n: int,
        within_sec: float = 300,
    ) -> list[str]:
        """长窗口相关性选择（PRD F3 嵌入相关性过滤）。

        简化语义：取 _batches 中 ts 不早于 (最近消息 ts - within_sec) 的批次，
        按其 embedding 与 anchor_emb 的余弦相似度降序，取 top_n 个批次的 text。

        - anchor_emb 为空 / 无候选批次 / top_n<=0 → 返回 []
        - 时间锚点优先取 _messages 最后一条 ts；无消息则取 _batches 最新 ts
        """
        if not anchor_emb or not self._batches or top_n <= 0:
            return []
        anchor_ts = self._messages[-1].ts if self._messages else self._batches[-1].ts
        candidates: list[tuple[float, str]] = []
        for b in self._batches:
            if not b.embedding:
                continue
            if b.ts < anchor_ts - within_sec:
                continue
            sim = _cosine(anchor_emb, b.embedding)
            candidates.append((sim, b.text))
        if not candidates:
            return []
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [text for _, text in candidates[:top_n]]

    def recent_speakers(self, n: int) -> list[tuple[str, str, str]]:
        """从 _messages 末尾向前取最近 n 个非 bot 不同用户。

        返回 [(user_id, nickname, last_text)]，同一用户只保留最新一条；
        顺序为从最近到较远；不足 n 个则返回实际数量。
        """
        if n <= 0:
            return []
        seen: set[str] = set()
        result: list[tuple[str, str, str]] = []
        for m in reversed(self._messages):
            if m.user_id == _BOT_USER_ID:
                continue
            if m.user_id in seen:
                continue
            seen.add(m.user_id)
            result.append((m.user_id, m.nickname, m.text))
            if len(result) >= n:
                break
        return result

    def last_message_ts(self) -> float:
        """_messages 最后一条的 ts，无消息返回 0.0。"""
        return self._messages[-1].ts if self._messages else 0.0
