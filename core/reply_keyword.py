"""基于回复分词的连续对话匹配（v0.2.5，PRD F1-F10）。

纯计算模块，无 I/O、无 await，可离线测试。
jieba 通过 try-import 加载，缺失时 extract 返回 None、available() 返回 False（功能禁用）。

实现要点：
- ReplyKeywordCache：单次回复的关键词缓存（目标用户 + 关键词权重 dict + 过期时间 + 连续低分计数）。
- ReplyKeywordManager.extract：jieba TF-IDF 提取 top_n 关键词（带 weight），过滤纯数字/单字/标点；
  短文本（≤10 字）回退 jieba.lcut 全分词；权重归一化（除以 max）；jieba 不可用或提取结果为空返回 None。
- ReplyKeywordManager.match_score：对用户消息分词，计算 Σ(命中词权重) / Σ(所有词权重)。
"""

from __future__ import annotations

from dataclasses import dataclass

# jieba 可选 import（即使 requirements.txt 声明，运行时仍可能未装）
try:
    import jieba
    import jieba.analyse

    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False


def _is_valid_keyword(word: str) -> bool:
    """检查分词是否为有效关键词。

    过滤规则：非空、非纯数字、长度 >1、非纯标点。
    纯标点判定：word 可打印且不含任何字母数字字符。
    """
    if not word:
        return False
    if word.isdigit():
        return False
    if len(word) <= 1:
        return False
    if word.isprintable() and not any(c.isalnum() for c in word):
        return False
    return True


@dataclass
class ReplyKeywordCache:
    """单次回复的关键词缓存。

    Attributes:
        target_user_id: Bot 上一次回复的直接目标用户 ID。
        keywords: word -> 归一化权重（范围 [0,1]）。
        expire_at: 过期时间戳（秒）。
        low_score_streak: 连续低分计数（用于提前清除）。
    """

    target_user_id: str
    keywords: dict[str, float]
    expire_at: float
    low_score_streak: int = 0

    def is_valid_for(self, user_id: str, now: float) -> bool:
        """缓存对指定用户在指定时刻是否有效（用户匹配 + 未过期 + 关键词非空）。"""
        return (
            self.target_user_id == user_id
            and now < self.expire_at
            and bool(self.keywords)
        )

    def is_expired(self, now: float) -> bool:
        """缓存是否已过期。"""
        return now >= self.expire_at


class ReplyKeywordManager:
    """关键词提取 + 匹配打分（静态方法，无状态）。"""

    @staticmethod
    def available() -> bool:
        """jieba 是否可导入（功能是否可用）。"""
        return _JIEBA_AVAILABLE

    @staticmethod
    def extract(
        text: str, target_user_id: str, now: float, cfg: dict
    ) -> ReplyKeywordCache | None:
        """从文本提取关键词并构建缓存。

        Args:
            text: 待提取的文本（Bot 回复内容）。
            target_user_id: 目标用户 ID。
            now: 当前时间戳（秒）。
            cfg: 配置 dict，读取 reply_keyword_top_n / reply_keyword_ttl_seconds。

        Returns:
            ReplyKeywordCache 或 None（jieba 不可用 / 输入为空 / 提取结果为空 / 异常）。
        """
        if not _JIEBA_AVAILABLE or not text or not target_user_id:
            return None
        try:
            top_n = max(1, int(cfg.get("reply_keyword_top_n", 5)))
            ttl = max(1.0, float(cfg.get("reply_keyword_ttl_seconds", 60)))

            # TF-IDF 提取关键词（带权重）
            raw = jieba.analyse.extract_tags(text, topK=top_n, withWeight=True)
            cleaned: dict[str, float] = {}
            for word, weight in raw:
                if not _is_valid_keyword(word):
                    continue
                cleaned[word] = float(weight)

            # 短文本回退：TF-IDF 结果为空且文本 ≤10 字时用全分词
            if not cleaned and len(text) <= 10:
                for word in jieba.lcut(text):
                    if not _is_valid_keyword(word):
                        continue
                    if word not in cleaned:  # 去重，保留首次出现
                        cleaned[word] = 1.0

            if not cleaned:
                return None

            # 权重归一化（除以 max，使最大权重为 1.0）
            max_w = max(cleaned.values())
            if max_w > 0:
                cleaned = {w: v / max_w for w, v in cleaned.items()}

            # 截断到 top_n（按 weight 降序取前 top_n 项）
            if len(cleaned) > top_n:
                sorted_items = sorted(
                    cleaned.items(), key=lambda kv: kv[1], reverse=True
                )
                cleaned = dict(sorted_items[:top_n])

            return ReplyKeywordCache(
                target_user_id=target_user_id,
                keywords=cleaned,
                expire_at=now + ttl,
                low_score_streak=0,
            )
        except Exception:
            return None

    @staticmethod
    def match_score(user_text: str, keywords: dict[str, float]) -> float:
        """计算用户文本与关键词的匹配得分（命中加权占比）。

        Args:
            user_text: 用户消息文本。
            keywords: 关键词 -> 权重 dict。

        Returns:
            匹配得分 [0,1]；keywords 为空 / user_text 为空 / jieba 不可用 / 异常时返回 0.0。
        """
        if not keywords or not user_text:
            return 0.0
        if not _JIEBA_AVAILABLE:
            return 0.0
        try:
            user_words = set(jieba.lcut(user_text))
            total_w = sum(keywords.values())
            if total_w <= 0:
                return 0.0
            matched_w = sum(
                weight for word, weight in keywords.items() if word in user_words
            )
            return matched_w / total_w
        except Exception:
            return 0.0
