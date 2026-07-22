"""test_engine.py —— D 唤醒决策引擎（纯计算，最易测，覆盖最全）。

测试对象：core/engine.py → WakeEngine
覆盖点：
- cosine：基本余弦、零向量、长度不一致、空列表（边界）
- interest_score：core/general/marginal 命中、cap 1.5、interest=None、无质心
- hate_score：有/无 hate 质心、interest=None
- evaluate：五因子公式、expecting 时 s_resp、动态阈值（级别修正 + 期待修正）
- rule_fallback：关键词命中 + 沉默阈值、无关键词、沉默不足、interest=None

对应 PRD §8.1（兴趣分级）、§8.5（反感屏蔽）、§8.6（降级）。
"""

from __future__ import annotations

import pytest

from core.engine import WakeEngine
from core.models import InterestData, InterestItem, InterestLevel


# ---------------------------------------------------------------------- #
# cosine
# ---------------------------------------------------------------------- #

def test_engine_cosine_basic():
    """相同向量余弦=1，正交向量=0。"""
    assert WakeEngine.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert WakeEngine.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_engine_cosine_zero_vector_returns_zero():
    """零向量不抛异常，返回 0.0（PRD §8 边界）。"""
    assert WakeEngine.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert WakeEngine.cosine([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_engine_cosine_mismatched_length_returns_zero():
    """长度不一致返回 0.0，不抛异常。"""
    assert WakeEngine.cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0


def test_engine_cosine_empty_list_returns_zero():
    """空列表返回 0.0。"""
    assert WakeEngine.cosine([], []) == 0.0
    assert WakeEngine.cosine([], [1.0]) == 0.0


def test_engine_cosine_negative_direction():
    """反方向余弦=-1。"""
    assert WakeEngine.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


# ---------------------------------------------------------------------- #
# interest_score
# ---------------------------------------------------------------------- #

def _make_interest(
    centroids: dict[str, list[float]],
    weights: dict[str, float] | None = None,
    high_kw: list[str] | None = None,
) -> InterestData:
    return InterestData(
        centroids=centroids,
        weights=weights or {"core": 1.5, "general": 1.0, "marginal": 0.6, "hate": 1.0},
        high_interest_keywords=high_kw or [],
        hate_keywords=[],
        items=[],
        persona_hash="t",
        dim=4,
    )


def test_engine_interest_score_core_cap_15():
    """core 质心命中，加权 1.0*1.5=1.5，cap 到 1.5；hit_level='core'。对应 §8.1。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert score == pytest.approx(1.5)
    assert level == "core"


def test_engine_interest_score_general():
    """仅 general 命中，加权 1.0*1.0=1.0；hit_level='general'。"""
    interest = _make_interest(
        {"general": [1.0, 0.0, 0.0, 0.0], "core": [0.0, 1.0, 0.0, 0.0]}
    )
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert score == pytest.approx(1.0)
    assert level == "general"


def test_engine_interest_score_marginal_lower_weight():
    """marginal 命中，加权 1.0*0.6=0.6；hit_level='marginal'。"""
    interest = _make_interest({"marginal": [1.0, 0.0, 0.0, 0.0]})
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert score == pytest.approx(0.6)
    assert level == "marginal"


def test_engine_interest_score_picks_max_across_levels():
    """多级别命中时取加权最大值（core 1.5 > general 1.0）。"""
    interest = _make_interest(
        {"core": [1.0, 0.0, 0.0, 0.0], "general": [0.9, 0.4, 0.0, 0.0]}
    )
    # batch_emb 与 core 完全相同 → 1.5；与 general 相似 → <1.0
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert level == "core"
    assert score == pytest.approx(1.5)


def test_engine_interest_score_none_interest():
    """interest=None → (0.0, 'none')。"""
    score, level = WakeEngine.interest_score([1.0, 0.0], None)
    assert score == 0.0
    assert level == "none"


def test_engine_interest_score_no_centroids():
    """无任何有效级别质心 → (0.0, 'none')。"""
    interest = _make_interest({})
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert score == 0.0
    assert level == "none"


def test_engine_interest_score_hate_not_participating():
    """hate 质心不参与 interest_score（走 hate_score）。"""
    interest = _make_interest({"hate": [1.0, 0.0, 0.0, 0.0]})
    score, level = WakeEngine.interest_score([1.0, 0.0, 0.0, 0.0], interest)
    assert score == 0.0
    assert level == "none"


# ---------------------------------------------------------------------- #
# hate_score
# ---------------------------------------------------------------------- #

def test_engine_hate_score_present():
    """有 hate 质心 → 返回余弦相似度。对应 §8.5。"""
    interest = _make_interest({"hate": [1.0, 0.0, 0.0, 0.0]})
    assert WakeEngine.hate_score([1.0, 0.0, 0.0, 0.0], interest) == pytest.approx(1.0)


def test_engine_hate_score_no_centroid():
    """无 hate 质心 → 0.0。"""
    interest = _make_interest({})
    assert WakeEngine.hate_score([1.0, 0.0, 0.0, 0.0], interest) == 0.0


def test_engine_hate_score_none_interest():
    """interest=None → 0.0。"""
    assert WakeEngine.hate_score([1.0, 0.0], None) == 0.0


# ---------------------------------------------------------------------- #
# evaluate
# ---------------------------------------------------------------------- #

def _default_weights():
    return {
        "w_int": 1.0,
        "w_topic": 0.4,
        "w_resp": 0.8,
        "w_cooldown": 0.5,
        "w_silence": 0.2,
    }


def _default_modifiers():
    return {"core": 0.7, "general": 1.0, "marginal": 1.3, "expecting": 0.8}


def test_engine_evaluate_full_formula_core_hit():
    """core 命中 + 完整五因子：验证总分公式与阈值（base*core_mod）。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    factors, score, threshold, hit_level = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=[1.0, 0.0, 0.0, 0.0],
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=60.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    # s_int=1.5(cap), s_topic=1.0, s_resp=0(not expecting), c_cooldown=0, p_silence=60/300=0.2
    # score = 1.0*1.5 + 0.4*1.0 + 0.8*0 - 0.5*0 + 0.2*0.2 = 1.5+0.4+0.04 = 1.94
    assert factors.s_int == pytest.approx(1.5)
    assert factors.s_topic == pytest.approx(1.0)
    assert factors.s_resp == 0.0
    assert factors.c_cooldown == 0.0
    assert factors.p_silence == pytest.approx(0.2)
    assert score == pytest.approx(1.94)
    # threshold = 0.65 * 0.7(core) * 1(not expecting) = 0.455
    assert threshold == pytest.approx(0.455)
    assert hit_level == "core"


def test_engine_evaluate_expecting_enables_s_resp():
    """expecting=True 时 s_resp 参与计算；False 时 s_resp=0。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    # expecting=True, bot_last_emb 与 batch_emb 相同 → s_resp=1.0
    factors, score, _, _ = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=[1.0, 0.0, 0.0, 0.0],
        expecting=True,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert factors.s_resp == pytest.approx(1.0)
    # score 含 w_resp*s_resp = 0.8*1.0
    # s_int=1.5, s_topic=0(topic None), c_cooldown=0, p_silence=0
    # score = 1.0*1.5 + 0 + 0.8*1.0 - 0 + 0 = 2.3
    assert score == pytest.approx(2.3)


def test_engine_evaluate_expecting_false_disables_s_resp():
    """expecting=False 时即使 bot_last_emb 提供也 s_resp=0。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    factors, _, _, _ = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=[1.0, 0.0, 0.0, 0.0],
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert factors.s_resp == 0.0


def test_engine_evaluate_threshold_general_modifier():
    """general 命中 → threshold = base * 1.0 * 1 = 0.65。"""
    interest = _make_interest({"general": [1.0, 0.0, 0.0, 0.0]})
    _, _, threshold, hit_level = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert hit_level == "general"
    assert threshold == pytest.approx(0.65)


def test_engine_evaluate_threshold_marginal_modifier_higher():
    """marginal 命中 → threshold = base * 1.3 = 0.845（边缘阈值更高）。"""
    interest = _make_interest({"marginal": [1.0, 0.0, 0.0, 0.0]})
    _, _, threshold, hit_level = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert hit_level == "marginal"
    assert threshold == pytest.approx(0.845)


def test_engine_evaluate_threshold_expecting_modifier():
    """expecting=True → threshold 再 ×0.8（core 命中）。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    _, _, threshold, _ = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=None,
        expecting=True,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    # 0.65 * 0.7(core) * 0.8(expecting) = 0.364
    assert threshold == pytest.approx(0.364)


def test_engine_evaluate_cooldown_penalty_subtracts():
    """c_cooldown > 0 时从总分中扣减 w_cooldown*c_cooldown。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    factors, score, _, _ = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=1.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    # score = 1.0*1.5 + 0 + 0 - 0.5*1.0 + 0 = 1.0
    assert factors.c_cooldown == 1.0
    assert score == pytest.approx(1.0)


def test_engine_evaluate_silence_capped_at_1():
    """silence_sec > 300 时 p_silence = 1.0（不溢出）。"""
    interest = _make_interest({"core": [1.0, 0.0, 0.0, 0.0]})
    factors, _, _, _ = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=interest,
        topic_emb=None,
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=1000.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert factors.p_silence == 1.0


def test_engine_evaluate_none_interest():
    """interest=None → s_int=0, hit_level='none', level_mod=1.0。"""
    factors, score, threshold, hit_level = WakeEngine.evaluate(
        batch_emb=[1.0, 0.0, 0.0, 0.0],
        interest=None,
        topic_emb=[1.0, 0.0, 0.0, 0.0],
        bot_last_emb=None,
        expecting=False,
        cooldown_ratio=0.0,
        silence_sec=0.0,
        weights=_default_weights(),
        base_threshold=0.65,
        modifiers=_default_modifiers(),
    )
    assert factors.s_int == 0.0
    assert hit_level == "none"
    # threshold = 0.65 * 1.0(none) * 1.0 = 0.65
    assert threshold == pytest.approx(0.65)


# ---------------------------------------------------------------------- #
# rule_fallback
# ---------------------------------------------------------------------- #

def test_engine_rule_fallback_hit_keyword_and_silence():
    """命中关键词 + 沉默超阈值 → True。对应 §8.6 降级。"""
    interest = _make_interest({}, high_kw=["符玄"])
    assert WakeEngine.rule_fallback("符玄怎么配队", interest, 200.0) is True


def test_engine_rule_fallback_no_keyword():
    """无关键词命中 → False。"""
    interest = _make_interest({}, high_kw=["符玄"])
    assert WakeEngine.rule_fallback("今天天气不错", interest, 200.0) is False


def test_engine_rule_fallback_short_silence():
    """沉默不足阈值 → False。"""
    interest = _make_interest({}, high_kw=["符玄"])
    assert WakeEngine.rule_fallback("符玄配队", interest, 100.0) is False


def test_engine_rule_fallback_none_interest():
    """interest=None → False。"""
    assert WakeEngine.rule_fallback("符玄", None, 200.0) is False


def test_engine_rule_fallback_empty_text():
    """空文本 → False。"""
    interest = _make_interest({}, high_kw=["符玄"])
    assert WakeEngine.rule_fallback("", interest, 200.0) is False


def test_engine_rule_fallback_empty_keywords():
    """关键词表为空 → False。"""
    interest = _make_interest({}, high_kw=[])
    assert WakeEngine.rule_fallback("符玄", interest, 200.0) is False


# ---------------------------------------------------------------------- #
# models.py 最小实例化测试（dataclass 契约）
# ---------------------------------------------------------------------- #

def test_models_dataclass_instantiation():
    """models.py 各 dataclass 可正常实例化（字段契约验证）。"""
    from core.models import (
        BatchDecision,
        BatchRecord,
        LogicalMessage,
        ScoreFactors,
        TrackerEntry,
    )

    lm = LogicalMessage("u1", "Alice", "hi", 1.0, "g1")
    assert lm.user_id == "u1" and lm.text == "hi"

    br = BatchRecord(text="t", embedding=[0.1], ts=1.0)
    assert br.messages == []  # default_factory

    sf = ScoreFactors(1.0, 0.5, 0.0, 0.2, 0.1)
    assert sf.s_int == 1.0

    bd = BatchDecision(
        ts=1.0, group_id="g1", batch_summary="s", factors=sf,
        score=0.5, threshold=0.65, hit_level="core",
        triggered=True, suppressed_reason="", dry_run=False, message_count=1,
    )
    assert bd.triggered is True

    te = TrackerEntry("u1", "Alice", [0.1], "hi", 1.0)
    assert te.irrelevant_streak == 0  # default

    # 枚举值与 PRD 契约一致
    assert GroupState_IDENTITY()  # 占位，下方独立验证


def GroupState_IDENTITY():
    from core.models import GroupState, InterestLevel
    assert GroupState.IDLE.value == "idle"
    assert GroupState.EXPECTING_REPLY.value == "expecting_reply"
    assert InterestLevel.CORE.value == "core"
    return True
