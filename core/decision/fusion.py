"""双通道融合引擎（v0.2，PRD F10）。

纯计算模块，静态方法、无 I/O、无 await、无状态，离线可测。
final = alpha * score_a + (1 - alpha) * score_b
融合阈值含级别修正(B) x 疲劳修正(A) x 期待修正 x 惯性修正。
"""

from __future__ import annotations

from ..common.models import FusionResult


class FusionEngine:
    """双通道融合引擎：通道开关 + 动态权重解析 + 修正因子 + 融合打分。

    全部静态方法，纯计算，禁止 import astrbot / numpy。
    读 config 一律 config.get(key, default)，异常输入给合理默认不抛异常。
    """

    @staticmethod
    def resolve_alpha(
        *,
        vector_enabled: bool,
        rule_enabled: bool,
        dynamic_enabled: bool,
        mentions_bot: bool,
        has_direct_word: bool,
        is_short: bool,
        expecting: bool,
        config: dict,
    ) -> float:
        """通道开关 + 动态权重（PRD F10），返回 alpha in [0,1]。

        - rule_enabled 且 not vector_enabled -> 1.0          # 仅 A（规则独立）
        - vector_enabled 且 not rule_enabled -> 0.0          # 仅 B（向量独立）
        - not rule_enabled 且 not vector_enabled -> 0.0      # 双关兜底
        - 双开（两者均启用）：
            base = float(config.get('fusion_weight_rule', 0.4))
            若 dynamic_enabled：
                mentions_bot 或 has_direct_word -> alpha = float(config.get('dynamic_alpha_wake', 0.8))
                elif is_short 且 expecting      -> alpha = float(config.get('dynamic_alpha_short_expect', 0.2))
                else                            -> alpha = base
            否则 alpha = base
        - 结果 clamp 到 [0,1]。
        """
        # 仅 A：规则通道单独开启，向量通道关闭
        if rule_enabled and not vector_enabled:
            return 1.0

        # 仅 B 或双关：向量通道单独开启，或两者均关闭（退化到仅 B 兜底）
        if not rule_enabled:
            return 0.0

        # 双开：rule_enabled 且 vector_enabled
        base = float(config.get("fusion_weight_rule", 0.4))

        if dynamic_enabled:
            # 强唤醒词或 @Bot -> 高权重信任规则通道
            if mentions_bot or has_direct_word:
                alpha = float(config.get("dynamic_alpha_wake", 0.8))
            # 短消息（<=8 字）且处于期待回复 -> 低权重，更信任语义通道
            elif is_short and expecting:
                alpha = float(config.get("dynamic_alpha_short_expect", 0.2))
            else:
                alpha = base
        else:
            alpha = base

        # clamp 到 [0, 1]
        if alpha < 0.0:
            alpha = 0.0
        elif alpha > 1.0:
            alpha = 1.0
        return alpha

    @staticmethod
    def b_modifier(hit_level: str, config: dict) -> float:
        """级别修正因子（通道 B 兴趣级别 -> 阈值倍率）。

        - core     -> float(config.get('core_interest_modifier', 0.7))
        - general  -> 1.0
        - marginal -> float(config.get('edge_interest_modifier', 1.3))
        - 其他('none'/'hate'/...) -> 1.0
        """
        if hit_level == "core":
            return float(config.get("core_interest_modifier", 0.7))
        if hit_level == "marginal":
            return float(config.get("edge_interest_modifier", 1.3))
        # general 或任何其他值（'none'/'hate'/...）-> 1.0
        return 1.0

    @staticmethod
    def a_modifier(fatigue_level: str, config: dict) -> float:
        """疲劳修正因子（全局疲劳级别 -> 阈值倍率）。

        - high   -> float(config.get('fatigue_high_modifier', 1.2))
        - medium -> float(config.get('fatigue_medium_modifier', 1.1))
        - 其他   -> 1.0
        """
        if fatigue_level == "high":
            return float(config.get("fatigue_high_modifier", 1.2))
        if fatigue_level == "medium":
            return float(config.get("fatigue_medium_modifier", 1.1))
        # low / none / 其他 -> 1.0
        return 1.0

    @staticmethod
    def fuse(
        *,
        score_a: float,
        score_b: float,
        hit_level: str,
        expecting: bool,
        mentions_bot: bool,
        has_direct_word: bool,
        is_short: bool,
        vector_enabled: bool,
        rule_enabled: bool,
        dynamic_enabled: bool,
        inertia_multiplier: float,
        fatigue_level: str,
        config: dict,
    ) -> FusionResult:
        """双通道融合主入口。

        1. alpha = resolve_alpha(...)
        2. final_score = alpha * score_a + (1 - alpha) * score_b
        3. b_mod = b_modifier(hit_level, config)
        4. a_mod = a_modifier(fatigue_level, config)
        5. expecting_mod = float(config.get('expecting_modifier', 0.8)) if expecting else 1.0
        6. inertia_multiplier = max(float(inertia_multiplier), 0.0)  # 防御负值
        7. threshold = base_threshold * b_mod * a_mod * expecting_mod * inertia_multiplier
        8. 返回 FusionResult(score_a, score_b, alpha, final_score, threshold, b_mod, a_mod, inertia_multiplier)

        任何意外异常 -> 兜底返回以 score_b 为 final、threshold=base_threshold 的 FusionResult（不抛）。
        """
        try:
            alpha = FusionEngine.resolve_alpha(
                vector_enabled=vector_enabled,
                rule_enabled=rule_enabled,
                dynamic_enabled=dynamic_enabled,
                mentions_bot=mentions_bot,
                has_direct_word=has_direct_word,
                is_short=is_short,
                expecting=expecting,
                config=config,
            )

            final_score = alpha * score_a + (1.0 - alpha) * score_b

            b_mod = FusionEngine.b_modifier(hit_level, config)
            a_mod = FusionEngine.a_modifier(fatigue_level, config)

            expecting_mod = (
                float(config.get("expecting_modifier", 0.8)) if expecting else 1.0
            )

            # 防御：惯性倍率不能为负
            safe_inertia = max(float(inertia_multiplier), 0.0)

            threshold = (
                float(config.get("base_threshold", 0.65))
                * b_mod
                * a_mod
                * expecting_mod
                * safe_inertia
            )

            return FusionResult(
                score_a=score_a,
                score_b=score_b,
                alpha=alpha,
                final_score=final_score,
                threshold=threshold,
                b_modifier=b_mod,
                a_modifier=a_mod,
                inertia_multiplier=safe_inertia,
            )
        except Exception:
            # 兜底：以 score_b 为最终分，threshold 使用 base_threshold 默认值
            base_threshold = float(config.get("base_threshold", 0.65))
            return FusionResult(
                score_a=score_a,
                score_b=score_b,
                alpha=0.0,
                final_score=score_b,
                threshold=base_threshold,
                b_modifier=1.0,
                a_modifier=1.0,
                inertia_multiplier=1.0,
            )
