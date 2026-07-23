"""通道 A 规则引擎（v0.2，PRD F9）。

独立重写自姊妹插件 private_companion 的隐式回复评分与疑问信号语义，
纯函数、无 I/O、无 worldbook、离线可测。仅依赖标准库 re 与 .models 中的 RuleSignal。
"""

from __future__ import annotations

import re
from typing import Any

from ..common.models import RuleSignal


def implicit_reply_score(
    text: str,
    *,
    matched_word: str = "",
    relation_hit: bool = False,
    mentions_bot: bool = False,
) -> int:
    """移植 private_companion._group_implicit_reply_score 的正则评分组，返回原始整数分（可叠加）。

    各评分组命中即加，text 为空返回 0：
      mentions_bot                               -> +70
      matched_word 非空                          -> +30
      relation_hit                               -> +22
      re.search(r"(你|bot).{0,12}(觉得|看|说|认为|怎么)", text, re.I) -> +42
      re.search(r"(你觉得|你看|你说|你认为)", text)   -> +40
      re.search(r"(然后呢|后来呢|接着呢|怎么办|接下来)", text) -> +32
      re.search(r"(对吧|是吧|好吗|行吗|可以吗|对不对)", text) -> +28
      re.search(r"(吗|呢|？|\\?)", text)            -> +20
      re.search(r"(帮|求|解释|怎么|如何|为什么|啥意思)", text) -> +18
      len(text) <= 12 且 matched_word 非空      -> +18

    relation_hit 恒由调用方传 False（Worldbook 未实现）。
    """
    if not text:
        return 0

    score = 0

    if mentions_bot:
        score += 70

    if matched_word:
        score += 30

    if relation_hit:
        score += 22

    # 称呼 Bot + 观点词（允许中间 0~12 字符）
    if re.search(r"(你|bot).{0,12}(觉得|看|说|认为|怎么)", text, re.I):
        score += 42

    # 直接询问 Bot 观点
    if re.search(r"(你觉得|你看|你说|你认为)", text):
        score += 40

    # 追问式衔接
    if re.search(r"(然后呢|后来呢|接着呢|怎么办|接下来)", text):
        score += 32

    # 句末征求意见
    if re.search(r"(对吧|是吧|好吗|行吗|可以吗|对不对)", text):
        score += 28

    # 疑问语气
    if re.search(r"(吗|呢|？|\?)", text):
        score += 20

    # 求助/解释类词
    if re.search(r"(帮|求|解释|怎么|如何|为什么|啥意思)", text):
        score += 18

    # 短句 + 命中关键词加成
    if len(text) <= 12 and matched_word:
        score += 18

    return score


# ---- question_signal 强模式定义（按优先级排序，命中取最高分，不叠加） ----
# 每条：(pattern, score, help_type, reason)
_QUESTION_PATTERNS: list[tuple[str, int, str, str]] = [
    (r"(有没有人|谁能|谁会|求助|求推荐|求问)", 80, "open_help", "公开求助"),
    (r"(报错|错误|异常|崩溃|卡死|怎么办|解决)", 78, "troubleshoot", "排障求助"),
    (r"(为什么|怎么理解|什么意思|啥意思|如何)", 72, "explain", "解释说明"),
    (r"(这是什么|这是啥|哪个|哪种|是什么)", 70, "identify", "识别确认"),
    (r"(怎么样|好不好|值不值|推荐吗|如何评价)", 68, "evaluation", "评价询问"),
]


def question_signal(text: str) -> dict[str, Any]:
    """精简移植 _group_wakeup_question_signal 强模式。

    非疑问或 text 为空返回 {}；否则返回:
        {'score': int, 'reason': str, 'help_type': str}

    强模式按优先级顺序判断，命中取**最高分那一档**（不叠加）：
      公开求助 -> 80, 'open_help'
      排障     -> 78, 'troubleshoot'
      解释     -> 72, 'explain'
      识别     -> 70, 'identify'
      评价     -> 68, 'evaluation'
      句末疑问 -> 55, 'yesno'（len(text) <= 40 且以吗/呢/？/? 结尾）
    """
    if not text:
        return {}

    # 强模式：按优先级顺序，命中即返回（取最高分）
    for pattern, score, help_type, reason in _QUESTION_PATTERNS:
        if re.search(pattern, text):
            return {"score": score, "reason": reason, "help_type": help_type}

    # 句末疑问（仅短文本，以疑问词结尾）
    if len(text) <= 40 and re.search(r"(吗|呢|？|\?)\s*$", text):
        return {"score": 55, "reason": "句末疑问", "help_type": "yesno"}

    return {}


# 屏蔽短语正则（命中则直接抑制，不回复）
_BLOCK_PATTERN = re.compile(r"(别回|不要回|不用回|不是叫你|不是问你|别理|不要理)")


