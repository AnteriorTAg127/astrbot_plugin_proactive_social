"""消息过滤模块（v0.3.11）。

在 on_group_message 入口处过滤无意义消息，避免 "打卡/赞我/111" 等内容
污染决策管线。命中过滤规则的消息完全不进入决策管线（不进 buffer、不计
context、不参与评分），直接 return。

5 条规则（顺序 A→B→C→D→E，任一命中即返回不再继续）：
- A 黑名单匹配：精确匹配 OR 短消息（长度 ≤ filter_short_msg_len）包含黑名单词
- B 纯拟声/无意义短语：精确匹配 filter_meaningless_phrases 词表
- C 重复刷屏：同一 user_id 在 filter_burst_window 秒内发送 ≥ filter_burst_count
  条相同消息（去空白后）
- D 纯表情/单字：正则 ^[\\s\\W\\d_]+$ 或长度 == 1
- E 超短消息：长度 ≤ filter_short_msg_len 且不含问号（? 或 ？）

本文件不 import astrbot，纯标准库可离线测试。
"""

from __future__ import annotations

import re
from collections import defaultdict, deque

# 规则 D：纯表情/单字正则（只含空白、非字母数字汉字、数字、下划线）
_PURE_SYMBOL_RE = re.compile(r"^[\s\W\d_]+$")

# 重复刷屏 deque 限长，避免内存泄漏
_BURST_DEQUE_MAXLEN = 20


class MessageFilter:
    """消息过滤器。

    构造函数接收 cfg dict，读取 filter_* 配置项（带默认值）。
    should_filter(text, user_id, ts) 按顺序检查 5 条规则，返回
    (是否过滤, 命中规则名)。规则名为 "" 表示不过滤，"A"/"B"/"C"/"D"/"E"
    表示对应规则命中。

    重复刷屏检测内部维护 {user_id: deque[(text, ts)]}，deque maxlen=20，
    不持久化（重启清空可接受）。
    """

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("filter_enabled", True))
        # 黑名单/短语用 set 加速查找
        self.blacklist: set[str] = set(
            cfg.get("filter_blacklist", ["打卡", "赞我", "+1", "111", "ddd"])
        )
        self.meaningless_phrases: set[str] = set(
            cfg.get(
                "filter_meaningless_phrases",
                ["啊啊啊", "哈哈哈哈", "嗯嗯", "哦哦", "呜呜"],
            )
        )
        self.short_msg_len: int = int(cfg.get("filter_short_msg_len", 2))
        self.burst_count: int = int(cfg.get("filter_burst_count", 3))
        self.burst_window: int = int(cfg.get("filter_burst_window", 10))
        # 重复刷屏检测状态：{user_id: deque[(normalized_text, ts)]}
        self._burst_history: dict[str, deque[tuple[str, float]]] = defaultdict(
            lambda: deque(maxlen=_BURST_DEQUE_MAXLEN)
        )

    def should_filter(self, text: str, user_id: str, ts: float) -> tuple[bool, str]:
        """检查消息是否应被过滤。

        Args:
            text: 消息文本
            user_id: 发送者 user_id
            ts: 消息时间戳（epoch 秒）

        Returns:
            (是否过滤, 命中规则名)。规则名为 "" 表示不过滤，
            "A"/"B"/"C"/"D"/"E" 表示对应规则命中。
        """
        # 总开关关闭时所有规则失效
        if not self.enabled:
            return (False, "")

        # 规则 A：黑名单匹配
        if self._check_blacklist(text):
            return (True, "A")

        # 规则 B：纯拟声/无意义短语（精确匹配）
        if text in self.meaningless_phrases:
            return (True, "B")

        # 规则 C：重复刷屏
        if self._check_burst(text, user_id, ts):
            return (True, "C")

        # 规则 D：纯表情/单字
        if self._is_pure_symbol(text):
            return (True, "D")

        # 规则 E：超短消息
        if self._is_too_short(text):
            return (True, "E")

        return (False, "")

    # ---- 规则 A：黑名单匹配 ----
    def _check_blacklist(self, text: str) -> bool:
        """精确匹配 OR 短消息（长度 ≤ short_msg_len）包含黑名单词。"""
        if not text:
            return False
        # 精确匹配
        if text in self.blacklist:
            return True
        # 短消息包含黑名单词
        if len(text) <= self.short_msg_len:
            for word in self.blacklist:
                # 跳过空字符串避免 "" in text 恒真
                if word and word in text:
                    return True
        return False

    # ---- 规则 C：重复刷屏 ----
    def _check_burst(self, text: str, user_id: str, ts: float) -> bool:
        """同一 user_id 在 burst_window 秒内发送 ≥ burst_count 条相同消息（去空白后）。

        每次调用先清理超时记录，再将当前消息加入 deque，最后统计相同消息条数。
        """
        normalized = "".join(text.split())
        history = self._burst_history[user_id]
        # 清理超时记录（ts < ts_now - burst_window）
        cutoff = ts - self.burst_window
        while history and history[0][1] < cutoff:
            history.popleft()
        # 加入当前消息
        history.append((normalized, ts))
        # 统计相同消息条数
        count = sum(1 for t, _ in history if t == normalized)
        return count >= self.burst_count

    # ---- 规则 D：纯表情/单字 ----
    @staticmethod
    def _is_pure_symbol(text: str) -> bool:
        """正则 ^[\\s\\W\\d_]+$ 匹配 或 长度 == 1。"""
        if not text:
            return False
        # 长度 == 1（单字符，含单字/单 emoji/单标点）
        if len(text) == 1:
            return True
        # 只含空白、非字母数字汉字、数字、下划线
        return bool(_PURE_SYMBOL_RE.match(text))

    # ---- 规则 E：超短消息 ----
    def _is_too_short(self, text: str) -> bool:
        """长度 ≤ short_msg_len 且不含问号（? 或 ？）。"""
        if len(text) > self.short_msg_len:
            return False
        # 不含问号（避免误伤短问句）
        if "?" in text or "？" in text:
            return False
        return True
