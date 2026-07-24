"""对话状态模块（v0.3.5 F6）。

纯启发式判定群聊对话状态，输出阈值修正倍率应用到 eff_threshold，
降低机械感。不增加 LLM/embedding 调用（零成本）。

判定维度：
- has_question：最近 N 条消息中任一含 ?？ 且长度 > 5
- is_monologue：最近 N 条中同一 user_id 占比 ≥ monologue_ratio
- is_argument：最近 N 条 ≥ 2 个不同用户交替 + 平均长度 > argument_msg_len + 标点占比 > 0.5
- is_casual_chat：多用户 + 平均长度 < 12 + 平均间隔 < 5s
- bot_turn：最近一条 is_wake=True 或 bot 发言后 ≤ 5s 无人回应

appropriateness 综合计算（clamp [0,1]）：
- has_question +0.3
- bot_turn +0.3
- is_casual_chat +0.2
- is_monologue -0.3
- is_argument -0.5

modifier = 1.0 + (0.5 - appropriateness) * 0.6
- appropriateness=1.0 → modifier=0.7（放宽阈值，更易触发）
- appropriateness=0.0 → modifier=1.3（收紧阈值，不易触发）
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.models import LogicalMessage


@dataclass
class ConversationState:
    """对话状态评估结果。"""

    has_question: bool
    is_monologue: bool
    is_argument: bool
    is_casual_chat: bool
    bot_turn: bool
    appropriateness: float  # [0,1] 插话适宜度
    modifier: float  # 阈值修正倍率（1.0=无影响，<1.0 放宽，>1.0 收紧）


class ConversationStateEvaluator:
    """对话状态评估器（纯启发式，无 I/O）。"""

    @staticmethod
    def evaluate(
        msgs: list[LogicalMessage],
        bot_user_id: str,
        cfg: dict,
        now: float,
    ) -> ConversationState:
        """评估最近消息的对话状态。

        Args:
            msgs: 最近 N 条消息（按时间顺序，最近在末尾）
            bot_user_id: 机器人 user_id（用于 bot_turn 判定，GroupContext 用 "__bot__"）
            cfg: 配置 dict（读 conversation_state_* 键）
            now: 当前时间戳（epoch 秒）

        Returns:
            ConversationState
        """
        try:
            window = int(cfg.get("conversation_state_window", 10))
            monologue_ratio = float(cfg.get("conversation_state_monologue_ratio", 0.6))
            argument_msg_len = int(cfg.get("conversation_state_argument_msg_len", 20))

            # 取最近 window 条消息做判定
            recent = msgs[-window:] if window > 0 else list(msgs)
            if not recent:
                return ConversationState(
                    has_question=False,
                    is_monologue=False,
                    is_argument=False,
                    is_casual_chat=False,
                    bot_turn=False,
                    appropriateness=0.5,
                    modifier=1.0,
                )

            # 非机器人消息
            non_bot = [m for m in recent if m.user_id != bot_user_id]

            # has_question：任一消息含 ? 或 ？ 且 len(text) > 5
            has_question = any(
                ("?" in m.text or "？" in m.text) and len(m.text) > 5 for m in recent
            )

            # is_monologue：同一 user_id（排除 bot）占比 ≥ monologue_ratio
            is_monologue = False
            if non_bot:
                user_counts: dict[str, int] = {}
                for m in non_bot:
                    user_counts[m.user_id] = user_counts.get(m.user_id, 0) + 1
                max_ratio = max(user_counts.values()) / len(non_bot)
                is_monologue = max_ratio >= monologue_ratio

            # 不同非 bot 用户数
            distinct_users = {m.user_id for m in non_bot}
            multi_user = len(distinct_users) >= 2

            # is_argument：≥ 2 个不同非 bot 用户 + 平均长度 > argument_msg_len + 标点占比 > 0.5
            is_argument = False
            if multi_user and non_bot:
                avg_len = sum(len(m.text) for m in non_bot) / len(non_bot)
                all_text = "".join(m.text for m in non_bot)
                total_chars = len(all_text)
                punct_count = sum(1 for c in all_text if c in "!？！?")
                punct_ratio = punct_count / total_chars if total_chars > 0 else 0.0
                is_argument = avg_len > argument_msg_len and punct_ratio > 0.5

            # is_casual_chat：≥ 2 个不同非 bot 用户 + 平均长度 < 12 + 平均间隔 < 5s
            is_casual_chat = False
            if multi_user and len(recent) >= 2:
                avg_len = sum(len(m.text) for m in recent) / len(recent)
                intervals = [
                    recent[i].ts - recent[i - 1].ts for i in range(1, len(recent))
                ]
                avg_interval = sum(intervals) / len(intervals) if intervals else 0.0
                is_casual_chat = avg_len < 12 and avg_interval < 5.0

            # bot_turn：最后一条 is_wake=True，或最后一条是 bot 发言且 (now - last_ts) ≤ 5.0
            last_msg = recent[-1]
            bot_turn = False
            if last_msg.is_wake:
                bot_turn = True
            elif last_msg.user_id == bot_user_id:
                bot_turn = (now - last_msg.ts) <= 5.0

            # appropriateness 综合计算（clamp [0.0, 1.0]）
            appropriateness = 0.0
            if has_question:
                appropriateness += 0.3
            if bot_turn:
                appropriateness += 0.3
            if is_casual_chat:
                appropriateness += 0.2
            if is_monologue:
                appropriateness -= 0.3
            if is_argument:
                appropriateness -= 0.5
            appropriateness = max(0.0, min(1.0, appropriateness))

            # modifier = 1.0 + (0.5 - appropriateness) * 0.6
            modifier = 1.0 + (0.5 - appropriateness) * 0.6

            return ConversationState(
                has_question=has_question,
                is_monologue=is_monologue,
                is_argument=is_argument,
                is_casual_chat=is_casual_chat,
                bot_turn=bot_turn,
                appropriateness=appropriateness,
                modifier=modifier,
            )
        except Exception:
            return ConversationState(
                has_question=False,
                is_monologue=False,
                is_argument=False,
                is_casual_chat=False,
                bot_turn=False,
                appropriateness=0.5,
                modifier=1.0,
            )