def _find_matched_word(
    text: str,
    direct_words: list[str],
    interest_words: list[str],
    context_words: list[str],
) -> tuple[str, str]:
    """在 text 中按优先级查找首个命中的关键词。

    优先级：direct_words > interest_words > context_words。

    Returns:
        (matched_word, category) — category 为 'direct'/'interest'/'context'/''
    """
    # 优先强唤醒词
    for kw in direct_words:
        if kw and kw in text:
            return (kw, "direct")

    # 次优先兴趣词
    for kw in interest_words:
        if kw and kw in text:
            return (kw, "interest")

    # 再次语境词
    for kw in context_words:
        if kw and kw in text:
            return (kw, "context")

    return ("", "")


class RuleEngine:
    """通道 A 规则引擎：纯静态方法，无状态、无 I/O、离线可测。"""

    @staticmethod
    def evaluate(
        *,
        text: str,
        mentions_bot: bool,
        high_interest_keywords: list[str],
        rule_fatigue_level: str,
        config: dict,
    ) -> RuleSignal:
        """主评估，严格按 PRD F9 流程：

        1. text 为空/非 str -> 返回 score_a=0、suppressed=True、suppress_reason='no_signal'
        2. 屏蔽短语命中 -> 直接抑制（block_phrase）
        3. 归一化除数 normalize = max(config['rule_score_normalize'], 1.0)
        4. matched_word 计算：direct > interest > context 优先级
        5. raw = implicit_reply_score(...) 并扣除规则内疲劳分
        6. hit_type 与 is_question 判定（direct > question > context > interest > none）
        7. score_a = clamp(raw/normalize, 0, 1)；no_signal 时抑制
        8. 返回 RuleSignal

        任何意外异常 -> 兜底返回 score_a=0、suppressed=True、suppress_reason='no_signal'
        """
        try:
            # 步骤 1：输入校验
            if not text or not isinstance(text, str):
                return RuleSignal(
                    score_a=0.0,
                    raw_score=0,
                    hit_type="none",
                    matched_word="",
                    mentions_bot=mentions_bot,
                    is_question=False,
                    suppressed=True,
                    suppress_reason="no_signal",
                    fatigue_level=rule_fatigue_level,
                )

            # 步骤 2：屏蔽短语
            if _BLOCK_PATTERN.search(text):
                return RuleSignal(
                    score_a=0.0,
                    raw_score=0,
                    hit_type="none",
                    matched_word="",
                    mentions_bot=mentions_bot,
                    is_question=False,
                    suppressed=True,
                    suppress_reason="block_phrase",
                    fatigue_level=rule_fatigue_level,
                )

            # 步骤 3：归一化除数
            normalize = max(float(config.get("rule_score_normalize", 100.0)), 1.0)

            # 步骤 4：matched_word 计算
            direct_words = list(config.get("rule_direct_wakeup_words", []) or [])
            context_words = list(config.get("rule_context_wakeup_words", []) or [])
            interest_words = list(high_interest_keywords or [])

            matched_word, matched_category = _find_matched_word(
                text, direct_words, interest_words, context_words
            )

            # 步骤 5：隐式评分 + 规则内疲劳扣除
            raw = implicit_reply_score(
                text,
                matched_word=matched_word,
                relation_hit=False,
                mentions_bot=mentions_bot,
            )

            if rule_fatigue_level == "high":
                raw -= 22
            elif rule_fatigue_level == "medium":
                raw -= 10
            raw = max(raw, 0)

            # 步骤 6：is_question 与 hit_type
            question_enabled = config.get("rule_question_enabled", True)
            question_threshold = int(config.get("rule_question_threshold", 65))
            context_threshold = int(config.get("rule_context_threshold", 50))

            qs: dict[str, Any] = {}
            if question_enabled:
                qs = question_signal(text)
            q_hit = bool(qs) and int(qs.get("score", 0)) >= question_threshold

            # 优先级：direct > question > context > interest > none
            if mentions_bot or matched_category == "direct":
                hit_type = "direct"
            elif q_hit:
                hit_type = "question"
            elif matched_category == "context" and raw >= context_threshold:
                hit_type = "context"
            elif matched_category == "interest":
                hit_type = "interest"
            else:
                hit_type = "none"

            is_question = q_hit

            # 步骤 7：归一化分与抑制
            score_a = max(0.0, min(raw / normalize, 1.0))

            if hit_type == "none" and not mentions_bot:
                suppressed = True
                suppress_reason = "no_signal"
                score_a = 0.0
            else:
                suppressed = False
                suppress_reason = ""

            # 步骤 8：返回
            return RuleSignal(
                score_a=score_a,
                raw_score=raw,
                hit_type=hit_type,
                matched_word=matched_word,
                mentions_bot=mentions_bot,
                is_question=is_question,
                suppressed=suppressed,
                suppress_reason=suppress_reason,
                fatigue_level=rule_fatigue_level,
            )

        except Exception:
            # 兜底：任何意外异常不崩溃，返回无信号抑制
            return RuleSignal(
                score_a=0.0,
                raw_score=0,
                hit_type="none",
                matched_word="",
                mentions_bot=mentions_bot,
                is_question=False,
                suppressed=True,
                suppress_reason="no_signal",
                fatigue_level=rule_fatigue_level,
            )
