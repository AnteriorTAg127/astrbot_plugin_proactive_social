"""共享数据结构（模块 A 产出，其余模块只读引用）。

本文件仅定义纯数据结构（dataclass / Enum），不 import astrbot、不 import numpy，
保证 core/ 模块可离线单元测试。所有字段名、类型、默认值与「开发/v0.1/分工.md」
接口契约逐字一致，不得更改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GroupState(str, Enum):
    """群组状态机五态（每群独立维护）。

    IDLE：普通监听，无期待，标准阈值。
    EXPECTING_REPLY：发言后短时段内，阈值 ×0.8，启用跟踪列表与瞥一眼。
    COOLDOWN：冷却惩罚期，阈值大幅提升，仅 core 兴趣可突破。
    ACTIVE_MONITORING：当前轮询周期正在监听的群。
    GLANCING：瞥一眼任务短暂切入的其他群。
    """

    IDLE = "idle"
    EXPECTING_REPLY = "expecting_reply"
    COOLDOWN = "cooldown"
    ACTIVE_MONITORING = "active_monitoring"
    GLANCING = "glancing"


class InterestLevel(str, Enum):
    """兴趣分级四态（与 LLM 生成的 label 字段对齐）。"""

    CORE = "core"  # 核心兴趣，权重最高
    GENERAL = "general"  # 一般兴趣
    MARGINAL = "marginal"  # 边缘兴趣，权重最低
    HATE = "hate"  # 反感话题，触发屏蔽


@dataclass
class LogicalMessage:
    """缓冲区内的一条逻辑消息（可能由多条短消息拼接而成）。

    user_id   : 发言者 ID
    nickname  : 发言者昵称（用于窗口文本展示）
    text      : 消息文本（短消息已拼接为一条）
    ts        : epoch 秒
    group_id  : 所属群 ID
    is_wake   : (v0.2) 本条是否 @Bot/强唤醒（批次级 mentions_bot 判定用，默认 False 向后兼容）
    """

    user_id: str
    nickname: str
    text: str
    ts: float
    group_id: str
    is_wake: bool = False


@dataclass
class BatchRecord:
    """一次批处理记录：文本 + 嵌入 + 时间，供长窗口相关性选择复用。

    text       : 批次拼接文本
    embedding  : 批次文本嵌入向量
    ts         : 批次时间戳（epoch 秒）
    messages   : 该批次包含的逻辑消息列表（默认空）
    """

    text: str
    embedding: list[float]
    ts: float
    messages: list[LogicalMessage] = field(default_factory=list)


@dataclass
class InterestItem:
    """单条兴趣条目（对应 LLM 生成的 interests 数组中一项）。

    level    : 兴趣级别
    topic    : 兴趣主题描述
    examples : 示例对话句列表
    weight   : 该级别权重（core 1.5 / general 1.0 / marginal 0.6）
    """

    level: InterestLevel
    topic: str
    examples: list[str]
    weight: float


@dataclass
class InterestData:
    """人设兴趣数据（启动时生成一次，持久化为 interests.npz）。

    centroids             : level.value -> 质心向量（按级别分组对示例嵌入求均值）
    weights               : level.value -> 权重（core 1.5 等，用于 s_int 加权）
    high_interest_keywords: 核心兴趣关键词表（规则降级 / 瞥一眼关键词匹配用）
    hate_keywords         : 反感关键词表
    items                 : 原始兴趣条目列表（供 Dashboard / persona show 展示）
    persona_hash          : 生成时的人设文本哈希，用于判断是否需要重建
    dim                   : 嵌入维度（一致性校验用）
    """

    centroids: dict[str, list[float]]
    weights: dict[str, float]
    high_interest_keywords: list[str]
    hate_keywords: list[str]
    items: list[InterestItem]
    persona_hash: str
    dim: int


@dataclass
class ScoreFactors:
    """五因子得分（每次批次决策的中间量，进决策日志）。

    s_int      : 兴趣得分（batch_emb 与各级质心加权余弦最大值，cap 1.5）
    s_topic    : 话题连贯性（batch_emb 与短窗口均值嵌入的余弦）
    s_resp     : 回应期待（EXPECTING 时 batch_emb 与机器人最后发言嵌入的余弦，否则 0）
    c_cooldown : 冷却惩罚（本群最近 N 条中机器人发言占比衰减，0~1）
    p_silence  : 沉默奖励（距上一条消息秒数归一化 min(sec/300, 1)）
    """

    s_int: float
    s_topic: float
    s_resp: float
    c_cooldown: float
    p_silence: float


@dataclass
class BatchDecision:
    """一次批次决策的完整记录（进决策日志 / Dashboard）。

    ts                : 决策时间戳（epoch 秒）
    group_id          : 群 ID
    batch_summary     : 批次文本截断（≤80 字，供展示）
    factors           : 五因子得分
    score             : 融合总分
    threshold         : 本批动态阈值
    hit_level         : 命中兴趣级别 "core"/"general"/"marginal"/"none"/"hate"
    triggered         : 是否触发主动回复（已扣减抑制因素后的最终判定）
    suppressed_reason : 抑制原因 "" / "hate" / "cooldown" / "below_threshold" / "dry_run" / "disabled" / "fatigue"
    dry_run           : 本批是否处于 DRY_RUN 模式
    message_count     : 本批消息条数
    score_a           : (v0.2) 通道 A 规则归一化分 [0,1]
    score_b           : (v0.2) 通道 B 向量融合原始分
    alpha             : (v0.2) 融合权重 α
    fatigue_level     : (v0.2) 决策时全局疲劳级别 "none"/"low"/"medium"/"high"
    fatigue_value     : (v0.2) 决策时全局疲劳值
    channel           : (v0.2) 决策通道 "vector"/"rule"/"fusion"
    keyword_match_score : (v0.2.5) 回复关键词匹配得分 [0,1]
    keyword_added_score : (v0.2.5) 回复关键词加分（match_score × boost_factor）
    """

    ts: float
    group_id: str
    batch_summary: str
    factors: ScoreFactors
    score: float
    threshold: float
    hit_level: str
    triggered: bool
    suppressed_reason: str
    dry_run: bool
    message_count: int
    # --- v0.2 双通道融合增量字段（带默认值，向后兼容 v0.1 持久化日志）---
    score_a: float = 0.0  # 通道 A（规则）归一化分 [0,1]
    score_b: float = 0.0  # 通道 B（向量）融合原始分
    alpha: float = 0.0  # 融合权重 α（通道 A 权重）
    fatigue_level: str = "none"  # 决策时全局疲劳级别
    fatigue_value: float = 0.0  # 决策时全局疲劳值
    channel: str = "vector"  # "vector"/"rule"/"fusion"
    # --- v0.2.5 回复关键词增量字段（带默认值，向后兼容 v0.2 持久化日志）---
    keyword_match_score: float = 0.0  # 回复关键词匹配得分 [0,1]
    keyword_added_score: float = 0.0  # 回复关键词加分（match × boost）


@dataclass
class TrackerEntry:
    """个人跟踪列表条目（F4 无 @ 接话）。

    user_id            : 被跟踪用户 ID
    nickname           : 被跟踪用户昵称
    bot_last_emb       : 机器人最后发言嵌入（计算 s_resp 用）
    last_own_text      : 该用户上一条消息文本（用于拼接缓解短回复语义稀薄）
    created_ts         : 跟踪建立时间（epoch 秒）
    irrelevant_streak  : 连续不相关消息计数（达 track_irrelevant_msgs 则移除）
    """

    user_id: str
    nickname: str
    bot_last_emb: list[float]
    last_own_text: str
    created_ts: float
    irrelevant_streak: int = 0


@dataclass
class RuleSignal:
    """通道 A 规则引擎评估结果（v0.2，PRD F9）。

    score_a         : 归一化分 [0,1]（raw_score / normalize，clamp）
    raw_score       : 原始整数分（已扣除规则内疲劳惩罚）
    hit_type        : 命中类型 "direct"/"context"/"question"/"interest"/"none"
    matched_word    : 命中的关键词（强唤醒词/语境词/兴趣词），无则 ""
    mentions_bot    : 是否 @Bot 或含强唤醒词
    is_question     : 是否命中疑问信号
    suppressed      : 规则内部判定不回复（屏蔽短语 / 无任何信号）
    suppress_reason : "" / "block_phrase" / "no_signal"
    fatigue_level   : 评估时的全局疲劳级别
    """

    score_a: float
    raw_score: int
    hit_type: str
    matched_word: str
    mentions_bot: bool
    is_question: bool
    suppressed: bool
    suppress_reason: str
    fatigue_level: str


@dataclass
class FusionResult:
    """双通道融合结果（v0.2，PRD F10）。

    score_a            : 通道 A 归一化分 [0,1]
    score_b            : 通道 B 向量融合原始分
    alpha              : 融合权重 α（通道 A 权重）
    final_score        : α·score_a + (1−α)·score_b
    threshold          : 融合阈值（base × B修正 × A修正 × 期待修正 × 惯性修正）
    b_modifier         : 级别修正（core/general/marginal）
    a_modifier         : 疲劳修正（high/medium）
    inertia_multiplier : 惯性阈值倍率（≤1）
    """

    score_a: float
    score_b: float
    alpha: float
    final_score: float
    threshold: float
    b_modifier: float
    a_modifier: float
    inertia_multiplier: float
