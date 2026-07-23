"""唤醒决策引擎（模块 D，纯计算无 I/O）。

实现 PRD F2 的五因子评分公式与动态阈值修正。全部静态方法、无状态、无 I/O、无 await，
可离线单测。严禁 import astrbot，严禁 import 其他 core 模块（engine 是被依赖方）。
"""

from __future__ import annotations

import numpy as np

from ..common.models import InterestData, ScoreFactors

# 仅 core/general/marginal 参与兴趣评分（hate 走 hate_score 单独屏蔽）
_INTEREST_LEVELS: tuple[str, ...] = ("core", "general", "marginal")
_INT_CAP: float = 1.5  # s_int 加权最大值 cap 上限（PRD F2）


class WakeEngine:
    """唤醒决策引擎：余弦相似度 + 五因子融合 + 动态阈值（PRD F2）。"""

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """numpy 余弦相似度；零向量 / 长度不一致 / 空列表均返回 0.0（不抛异常）。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))

    @staticmethod
    def interest_score(
        batch_emb: list[float], interest: InterestData | None
    ) -> tuple[float, str]:
        """返回 (加权最大余弦相似度 cap 1.5, 命中级别 'core'/'general'/'marginal'/'none')。

        - interest 为 None 或无任何有效级别质心 → (0.0, "none")
        - hate 级别不参与本函数（走 hate_score）
        """
        if interest is None:
            return 0.0, "none"
        best_weighted, best_level = 0.0, "none"
        for level in _INTEREST_LEVELS:
            centroid = interest.centroids.get(level)
            if centroid is None:
                continue
            weighted = WakeEngine.cosine(batch_emb, centroid) * float(
                interest.weights.get(level, 1.0)
            )
            if weighted > best_weighted:
                best_weighted, best_level = weighted, level
        return min(best_weighted, _INT_CAP), best_level

    @staticmethod
    def hate_score(batch_emb: list[float], interest: InterestData | None) -> float:
        """interest 无 hate 质心 → 0.0；否则返回 batch_emb 与 hate 质心的余弦。"""
        if interest is None:
            return 0.0
        centroid = interest.centroids.get("hate")
        return 0.0 if centroid is None else WakeEngine.cosine(batch_emb, centroid)

    @staticmethod
    def evaluate(
        *,
        batch_emb: list[float],
        interest: InterestData | None,
        topic_emb: list[float] | None,
        bot_last_emb: list[float] | None,
        expecting: bool,
        cooldown_ratio: float,
        silence_sec: float,
        weights: dict,
        base_threshold: float,
        modifiers: dict,
    ) -> tuple[ScoreFactors, float, float, str]:
        """返回 (factors, score, threshold, hit_level)。weights 键:
        w_int/w_topic/w_resp/w_cooldown/w_silence；modifiers 键:
        core/general/marginal/expecting；
        threshold = base_threshold * 级别修正 * (expecting ? expecting_modifier : 1)。"""
        # --- 五因子（PRD F2）---
        if interest is None:
            s_int, hit_level = 0.0, "none"
        else:
            s_int, hit_level = WakeEngine.interest_score(batch_emb, interest)
        s_topic = (
            WakeEngine.cosine(batch_emb, topic_emb) if topic_emb is not None else 0.0
        )
        s_resp = (
            WakeEngine.cosine(batch_emb, bot_last_emb)
            if expecting and bot_last_emb is not None
            else 0.0
        )
        c_cooldown = float(cooldown_ratio)  # 调用方算好的衰减值（0~1）
        p_silence = min(float(silence_sec) / 300.0, 1.0)  # min(sec/300, 1)

        factors = ScoreFactors(s_int, s_topic, s_resp, c_cooldown, p_silence)

        # --- 融合总分（PRD F2 公式）---
        # score = w1*s_int + w2*s_topic + w3*s_resp - w4*c_cooldown + w5*p_silence
        w_int = float(weights.get("w_int", 1.0))
        w_topic = float(weights.get("w_topic", 0.4))
        w_resp = float(weights.get("w_resp", 0.8))
        w_cooldown = float(weights.get("w_cooldown", 0.5))
        w_silence = float(weights.get("w_silence", 0.2))
        score = (
            w_int * s_int
            + w_topic * s_topic
            + w_resp * s_resp
            - w_cooldown * c_cooldown
            + w_silence * p_silence
        )

        # --- 动态阈值（PRD F2）：级别修正 × 期待修正 ---
        if hit_level == "core":
            level_mod = float(modifiers.get("core", 0.7))
        elif hit_level == "general":
            level_mod = float(modifiers.get("general", 1.0))
        elif hit_level == "marginal":
            level_mod = float(modifiers.get("marginal", 1.3))
        else:
            level_mod = 1.0
        expecting_mod = float(modifiers.get("expecting", 0.8)) if expecting else 1.0
        threshold = float(base_threshold) * level_mod * expecting_mod

        return factors, float(score), float(threshold), hit_level

    @staticmethod
    def rule_fallback(
        text: str,
        interest: InterestData | None,
        silence_sec: float,
        silence_threshold: float = 180,
    ) -> bool:
        """嵌入不可用时的降级（PRD §6.6 / F2 降级策略）。命中 high_interest_keywords
        任一关键词（子串匹配）且 silence_sec ≥ silence_threshold → True；
        interest 为 None 或关键词表为空 → False。"""
        if interest is None or not text:
            return False
        keywords = interest.high_interest_keywords
        if not keywords or float(silence_sec) < float(silence_threshold):
            return False
        return any(kw and kw in text for kw in keywords)
