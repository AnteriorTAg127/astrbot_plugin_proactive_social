"""LLM 自动调参统计与触发（AutotuneStatsMixin，对应 PRD F3/v0.2.8/v0.2.9）。

将 scheduler.py 的调参统计收集、自适应阈值摘要、配置快照、自动触发与事件日志
5 个方法拆出为独立 Mixin，经多继承注入 SocialScheduler。

设计要点：
- **不定义 __init__**：依赖 SocialScheduler 经 MRO 提供的实例属性与方法。
- **不 import astrbot**：日志经 SocialScheduler 注入的 log_fn 获得。
- collect_tune_stats 汇总最近 200 条决策供 LLM 诊断调参；空日志返回全默认值。
- _maybe_autotune 在触发率越界时后台触发 autotune_trigger_fn，速率限制由 main.py 判断。
"""

from __future__ import annotations

import asyncio
import statistics

from ..decision.adaptive import AdaptiveThreshold


class AutotuneStatsMixin:
    """LLM 自动调参统计与触发 Mixin。

    依赖 SocialScheduler 经 MRO 提供的实例属性与方法：
    - ``self._decision_log``：DecisionLog 实例（recent(n) 取最近决策）。
    - ``self._groups``：每群运行时状态 dict（含 "adaptive" AdaptiveThreshold 实例）。
    - ``self._config_getter()``：实时读取配置 dict。
    - ``self._log(level, msg)``：日志回调。
    - ``self._autotune_trigger``：LLM 自动调参触发回调（None → 不触发）。
    """

    async def _maybe_autotune(
        self, group_id: str, adaptive: AdaptiveThreshold, now: float
    ) -> None:
        """v0.3.5 F4：触发率越界时自动触发 LLM 调参。

        两条触发路径：
        - 强制触发：rate > autotune_force_rate_threshold 且 allow_force 通过 →
          _autotune_trigger(force=True)，record_force 防抖（默认 1h 冷却）
        - 普通越界触发：rate > autotune_safe_rate_hi 或 < autotune_safe_rate_lo →
          _autotune_trigger(force=True) 修复限流 bug（原 force=False 被 rate_limited 拒绝）
        """
        cfg = self._config_getter()
        min_decisions = int(cfg.get("autotune_min_decisions", 30))
        hi = float(cfg.get("autotune_safe_rate_hi", 0.30))
        lo = float(cfg.get("autotune_safe_rate_lo", 0.05))
        force_threshold = float(cfg.get("autotune_force_rate_threshold", 0.50))
        samples = adaptive.window_size()
        rate = adaptive.window_rate()
        if samples < min_decisions:
            # 样本不足，跳过
            return

        # 优先判定强制触发（更高阈值，独立冷却防抖）
        if rate > force_threshold:
            # force 触发受独立冷却防抖（force_history）
            # force_cooldown 在 main 侧 _autotune_trigger 内读取并执行 allow_force 判断
            direction = "force_high_rate"
            asyncio.create_task(self._autotune_trigger(force=True))  # noqa
            self._log_autotune_event(group_id, direction, rate, samples)
            return

        # 普通越界触发（修复限流 bug：force=True 跳过 allow）
        if rate > hi or rate < lo:
            direction = "high_rate" if rate > hi else "low_rate"
            # 后台触发，不阻塞 run_batch
            asyncio.create_task(self._autotune_trigger(force=True))  # noqa
            self._log_autotune_event(group_id, direction, rate, samples)

    def _log_autotune_event(
        self, group_id: str, direction: str, rate: float, samples: int
    ) -> None:
        """记录一次自动调参触发事件（沿用 scheduler 注入的 log_fn）。"""
        try:
            self._log(
                "info",
                f"[ProSocial] autotune_triggered group={group_id} "
                f"direction={direction} rate={rate:.2f} samples={samples}",
            )
        except Exception:
            # 日志失败不影响主路径
            pass

    def collect_tune_stats(self) -> dict:
        """汇总最近 200 条决策用于 LLM 诊断调参（v0.2.8）。

        返回 dict 含：total / triggered_count / triggered_rate / suppressed_hist /
        score_{mean,median,min,max} / threshold_mean / hit_level_hist /
        factors_mean（五键） / fatigue_value_mean / config（全量配置快照，v0.2.9） /
        adaptive_summary（每群自适应阈值状态列表，v0.2.9）。

        空日志返回所有字段默认值（total=0 等），便于下游 LLM 安全引用。
        """
        decisions = self._decision_log.recent(200)
        # 配置快照（无论是否有决策都返回，供 LLM 对照当前参数）
        config_subset = self._tune_config_subset()
        # v0.2.9 adaptive_summary：每群自适应阈值状态（含 mult/window_rate/samples）
        adaptive_summary = self._build_adaptive_summary()
        # v0.3.5 F6：对话状态摘要供 LLM 诊断
        conversation_state_summary = self._build_conversation_state_summary()

        if not decisions:
            return {
                "total": 0,
                "triggered_count": 0,
                "triggered_rate": 0.0,
                "suppressed_hist": {},
                "score_mean": 0.0,
                "score_median": 0.0,
                "score_min": 0.0,
                "score_max": 0.0,
                "threshold_mean": 0.0,
                "hit_level_hist": {},
                "factors_mean": {
                    "s_int": 0.0,
                    "s_topic": 0.0,
                    "s_resp": 0.0,
                    "c_cooldown": 0.0,
                    "p_silence": 0.0,
                },
                "fatigue_value_mean": 0.0,
                "config": config_subset,
                "adaptive_summary": adaptive_summary,
                "conversation_state_summary": conversation_state_summary,
            }

        total = len(decisions)
        triggered_count = sum(1 for d in decisions if d.get("triggered"))
        triggered_rate = triggered_count / total if total > 0 else 0.0

        # suppressed_hist：suppressed_reason 频次（空串=未抑制但未触发→below_threshold）
        suppressed_hist: dict[str, int] = {}
        for d in decisions:
            reason = d.get("suppressed_reason") or ""
            if not reason and not d.get("triggered"):
                reason = "below_threshold"
            suppressed_hist[reason] = suppressed_hist.get(reason, 0) + 1

        # hit_level_hist
        hit_level_hist: dict[str, int] = {}
        for d in decisions:
            level = str(d.get("hit_level", "none"))
            hit_level_hist[level] = hit_level_hist.get(level, 0) + 1

        scores = [float(d.get("score", 0.0)) for d in decisions]
        thresholds = [float(d.get("threshold", 0.0)) for d in decisions]
        fatigue_values = [float(d.get("fatigue_value", 0.0)) for d in decisions]

        # factors_mean（五键）
        factor_keys = ("s_int", "s_topic", "s_resp", "c_cooldown", "p_silence")
        factors_mean: dict[str, float] = {}
        for k in factor_keys:
            vals = [
                float(d.get("factors", {}).get(k, 0.0))
                for d in decisions
                if isinstance(d.get("factors"), dict)
            ]
            factors_mean[k] = statistics.mean(vals) if vals else 0.0

        return {
            "total": total,
            "triggered_count": triggered_count,
            "triggered_rate": triggered_rate,
            "suppressed_hist": suppressed_hist,
            "score_mean": statistics.mean(scores),
            "score_median": statistics.median(scores),
            "score_min": min(scores),
            "score_max": max(scores),
            "threshold_mean": statistics.mean(thresholds),
            "hit_level_hist": hit_level_hist,
            "factors_mean": factors_mean,
            "fatigue_value_mean": statistics.mean(fatigue_values),
            "config": config_subset,
            "adaptive_summary": adaptive_summary,
            "conversation_state_summary": conversation_state_summary,
        }

    def _build_adaptive_summary(self) -> list[dict]:
        """构建每群自适应阈值状态摘要（v0.2.9）。

        遍历 self._groups，对每群导出 group_id / mult / window_rate / samples。
        仅含 adaptive 实例存在的群；_groups 为空返回 []。
        """
        summary: list[dict] = []
        for gid, g in self._groups.items():
            adaptive = g.get("adaptive")
            if adaptive is None:
                continue
            try:
                summary.append(
                    {
                        "group_id": gid,
                        "mult": float(adaptive.multiplier()),
                        "window_rate": float(adaptive.window_rate()),
                        "samples": int(adaptive.window_size()),
                    }
                )
            except Exception:
                # 单群异常跳过，不阻塞整体摘要
                continue
        return summary

    def _build_conversation_state_summary(self) -> dict:
        """v0.3.5 F6：构建对话状态摘要供 LLM 诊断。

        遍历每群最近 N 条消息，统计平均 appropriateness 和各状态占比。
        """
        try:
            from ..decision.conversation_state import ConversationStateEvaluator

            cfg = self._config_getter()
            if not bool(cfg.get("conversation_state_enabled", True)):
                return {"enabled": False}
            window = int(cfg.get("conversation_state_window", 10))
            summaries: list[dict] = []
            all_approps: list[float] = []
            all_has_q = all_mono = all_arg = all_casual = all_bot_turn = 0
            total = 0
            for gid, g in self._groups.items():
                try:
                    recent = g["context"]._messages[-window:]
                    if not recent:
                        continue
                    state = ConversationStateEvaluator.evaluate(
                        msgs=recent,
                        bot_user_id="__bot__",
                        cfg=cfg,
                        now=__import__("time").time(),
                    )
                    summaries.append(
                        {
                            "group_id": gid,
                            "appropriateness": round(state.appropriateness, 3),
                            "has_question": state.has_question,
                            "is_monologue": state.is_monologue,
                            "is_argument": state.is_argument,
                            "is_casual_chat": state.is_casual_chat,
                            "bot_turn": state.bot_turn,
                            "modifier": round(state.modifier, 3),
                        }
                    )
                    all_approps.append(state.appropriateness)
                    if state.has_question:
                        all_has_q += 1
                    if state.is_monologue:
                        all_mono += 1
                    if state.is_argument:
                        all_arg += 1
                    if state.is_casual_chat:
                        all_casual += 1
                    if state.bot_turn:
                        all_bot_turn += 1
                    total += 1
                except Exception:
                    continue
            if total == 0:
                return {"enabled": True, "groups": [], "avg_appropriateness": 0.0}
            return {
                "enabled": True,
                "groups": summaries,
                "avg_appropriateness": round(sum(all_approps) / len(all_approps), 3)
                if all_approps
                else 0.0,
                "has_question_ratio": round(all_has_q / total, 3),
                "is_monologue_ratio": round(all_mono / total, 3),
                "is_argument_ratio": round(all_arg / total, 3),
                "is_casual_chat_ratio": round(all_casual / total, 3),
                "bot_turn_ratio": round(all_bot_turn / total, 3),
            }
        except Exception:
            return {"enabled": True, "error": "summary_build_failed"}

    def _tune_config_subset(self) -> dict:
        """返回全量配置快照供 LLM 诊断时对照当前参数（v0.2.9）。

        v0.2.8 时仅返回调参相关子集；v0.2.9 起 LLM 诊断需要全量配置视野
        （含作息/兴趣/embedding 等），故改为返回完整 dict 副本。
        注意：DENYLIST 过滤是 main.py apply 阶段的事，此处不过滤。
        """
        cfg = self._config_getter()
        # 浅拷贝避免调用方误改 live 配置
        return dict(cfg)
