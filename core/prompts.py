"""Prompt 模板构建器（模块 A 产出）。

四个纯函数，纯字符串拼接，不 import astrbot / numpy。
- build_interest_prompt      : 对应 PRD 附录 A（启动时一次兴趣生成）
- build_summary_prompt       : 对应 PRD 附录 B（长窗口 LLM 摘要，可选）
- build_reply_prompt         : 主动回复，缓存友好结构（固定段在前，变化在尾部）
- build_glance_reply_prompt  : 瞥一眼简短插话（≤30 字）
"""

from __future__ import annotations


def build_interest_prompt(
    persona_text: str,
    persona_knowledge: str,
    example_count: int = 3,
    keyword_count: int = 12,
) -> str:
    """构建人设兴趣生成 Prompt（PRD 附录 A）。

    persona_text     : 人设自然语言描述
    persona_knowledge: 补充知识文档（为空时优雅处理，显示「（无）」）
    example_count    : 每个兴趣级别生成的示例句子数（默认 3）
    keyword_count    : 高唤醒关键词生成数量（默认 12）
    """
    # 补充知识为空时不输出 "None"，改用占位「（无）」
    knowledge_block = (
        persona_knowledge.strip()
        if persona_knowledge and persona_knowledge.strip()
        else "（无）"
    )

    return f"""你是一个角色设定分析师。请阅读以下角色描述，并生成一个 JSON 格式的兴趣列表。
角色描述：
{persona_text}

补充知识：
{knowledge_block}

要求：
1. 将兴趣分为四个级别：core（核心兴趣）、general（一般兴趣）、marginal（边缘兴趣）、hate（反感话题）。
2. 为每个兴趣生成 {example_count} 句示例对话，模拟该角色在群聊中可能说或听到的话。
3. 从 core 兴趣中提炼 {keyword_count} 个高唤醒关键词（词或短语）。
4. 仅输出 JSON，不要任何其他文字。

输出 JSON 格式：
{{
  "interests": [
    {{"label": "core", "topic": "星穹铁道配队", "examples": ["符玄怎么配队？", "量子队现版本还强吗？"], "weight": 1.5}},
    {{"label": "general", "topic": "...", "examples": ["..."], "weight": 1.0}},
    {{"label": "marginal", "topic": "...", "examples": ["..."], "weight": 0.6}},
    {{"label": "hate", "topic": "...", "examples": ["..."], "weight": 1.0}}
  ],
  "hate_keywords": ["..."],
  "high_interest_keywords": ["符玄", "穷观阵", "量子队", "银狼"]
}}"""


def build_summary_prompt(long_text: str, short_text: str) -> str:
    """构建长窗口总结 Prompt（PRD 附录 B，long_window_summarize=true 时使用）。

    long_text : 长窗口历史文本
    short_text: 短窗口最近几条消息文本
    """
    return f"""以下是群聊中的一段较长历史（长窗口）：
{long_text}

以下是当前正在讨论的最近几条消息（短窗口）：
{short_text}

请仅提取长窗口中与短窗口话题直接相关的信息，忽略无关闲聊。用 3~5 句话概括对当前讨论有帮助的历史要点。"""


def build_reply_prompt(
    persona_text: str,
    short_window: str,
    extra_context: str,
    batch_text: str,
    reply_style_hint: str = "",
) -> str:
    """构建主动回复 Prompt（缓存友好结构）。

    缓存友好 = 固定系统段 + 人设 + 短窗口（格式固定）+ 可选额外上下文 + 变化尾段。
    变化部分（batch_text / reply_style_hint）放在尾部，保护前缀缓存命中。

    persona_text     : 人设自然语言描述
    short_window     : 短窗口文本（已格式化为「昵称: 内容」逐行）
    extra_context    : 长窗口相关性内容（空则跳过，不输出该段）
    batch_text       : 当前批次文本（用于自然接话）
    reply_style_hint : 可选风格提示（非空时附加到指令尾段）
    """
    # 固定系统段：角色定位 + 自然接话风格要求（≤2 句）
    system_block = (
        "你是群聊里的一位真实参与者，根据当前对话自然地接一两句话。"
        "像真人一样简短、口语化，不要说「作为AI」，不要复读别人，不要超过两三句。"
    )

    # 人设段
    persona_block = f"你的人设：\n{persona_text}"

    # 短窗口段（格式已固定，缓存友好）
    short_block = f"最近对话：\n{short_window}"

    # 额外上下文段（长窗口相关性内容，空则跳过）
    parts = [system_block, persona_block, short_block]
    if extra_context and extra_context.strip():
        parts.append(f"相关历史背景：\n{extra_context}")

    # 指令尾段（变化部分）：基于批次文本自然接话
    tail = f"当前批次对话内容：\n{batch_text}\n\n请直接给出你的接话内容，不要任何前缀或解释。"
    if reply_style_hint and reply_style_hint.strip():
        tail += f"\n风格提示：{reply_style_hint}"

    parts.append(tail)
    return "\n\n".join(parts)


def build_glance_reply_prompt(persona_text: str, target_message: str) -> str:
    """构建瞥一眼简短插话 Prompt（≤30 字）。

    persona_text    : 人设自然语言描述
    target_message  : 目标消息文本（瞥眼命中后对其插话）
    """
    return f"""你的人设：\n{persona_text}

你正在群聊里路过，看到了这样一条消息：
{target_message}

请像路人随口一句一样简短地插句话。要求：
1. 不超过 30 个字。
2. 自然、不打断节奏，像真人随口一嘴。
3. 不要说「作为AI」，不要复读原消息，不要提问追话题。
4. 直接输出插话内容，不要任何前缀或解释。"""
