"""test_prompts.py —— A Prompt 模板构建器。

测试对象：core/prompts.py → 4 个纯函数
覆盖点：
- build_interest_prompt：含必要段、空 persona_knowledge 用「（无）」
- build_summary_prompt：含长/短窗口段
- build_reply_prompt：固定段在前、extra_context 空时跳过、style_hint 加尾段
- build_glance_reply_prompt：含人设、目标消息、30 字限制说明

对应 PRD 附录 A / 附录 B / F2 / F5。
"""

from __future__ import annotations

from core.common.prompts import (
    build_glance_reply_prompt,
    build_interest_prompt,
    build_reply_prompt,
    build_summary_prompt,
)


# ---------------------------------------------------------------------- #
# build_interest_prompt（附录 A）
# ---------------------------------------------------------------------- #

def test_prompts_interest_prompt_contains_required_segments():
    """含角色描述、补充知识、四级分类要求、JSON 格式说明。"""
    p = build_interest_prompt("测试人设", "测试知识")
    assert "角色描述" in p
    assert "测试人设" in p
    assert "补充知识" in p
    assert "测试知识" in p
    assert "core" in p and "general" in p and "marginal" in p and "hate" in p
    assert "JSON" in p
    assert "high_interest_keywords" in p


def test_prompts_interest_prompt_empty_knowledge_uses_placeholder():
    """空 persona_knowledge 用「（无）」占位，不输出 None。"""
    p = build_interest_prompt("人设", "")
    assert "（无）" in p
    assert "None" not in p


def test_prompts_interest_prompt_whitespace_knowledge_uses_placeholder():
    """仅空白字符的 persona_knowledge 也用占位。"""
    p = build_interest_prompt("人设", "   \n  ")
    assert "（无）" in p


def test_prompts_interest_prompt_contains_weight_examples():
    """含 weight / examples 字段示例。"""
    p = build_interest_prompt("人设", "")
    assert "weight" in p
    assert "examples" in p


# ---------------------------------------------------------------------- #
# build_summary_prompt（附录 B）
# ---------------------------------------------------------------------- #

def test_prompts_summary_prompt_contains_segments():
    p = build_summary_prompt("长窗口历史", "短窗口消息")
    assert "长窗口" in p
    assert "长窗口历史" in p
    assert "短窗口" in p
    assert "短窗口消息" in p
    assert "3~5 句话" in p


def test_prompts_summary_prompt_empty_inputs():
    """空输入不抛异常。"""
    p = build_summary_prompt("", "")
    assert isinstance(p, str)
    assert "长窗口" in p


# ---------------------------------------------------------------------- #
# build_reply_prompt（缓存友好结构）
# ---------------------------------------------------------------------- #

def test_prompts_reply_prompt_basic_segments():
    """含系统段、人设段、短窗口段、批次文本段。"""
    p = build_reply_prompt(
        persona_text="友善机器人",
        short_window="Alice: hi\nBob: hello",
        extra_context="",
        batch_text="最新批次",
    )
    assert "友善机器人" in p
    assert "Alice: hi" in p
    assert "最新批次" in p
    # 缓存友好：固定系统段在前
    assert "真实参与者" in p


def test_prompts_reply_prompt_extra_context_empty_skipped():
    """extra_context 为空时跳过「相关历史背景」段。"""
    p = build_reply_prompt("人设", "短窗口", "", "批次")
    assert "相关历史背景" not in p


def test_prompts_reply_prompt_extra_context_present():
    """extra_context 非空时包含「相关历史背景」段。"""
    p = build_reply_prompt("人设", "短窗口", "历史背景内容", "批次")
    assert "相关历史背景" in p
    assert "历史背景内容" in p


def test_prompts_reply_prompt_style_hint_appended_to_tail():
    """reply_style_hint 非空时附加到指令尾段。"""
    p = build_reply_prompt("人设", "短窗口", "", "批次", reply_style_hint="简短口语")
    assert "风格提示" in p
    assert "简短口语" in p


def test_prompts_reply_prompt_style_hint_empty_omitted():
    """reply_style_hint 为空时不附加。"""
    p = build_reply_prompt("人设", "短窗口", "", "批次", reply_style_hint="")
    assert "风格提示" not in p


def test_prompts_reply_prompt_cache_friendly_order():
    """固定段在前，变化段（batch_text）在尾部。验证 batch_text 出现位置晚于 persona。"""
    p = build_reply_prompt("PERSONA_X", "SHORT_Y", "", "BATCH_Z")
    idx_persona = p.find("PERSONA_X")
    idx_short = p.find("SHORT_Y")
    idx_batch = p.find("BATCH_Z")
    # persona < short < batch（缓存友好：固定前缀在变化尾段之前）
    assert idx_persona < idx_batch
    assert idx_short < idx_batch


# ---------------------------------------------------------------------- #
# build_glance_reply_prompt
# ---------------------------------------------------------------------- #

def test_prompts_glance_reply_prompt_contains_segments():
    p = build_glance_reply_prompt("人设描述", "目标消息内容")
    assert "人设描述" in p
    assert "目标消息内容" in p
    assert "30" in p  # 30 字限制


def test_prompts_glance_reply_prompt_empty_target():
    """空目标消息不抛异常。"""
    p = build_glance_reply_prompt("人设", "")
    assert isinstance(p, str)
