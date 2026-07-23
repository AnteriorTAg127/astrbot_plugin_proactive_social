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
        """触发率越界时自动触发 LLM 调参（v0.2.9 F3）。

        条件：窗口样本数 ≥ autotune_min_decisions 且 window_rate > autotune_safe_rate_hi
        或 < autotune_safe_rate_lo。后台 asyncio.create_task 调 autotune_trigger_fn。
        速率限制由 main.py 的 _autotune_trigger 内部判断（返回 ok:False,error:rate_limited）。
        """
        cfg = self._config_getter()
        min_decisions = int(cfg.get("autotune_min_decisions", 30))
        hi = float(cfg.get("autotune_safe_rate_hi", 0.30))
        lo = float(cfg.get("autotune_safe_rate_lo", 0.05))
        samples = adaptive.window_size()
        rate = adaptive.window_rate()
        if samples < min_decisions:
            # 样本不足，跳过
            return
        if rate > hi or rate < lo:
            direction = "high_rate" if rate > hi else "low_rate"
            # 后台触发，不阻塞 run_batch
            asyncio.create_task(self._autotune_trigger())  # noqa
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

    def _tune_config_subset(self) -> dict:
        """返回全量配置快照供 LLM 诊断时对照当前参数（v0.2.9）。

        v0.2.8 时仅返回调参相关子集；v0.2.9 起 LLM 诊断需要全量配置视野
        （含作息/兴趣/embedding 等），故改为返回完整 dict 副本。
        注意：DENYLIST 过滤是 main.py apply 阶段的事，此处不过滤。
        """
        cfg = self._config_getter()
        # 浅拷贝避免调用方误改 live 配置
        return dict(cfg)
