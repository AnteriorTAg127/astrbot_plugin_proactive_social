"""emoji 过滤工具（v0.3.5 F2）。

提供按 Unicode 范围移除 emoji 字符的纯函数，供 GroupBuffer.append 与
scheduler.on_message 在入缓冲前过滤 emoji，避免 emoji 污染 embedding 向量。

本文件不 import astrbot/numpy，纯标准库可离线测试。
"""

from __future__ import annotations

# Unicode emoji 范围（按 PRD F2 §2.2 定义）
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F300, 0x1FAFF),  # emoji symbols & pictographs
    (0x2600, 0x27BF),  # misc symbols & dingbats
    (0xFE00, 0xFE0F),  # variation selectors
    (0x1F1E6, 0x1F1FF),  # regional indicator pairs (国旗)
    (0x1F900, 0x1F9FF),  # supplemental symbols and pictographs
    (0x2300, 0x23FF),  # technical misc (含部分表情)
)


def _is_emoji_char(ch: str) -> bool:
    """判断单个字符是否属于 emoji 范围。"""
    cp = ord(ch)
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def strip_emoji(text: str) -> str:
    """移除 text 中所有 emoji 字符，返回过滤后的文本。

    保留所有非 emoji 字符（中文、英文、数字、标点、空白等）。
    边界：空字符串返回空字符串；纯 emoji 返回空字符串。
    """
    if not text:
        return ""
    return "".join(ch for ch in text if not _is_emoji_char(ch))


def is_pure_emoji(text: str) -> bool:
    """判断 text 是否为纯 emoji（过滤后为空或仅空白）。

    边界：空字符串返回 True（视为无效消息，不入缓冲）。
    """
    if not text:
        return True
    return not strip_emoji(text).strip()
