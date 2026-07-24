"""批次决策管线 Mixin（v0.3.0 模块 G 产出，对应 T7）。

将 SocialScheduler 中最重的 5 个方法（run_batch 决策管线主体 + _schedule_batch +
_dispatch_proactive + _send_wait_window_reply + glance_once）整体迁入
BatchPipelineMixin，经 MRO 仍可访问 SocialScheduler 实例属性与其他方法。

设计要点：
- **不定义 __init__**：纯 Mixin，状态由 SocialScheduler.__init__ 统一初始化。
- **不 import astrbot**：所有外部能力经注入回调获得，保证可离线测试。
- **方法签名与实现完全保持**：从 scheduler.py 整体迁入，未做任何逻辑变更。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

from ..common.models import BatchDecision, BatchRecord, GroupState, ScoreFactors
from ..common.prompts import build_glance_reply_prompt, build_reply_prompt
from ..decision.engine import WakeEngine
from ..decision.fusion import FusionEngine
from ..decision.inertia import WaitWindow
from ..decision.reply_keyword import ReplyKeywordManager
from ..decision.rule_engine import RuleEngine
from ..tracking.buffer import GroupBuffer


class BatchPipelineMixin:
    """批次决策管线 Mixin：run_batch / _schedule_batch / _dispatch_proactive /
    _send_wait_window_reply / glance_once 5 个方法。

    通过 mixin 多继承注入 SocialScheduler，方法经 MRO 仍访问 self 上的实例属性
    与其他方法（_get_group / _check_state_expiry / _cooldown_ratio / _embed /
    _llm / _send / on_bot_sent / _maybe_autotune / group_enabled）。

    依赖的实例属性（由 SocialScheduler.__init__ 注入）：
    - _config_getter / _interest_mgr / _inject / _autotune_trigger
    - _send_fn / _llm_fn / _embed_fn / _rate_limiter / _kv_get / _kv_set / _log
    - _groups / _umo_map / _fatigue
    - _replay_active / _dry_run_override

    不定义 __init__，所有状态由 SocialScheduler.__init__ 统一初始化。
    """

    async def _schedule_batch(self, group_id: str) -> None:
        """单群批次定时器：动态间隔睡眠 -> (转折词命中则跳过睡眠) -> run_batch。"""
        try:
            g = self._get_group(group_id)
            cfg = self._config_getter()

            # v0.2 惰性检查主动话题超时（不另起定时器，批处理前顺带检查）
            try:
                g["inertia"].check_proactive_timeout(time.time())
            except Exception:
                pass

            # 计算动态间隔：近 10 秒消息速率
            now = time.time()
            recent_count = sum(1 for t in g["msg_timestamps"] if now - t <= 10.0)
            recent_rate = recent_count / 10.0
            min_iv = float(cfg.get("batch_interval_min", 2.0))
            max_iv = float(cfg.get("batch_interval_max", 5.0))
            interval = GroupBuffer.dynamic_interval(recent_rate, min_iv, max_iv)

            # 紧急转弯词命中 -> 立即决策（不睡 interval）
            turn_kws = cfg.get("topic_turn_keywords", []) or []
            if not g["buffer"].contains_turn_keyword(list(turn_kws)):
                await asyncio.sleep(max(0.1, interval))

            await self.run_batch(group_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(
                "warning", f"[ProSocial] _schedule_batch 异常 group={group_id}: {e}"
            )
        finally:
            try:
                self._groups[group_id]["batch_task"] = None
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 核心决策管线
    # ------------------------------------------------------------------ #

    async def run_batch(self, group_id: str) -> None:
        """批次任务核心：flush 缓冲 -> 限流+嵌入 -> 个人跟踪快通道 -> engine.evaluate
        -> 记录决策 -> 触发则生成并发送 -> 状态转移。

        全程 try/except，单次失败 log 不影响后续。
        """
        try:
            g = self._get_group(group_id)
            now = time.time()
            self._check_state_expiry(g, now)

            # v0.2 惰性检查主动话题超时（批处理开头顺带检查，不另起定时器）
            try:
                g["inertia"].check_proactive_timeout(now)
            except Exception:
                pass

            # 1. flush 缓冲
            msgs = g["buffer"].flush()
            if not msgs:
                return

            # 2. 读 live 配置
            cfg = self._config_getter()
            base_threshold = float(cfg.get("base_threshold", 0.65))
            personal_threshold = float(cfg.get("personal_threshold", 0.55))
            hate_threshold = float(cfg.get("hate_similarity_threshold", 0.75))
            weights = {
                "w_int": float(cfg.get("w_int", 1.0)),
                "w_topic": float(cfg.get("w_topic", 0.4)),
                "w_resp": float(cfg.get("w_resp", 0.8)),
                "w_cooldown": float(cfg.get("w_cooldown", 0.5)),
                "w_silence": float(cfg.get("w_silence", 0.2)),
            }
            modifiers = {
                "core": float(cfg.get("core_interest_modifier", 0.7)),
                "general": float(cfg.get("general_interest_modifier", 1.0)),
                "marginal": float(cfg.get("edge_interest_modifier", 1.3)),
                "expecting": float(cfg.get("expecting_modifier", 0.8)),
            }

            # 3. 拼接批次文本
            batch_text = " ".join(m.text for m in msgs)
            # F5: 空批次过滤——文本全为空白时跳过嵌入和评估
            if not batch_text.strip():
                return

            # v0.3.5 F1：短批次合并——batch_text 过短且消息 ≤ 1 时回填缓冲区等待下次合并
            min_text_len = int(cfg.get("batch_min_text_length", 12))
            max_attempts = int(cfg.get("batch_short_merge_max_attempts", 2))
            attempts = g.get("short_batch_attempts", 0)
            if (
                len(batch_text) < min_text_len
                and len(msgs) <= 1
                and attempts < max_attempts
            ):
                # 回填缓冲区，等待下一次 _schedule_batch 触发时合并
                g["buffer"].prepend(msgs)
                g["short_batch_attempts"] = attempts + 1
                self._log(
                    "debug",
                    f"[ProSocial] run_batch: 短批次合并回填 group={group_id} "
                    f"len={len(batch_text)} attempts={attempts + 1}/{max_attempts}",
                )
                return

            batch_summary = batch_text[:80]

            # 4. 嵌入（限流）
            batch_emb: list[float] | None = None
            try:
                await self._rate_limiter.acquire()
                embs = await self._embed([batch_text])
                if embs:
                    batch_emb = embs[0]
                # 嵌入调用计数
                await self._metrics.incr("embedding_calls", self._kv_set)
            except Exception as e:
                self._log(
                    "warning", f"[ProSocial] run_batch: 嵌入异常 group={group_id}: {e}"
                )
                batch_emb = None

            # 5. (移除原 push_batch 调用，BUG-2: 移到 evaluate 之后，
            #     确保 topic_embedding 计算时 _batches 不含当前批次，避免自相似膨胀)

            interest = self._interest_mgr.get()

            # v0.2.5 提前读取回复关键词缓存（集成点 1 与集成点 2 共用）
            rk_cache = g.get("reply_keyword_cache")
            rk_enabled = bool(cfg.get("reply_keyword_enabled", True))
            # 关键词触发标志位（集成点 2 命中时置 True，供回复后清除与疲劳档位选择）
            keyword_triggered = False

            # 6. 个人跟踪快通道
            personal_triggered = False
            personal_users: set[str] = set()
            if batch_emb is not None:
                for entry in list(g["tracker"].all()):
                    # 该跟踪用户在本批有发言
                    if any(m.user_id == entry.user_id for m in msgs):
                        sim = WakeEngine.cosine(batch_emb, entry.bot_last_emb)
                        if sim >= personal_threshold:
                            personal_triggered = True
                            personal_users.add(entry.user_id)
                            # 触发后移出跟踪列表（PRD F4）
                            g["tracker"].remove(entry.user_id)
                            break  # 一次批次最多一个个人触发
                        else:
                            # v0.2.5 集成点 2：向量相似度不足时，转用关键词匹配作为强信号
                            keyword_match_for_track = 0.0
                            if (
                                rk_enabled
                                and rk_cache is not None
                                and rk_cache.is_valid_for(entry.user_id, now)
                            ):
                                try:
                                    track_user_text = " ".join(
                                        m.text
                                        for m in msgs
                                        if m.user_id == entry.user_id
                                    )
                                    keyword_match_for_track = (
                                        ReplyKeywordManager.match_score(
                                            track_user_text, rk_cache.keywords
                                        )
                                    )
                                except Exception:
                                    keyword_match_for_track = 0.0
                                if keyword_match_for_track >= float(
                                    cfg.get("reply_keyword_min_score_to_trigger", 0.5)
                                ):
                                    personal_triggered = True
                                    personal_users.add(entry.user_id)
                                    keyword_triggered = True
                                    g["tracker"].remove(entry.user_id)
                                    break  # 一次批次最多一个个人触发
                            # 关键词未触发 -> 累计不相关计数
                            if not keyword_triggered:
                                g["tracker"].bump_irrelevant(entry.user_id)

            # 清理超时/连续不相关跟踪条目
            try:
                g["tracker"].cleanup(
                    now,
                    timeout_sec=float(cfg.get("personal_track_timeout", 30)),
                    max_irrelevant=int(cfg.get("track_irrelevant_msgs", 3)),
                )
            except Exception:
                pass

            # 7. 计算 engine.evaluate 参数
            # BUG-2: topic_embedding 在 push_batch 之前算，此时 _batches 仅含历史批次，
            # 不含当前批次（push_batch 已下移到 evaluate 之后），避免 s_topic 自相似膨胀。
            topic_emb = g["context"].topic_embedding()
            bot_last_emb = g["last_bot_emb"]
            expecting = g["state"] == GroupState.EXPECTING_REPLY
            cooldown_ratio = self._cooldown_ratio(g, cfg)
            last_msg_ts = g["context"].last_message_ts()
            silence_sec = (now - last_msg_ts) if last_msg_ts > 0 else 1e9

            # 8. evaluate（batch_emb 非 None）或 rule_fallback（batch_emb 为 None）
            # v0.2: score_b/threshold_b 即 v0.1 的 score/threshold（通道 B 向量分）
            if batch_emb is not None:
                factors, score_b, threshold_b, hit_level = WakeEngine.evaluate(
                    batch_emb=batch_emb,
                    interest=interest,
                    topic_emb=topic_emb,
                    bot_last_emb=bot_last_emb,
                    expecting=expecting,
                    cooldown_ratio=cooldown_ratio,
                    silence_sec=silence_sec,
                    weights=weights,
                    base_threshold=base_threshold,
                    modifiers=modifiers,
                )
            else:
                # 降级：rule_fallback
                rule_hit = WakeEngine.rule_fallback(batch_text, interest, silence_sec)
                p_silence = min(silence_sec / 300.0, 1.0)
                factors = ScoreFactors(
                    s_int=0.0,
                    s_topic=0.0,
                    s_resp=0.0,
                    c_cooldown=cooldown_ratio,
                    p_silence=p_silence,
                )
                score_b = 0.0
                threshold_b = base_threshold
                hit_level = "none"
                # rule_hit 直接作为触发候选（仍受 hate/dry_run 约束）
                # 通过下方 triggered 逻辑处理

            # 8.5 BUG-2: push_batch 移到 evaluate 之后（topic_emb 计算前 _batches 不含当前批次）。
            # 当前批次压入滑动窗口供下次决策的 topic_embedding / 长窗口相关性复用。
            if batch_emb is not None:
                try:
                    g["context"].push_batch(
                        BatchRecord(
                            text=batch_text,
                            embedding=batch_emb,
                            ts=now,
                            messages=list(msgs),
                        )
                    )
                except Exception:
                    pass

            # 8.6 v0.2 双通道融合：RuleEngine + FusionEngine
            # 通道开关与动态权重参数
            vector_enabled = bool(cfg.get("enable_vector_channel", True))
            rule_enabled = bool(cfg.get("enable_rule_channel", True))
            dynamic_enabled = bool(cfg.get("dynamic_fusion_enabled", False))
            # 批次级 mentions_bot：任一消息 @Bot 或 batch_text 命中强唤醒词
            direct_wakeup_words = list(cfg.get("rule_direct_wakeup_words", []) or [])
            mentions_bot_batch = any(m.is_wake for m in msgs) or any(
                w in batch_text for w in direct_wakeup_words
            )
            # 规则引擎评估
            high_interest_keywords = (
                list(interest.high_interest_keywords) if interest is not None else []
            )
            fatigue_level_now = self._fatigue.level(now)
            try:
                rule_signal = RuleEngine.evaluate(
                    text=batch_text,
                    mentions_bot=mentions_bot_batch,
                    high_interest_keywords=high_interest_keywords,
                    rule_fatigue_level=fatigue_level_now,
                    config=cfg,
                )
            except Exception:
                # 兜底：规则引擎异常不阻塞决策，按无信号处理
                from ..common.models import RuleSignal

                rule_signal = RuleSignal(
                    score_a=0.0,
                    raw_score=0,
                    hit_type="none",
                    matched_word="",
                    mentions_bot=mentions_bot_batch,
                    is_question=False,
                    suppressed=False,
                    suppress_reason="",
                    fatigue_level=fatigue_level_now,
                )

            # 融合参数
            is_short = len(batch_text) <= 8
            has_direct_word = (
                bool(rule_signal.matched_word) and rule_signal.hit_type == "direct"
            )
            inertia_mult = g["inertia"].threshold_multiplier(now)
            # 融合计算（仅非降级路径参与；降级路径下方单独处理）
            if batch_emb is not None:
                try:
                    fusion = FusionEngine.fuse(
                        score_a=rule_signal.score_a,
                        score_b=score_b,
                        hit_level=hit_level,
                        expecting=expecting,
                        mentions_bot=mentions_bot_batch,
                        has_direct_word=has_direct_word,
                        is_short=is_short,
                        vector_enabled=vector_enabled,
                        rule_enabled=rule_enabled,
                        dynamic_enabled=dynamic_enabled,
                        inertia_multiplier=inertia_mult,
                        fatigue_level=fatigue_level_now,
                        config=cfg,
                    )
                except Exception:
                    # 兜底：融合异常退化为仅通道 B
                    from ..common.models import FusionResult

                    fusion = FusionResult(
                        score_a=rule_signal.score_a,
                        score_b=score_b,
                        alpha=0.0,
                        final_score=score_b,
                        threshold=threshold_b,
                        b_modifier=1.0,
                        a_modifier=1.0,
                        inertia_multiplier=inertia_mult,
                    )
            else:
                # 降级路径不参与融合（保持 v0.1 rule_fallback 语义）
                from ..common.models import FusionResult

                fusion = FusionResult(
                    score_a=rule_signal.score_a,
                    score_b=score_b,
                    alpha=0.0,
                    final_score=score_b,
                    threshold=threshold_b,
                    b_modifier=1.0,
                    a_modifier=1.0,
                    inertia_multiplier=inertia_mult,
                )

            # 8.7 v0.2.5 集成点 1：回复关键词匹配加分（融合 final_score 之后、triggered 判定之前）
            keyword_match_score = 0.0
            keyword_added_score = 0.0
            if rk_enabled and rk_cache is not None:
                # 找出本批中属于"最后交互对象"（target_user_id）的消息
                target_msgs = [m for m in msgs if m.user_id == rk_cache.target_user_id]
                if target_msgs and rk_cache.is_valid_for(rk_cache.target_user_id, now):
                    try:
                        user_text = " ".join(m.text for m in target_msgs)
                        keyword_match_score = ReplyKeywordManager.match_score(
                            user_text, rk_cache.keywords
                        )
                        keyword_added_score = keyword_match_score * float(
                            cfg.get("reply_keyword_boost_factor", 0.25)
                        )
                        fusion.final_score += keyword_added_score
                        # 连续低分清除：得分低于阈值时计数 +1，达 2 次清除缓存
                        if keyword_match_score < float(
                            cfg.get("reply_keyword_early_clear_low_score", 0.1)
                        ):
                            rk_cache.low_score_streak += 1
                            if rk_cache.low_score_streak >= 2:
                                g["reply_keyword_cache"] = None
                        # dry_run 日志：记录关键词、匹配得分、叠加后 final_score
                        self._log(
                            "debug",
                            f"[ProSocial] run_batch: reply_keyword group={group_id} "
                            f"target={rk_cache.target_user_id} "
                            f"keywords={list(rk_cache.keywords.keys())} "
                            f"match={keyword_match_score:.3f} added={keyword_added_score:.3f} "
                            f"final={fusion.final_score:.3f} thr={fusion.threshold:.3f}",
                        )
                    except Exception as e:
                        self._log(
                            "warning",
                            f"[ProSocial] run_batch: reply_keyword 加分失败 group={group_id}: {e}",
                        )

            # 9. 反感屏蔽
            suppressed_reason = ""
            if (
                interest is not None
                and batch_emb is not None
                and WakeEngine.hate_score(batch_emb, interest) >= hate_threshold
            ):
                suppressed_reason = "hate"

            # 9.5 v0.2 规则屏蔽短语：block_phrase 直接抑制（优先级高于冷却/疲劳）
            if (
                not suppressed_reason
                and rule_signal.suppressed
                and rule_signal.suppress_reason == "block_phrase"
            ):
                suppressed_reason = "block_phrase"

            # 10. 冷却压制（个人触发 bypass；core 可突破）
            if not suppressed_reason and not personal_triggered:
                if g["state"] == GroupState.COOLDOWN and hit_level != "core":
                    suppressed_reason = "cooldown"

            # v0.2.8 自适应阈值：eff_threshold = threshold * adaptive.multiplier()
            adaptive = g["adaptive"]
            if bool(cfg.get("adaptive_threshold_enabled", True)):
                eff_threshold = fusion.threshold * adaptive.multiplier()
            else:
                eff_threshold = fusion.threshold

            # v0.3.5 F6：对话状态模块——根据群聊氛围修正 eff_threshold
            conv_state_mod = 1.0
            if bool(cfg.get("conversation_state_enabled", True)):
                try:
                    from ..decision.conversation_state import (
                        ConversationStateEvaluator,
                    )

                    # 取最近 window 条消息（从 context._messages 取，含 bot 消息）
                    recent_msgs = g["context"]._messages[
                        -int(cfg.get("conversation_state_window", 10)) :
                    ]
                    conv_state = ConversationStateEvaluator.evaluate(
                        msgs=recent_msgs,
                        bot_user_id="__bot__",
                        cfg=cfg,
                        now=now,
                    )
                    conv_state_mod = conv_state.modifier
                    eff_threshold *= conv_state.modifier
                except Exception as e:
                    self._log(
                        "debug",
                        f"[ProSocial] run_batch: 对话状态评估失败 group={group_id}: {e}",
                    )

            # 11. 判定唤醒（v0.2 融合判定 + v0.2.8 自适应阈值）
            if suppressed_reason:
                triggered = False
            elif personal_triggered:
                # 个人快通道：bypass score 阈值
                triggered = True
            elif batch_emb is None:
                # 降级路径：保持 v0.1 rule_fallback 语义（不引入融合）
                triggered = bool(rule_hit)
            else:
                # v0.2 融合判定：final_score >= eff_threshold 且未被疲劳抑制
                is_forced = (
                    hit_level == "core"
                    or rule_signal.mentions_bot
                    or rule_signal.hit_type == "direct"
                )
                if self._fatigue.should_suppress(is_forced, now):
                    suppressed_reason = "fatigue"
                    triggered = False
                else:
                    triggered = fusion.final_score >= eff_threshold

            # v0.2.8 配额检查：触发或个人触发时，检查每群发送频率硬上限
            # 超限 → suppressed_reason="quota"，triggered 与 personal_triggered 均清零
            if triggered or personal_triggered:
                if not g["quota"].check(
                    now,
                    int(cfg.get("max_proactive_per_hour", 5)),
                    int(cfg.get("max_proactive_per_day", 20)),
                ):
                    suppressed_reason = "quota"
                    triggered = False
                    personal_triggered = False

            # v0.3.7：主动消息最小间隔冷却
            # 距上次主动消息不足 proactive_min_interval 秒 → suppressed_reason="min_interval"
            # 防止短时间内反复触发感兴趣话题导致话痨
            if triggered or personal_triggered:
                min_interval = int(cfg.get("proactive_min_interval", 180))
                if min_interval > 0:
                    last_proactive = g.get("last_proactive_ts", 0.0)
                    if last_proactive > 0 and (now - last_proactive) < min_interval:
                        suppressed_reason = "min_interval"
                        triggered = False
                        personal_triggered = False

            # 12. DRY_RUN（含回放：replay_active 视同 dry_run）
            is_dry = self._replay_active or (
                self._dry_run_override
                if self._dry_run_override is not None
                else bool(cfg.get("dry_run", False))
            )
            if triggered and is_dry:
                suppressed_reason = "dry_run"
                triggered = False

            # 总开关关 -> disabled（配置可能在运行时被关）
            if not bool(cfg.get("enable", True)) and not suppressed_reason:
                suppressed_reason = "disabled"
                triggered = False

            # v0.3.5 F1：成功评估（无论是否触发）后重置短批次合并计数
            g["short_batch_attempts"] = 0

            # 13. 构造决策日志并持久化（v0.2: score/threshold 取融合值，追加 6 字段）
            # channel: 双开 fusion / 仅A rule / 仅B vector
            channel = (
                "fusion"
                if (vector_enabled and rule_enabled)
                else ("rule" if rule_enabled and not vector_enabled else "vector")
            )
            d = BatchDecision(
                ts=now,
                group_id=group_id,
                batch_summary=batch_summary,
                factors=factors,
                score=float(fusion.final_score),
                threshold=float(fusion.threshold),
                hit_level=hit_level,
                triggered=triggered,
                suppressed_reason=suppressed_reason,
                dry_run=is_dry,
                message_count=len(msgs),
                # v0.2 双通道增量字段
                score_a=float(rule_signal.score_a),
                score_b=float(score_b),
                alpha=float(fusion.alpha),
                fatigue_level=fatigue_level_now,
                fatigue_value=float(self._fatigue.snapshot(now)["value"]),
                channel=channel,
                keyword_match_score=float(keyword_match_score),
                keyword_added_score=float(keyword_added_score),
                # v0.2.6 Embedding 降级标记（F12）
                embedding_degraded=(batch_emb is None),
                # v0.2.8 自适应阈值倍率（F2a）
                adaptive_mult=float(adaptive.multiplier()),
                conversation_state_mod=float(conv_state_mod),
            )
            self._decision_log.add(d)
            try:
                await self._kv_set("decision_log", self._decision_log.to_list())
            except Exception as e:
                self._log(
                    "warning", f"[ProSocial] run_batch: 持久化 decision_log 失败: {e}"
                )

            # v0.2.8 自适应阈值控制器：每次决策后记录（含未触发），满 20 自动步进
            # v0.2.9：评估周期刚到（record 返回 True）时联动 LLM 自动调参触发
            try:
                just_evaluated = adaptive.record(float(fusion.final_score), triggered)
            except Exception:
                just_evaluated = False
            if (
                just_evaluated
                and bool(cfg.get("autotune_auto_trigger_enabled", True))
                and self._autotune_trigger is not None
            ):
                try:
                    await self._maybe_autotune(group_id, adaptive, now)
                except Exception:
                    # 自动调参触发异常不影响 run_batch 主路径
                    pass

            # 14. 触发 -> 生成并发送（v0.2.8 统一走 _dispatch_proactive）
            if triggered:
                # 注入文本：短窗口文本（含「昵称: 内容」上下文），空则退回 batch_text
                inject_text = g["context"].short_window_text() or batch_text
                hint = "这是群聊最新动态，请以你的人设自然接一两句话，简短口语化，不要复读。"
                # v0.2: reply_type 区分 active（批处理触发）/ track（个人跟踪）
                reply_type = "track" if personal_triggered else "active"

                # 判断是否走注入路径（决定长窗口上下文是否预计算）
                inject_enabled = bool(cfg.get("reply_via_pipeline", True)) and (
                    self._inject is not None
                )

                # F8: 主动回复时注入长窗口上下文（仅旧路径需要，预计算保 lazy）
                precomputed_extra_ctx = ""
                if not inject_enabled and bool(
                    cfg.get("long_window_inject_proactive", True)
                ):
                    try:
                        short_text = g["context"].short_window_text()
                        if short_text and batch_emb is not None:
                            anchor_embs = await self._embed([short_text])
                            anchor_emb = anchor_embs[0] if anchor_embs else None
                            if anchor_emb:
                                top_n = int(cfg.get("long_window_top_n", 6))
                                long_texts = g["context"].select_long_relevant(
                                    anchor_emb, top_n
                                )
                                if long_texts:
                                    long_window_text = "\n".join(long_texts)
                                    long_summarize = bool(
                                        cfg.get("long_window_summarize", False)
                                    )
                                    if long_summarize:
                                        from ..common.prompts import (
                                            build_summary_prompt,
                                        )

                                        summary = await self._llm(
                                            build_summary_prompt(
                                                long_window_text, short_text
                                            )
                                        )
                                        precomputed_extra_ctx = (
                                            f"相关历史摘要：\n{summary}"
                                            if summary
                                            else ""
                                        )
                                    else:
                                        precomputed_extra_ctx = (
                                            f"相关历史背景：\n{long_window_text}"
                                        )
                    except Exception as e:
                        self._log(
                            "warning",
                            f"[ProSocial] run_batch: 长窗口注入失败 group={group_id}: {e}",
                        )

                # fallback_prompt_builder：旧路径惰性构造 prompt（闭包捕获预计算上下文）
                def _build_reply_prompt(_extra_ctx=precomputed_extra_ctx):
                    persona_text = str(cfg.get("persona_text", ""))
                    short_window_text = g["context"].short_window_text()
                    return build_reply_prompt(
                        persona_text=persona_text,
                        short_window=short_window_text,
                        extra_context=_extra_ctx,
                        batch_text=batch_text,
                    )

                sent = await self._dispatch_proactive(
                    group_id=group_id,
                    inject_text=inject_text,
                    hint=hint,
                    reply_type=reply_type,
                    fallback_prompt_builder=_build_reply_prompt,
                    is_proactive=True,
                    # v0.2.8：Sender 用触发用户 ID——个人跟踪取目标用户，否则取本批首条发言者
                    sender_id=(
                        rk_cache.target_user_id
                        if personal_triggered and rk_cache is not None
                        else (msgs[0].user_id if msgs else "")
                    ),
                )
                if sent:
                    # v0.2.5 集成点 3：因关键词触发（集成点 2）的回复清除关键词缓存，防重复
                    # 注：on_bot_sent 会基于新回复重建缓存；此处清除是防止 on_bot_sent 失败时旧缓存残留
                    if keyword_triggered:
                        g["reply_keyword_cache"] = None
                    # v0.2 等待窗口：发送成功后开窗收集同触发用户后续消息
                    # 降级方案：不起 100ms 轮询 task，依赖 on_message 触发 should_close 检查
                    # 注：注入模式成功后同样就地 open 等待窗口，不依赖 after_message_sent 钩子
                    try:
                        ww_duration_ms = int(cfg.get("wait_window_duration_ms", 3000))
                        if ww_duration_ms > 0:
                            ww = WaitWindow(
                                duration_ms=ww_duration_ms,
                                max_extra=int(cfg.get("wait_window_max_extra", 3)),
                            )
                            # 触发用户 ID：与 sender_id 一致——个人跟踪取目标用户，否则取首条发言者
                            trigger_uid = (
                                rk_cache.target_user_id
                                if personal_triggered and rk_cache is not None
                                else (msgs[0].user_id if msgs else "")
                            )
                            ww.open(
                                now_ms=time.time() * 1000.0, trigger_user_id=trigger_uid
                            )
                            if ww.active:
                                g["wait_window"] = ww
                    except Exception as e:
                        self._log(
                            "warning",
                            f"[ProSocial] run_batch: 开等待窗口失败 group={group_id}: {e}",
                        )
            else:
                # 15. 未触发 -> debug
                self._log(
                    "debug",
                    f"[ProSocial] run_batch: 未触发 group={group_id} "
                    f"final={fusion.final_score:.3f} thr={eff_threshold:.3f} "
                    f"hit={hit_level} reason={suppressed_reason or 'below_threshold'}",
                )
        except Exception as e:
            self._log("error", f"[ProSocial] run_batch 异常 group={group_id}: {e}")

    # ------------------------------------------------------------------ #
    # v0.2.8 统一主动回复分发
    # ------------------------------------------------------------------ #

    async def _dispatch_proactive(
        self,
        *,
        group_id: str,
        inject_text: str,
        hint: str,
        reply_type: str,
        fallback_prompt_builder: Callable[[], str],
        is_proactive: bool,
        call_on_bot_sent: bool = True,
        sender_id: str = "",
    ) -> bool:
        """统一主动回复分发（v0.2.8）。

        cfg ``reply_via_pipeline``=True 且 ``self._inject`` 存在 → 走管线注入
        （main.py 实现 ``inject_fn(umo, text, hint, group_id, sender_id) -> bool``，
        sender_id 作为合成消息 Sender 的 user_id，让对话历史显示真实触发用户）；
        注入成功 → 计数 proactive_sends + proactive_triggered、quota.record、返回 True
        （on_bot_sent 由 main.py after_message_sent 钩子触发，不在此调）。

        注入失败/关闭 → 旧路径：``fallback_prompt_builder()`` → ``self._llm`` →
        ``self._send`` → 成功后 quota.record + on_bot_sent（call_on_bot_sent=True 时）。

        返回 True 表示已成功发送（注入或旧路径），False 表示未发送/失败。
        """
        cfg = self._config_getter()
        g = self._get_group(group_id)
        now = time.time()
        umo = g.get("umo") or self._umo_map.get(group_id, "")

        # 注入路径
        if bool(cfg.get("reply_via_pipeline", True)) and self._inject is not None:
            try:
                ok = await self._inject(umo, inject_text, hint, group_id, sender_id)
            except Exception as e:
                self._log(
                    "warning",
                    f"[ProSocial] _dispatch_proactive: inject_fn 异常 group={group_id}: {e}",
                )
                ok = False
            if ok:
                await self._metrics.incr("proactive_sends", self._kv_set)
                await self._metrics.incr("proactive_triggered", self._kv_set)
                g["quota"].record(now)
                # v0.3.7：记录主动消息发送时间戳，用于 proactive_min_interval 冷却
                g["last_proactive_ts"] = now
                return True
            # 注入失败 → 降级走旧路径

        # 旧路径：惰性构造 prompt → LLM 生成 → 发送
        prompt = fallback_prompt_builder()
        await self._metrics.incr("llm_calls", self._kv_set)
        reply = await self._llm(prompt)
        if not reply:
            self._log(
                "warning",
                f"[ProSocial] _dispatch_proactive: LLM 无回复 group={group_id}",
            )
            return False
        ok = await self._send(umo, reply)
        await self._metrics.incr("proactive_sends", self._kv_set)
        if ok:
            await self._metrics.incr("proactive_triggered", self._kv_set)
            g["quota"].record(now)
            # v0.3.7：记录主动消息发送时间戳，用于 proactive_min_interval 冷却
            g["last_proactive_ts"] = now
            if call_on_bot_sent:
                try:
                    await self.on_bot_sent(
                        group_id=group_id,
                        text=reply,
                        ts=time.time(),
                        reply_type=reply_type,
                        is_proactive=is_proactive,
                    )
                except Exception as e:
                    self._log(
                        "warning",
                        f"[ProSocial] _dispatch_proactive: on_bot_sent 异常 group={group_id}: {e}",
                    )
            return True
        else:
            self._log(
                "warning",
                f"[ProSocial] _dispatch_proactive: 发送失败 group={group_id}",
            )
            return False

    async def _send_wait_window_reply(
        self, group_id: str, merged_text: str, trigger_user: str
    ) -> None:
        """等待窗口关闭后：把合并文本交 LLM 生成连贯回复并发送（v0.2）。

        被动接话语义（is_proactive=False，reply_type="passive"），发送成功后走
        on_bot_sent 完成疲劳消耗/惯性/跟踪/瞥眼。v0.2.8 起统一走 _dispatch_proactive
        （注入路径 + 旧路径降级），配额检查前置。
        """
        try:
            g = self._get_group(group_id)
            cfg = self._config_getter()
            now = time.time()

            # v0.2.8 配额检查前置：超限直接 return（省 LLM 开销）
            if not g["quota"].check(
                now,
                int(cfg.get("max_proactive_per_hour", 5)),
                int(cfg.get("max_proactive_per_day", 20)),
            ):
                self._log(
                    "debug",
                    f"[ProSocial] _send_wait_window_reply: 配额超限 group={group_id}",
                )
                return

            # fallback_prompt_builder：旧路径惰性构造 prompt
            def _build_prompt():
                persona_text = str(cfg.get("persona_text", ""))
                short_window_text = g["context"].short_window_text()
                return build_reply_prompt(
                    persona_text=persona_text,
                    short_window=short_window_text,
                    extra_context="",
                    batch_text=merged_text,
                )

            await self._dispatch_proactive(
                group_id=group_id,
                inject_text=merged_text,
                hint="请基于对方刚才的连续发言连贯回复一两句。",
                reply_type="passive",
                fallback_prompt_builder=_build_prompt,
                is_proactive=False,
                # v0.2.8：Sender 用等待窗口的触发用户
                sender_id=trigger_user,
            )
        except Exception as e:
            self._log(
                "warning",
                f"[ProSocial] _send_wait_window_reply 异常 group={group_id}: {e}",
            )

    # ------------------------------------------------------------------ #
    # 瞥一眼（F5）
    # ------------------------------------------------------------------ #

    async def glance_once(self, from_group: str) -> None:
        """多群随机瞥一眼：候选群 -> 排除过热/沉默 -> 关键词命中 -> 嵌入判断 -> 插话（最多一群）。"""
        try:
            cfg = self._config_getter()
            if not bool(cfg.get("glance_enable", True)):
                return
            if self._replay_active:
                return  # 回放期间不瞥眼

            now = time.time()
            hot_limit = int(cfg.get("hot_group_msg_limit", 30))
            silent_sec = int(cfg.get("silent_group_minutes", 10)) * 60
            glance_count = int(cfg.get("glance_group_count", 3))
            glance_min_score = float(cfg.get("glance_min_score", 0.85))
            persona_text = str(cfg.get("persona_text", ""))

            # BUG-1: 关键词从 InterestData 取，非 config（PRD F5/§8.3）。
            # _conf_schema 无 high_interest_keywords 项，从 cfg 取恒为 []；
            # PRD F5 明确这些关键词属于启动时 LLM 生成的兴趣语料，存在 InterestData。
            interest = self._interest_mgr.get()
            high_kws = list(interest.high_interest_keywords) if interest else []

            # 1. 候选群：非 from_group、已启用
            candidates: list[str] = []
            for gid, g in self._groups.items():
                if gid == from_group:
                    continue
                if not self.group_enabled(gid):
                    continue
                # 排除过热群（最近 60 秒消息数 > hot_limit）
                recent_60 = sum(1 for t in g["msg_timestamps"] if now - t <= 60.0)
                if recent_60 > hot_limit:
                    continue
                # 排除沉默群
                if g["last_active_ts"] <= 0 or (now - g["last_active_ts"]) > silent_sec:
                    continue
                candidates.append(gid)

            if not candidates:
                return

            # 2. 随机选 min(glance_count, len(candidates)) 个
            random.shuffle(candidates)
            picked = candidates[: max(1, glance_count)]

            # 3. 逐个检查：最后一条消息命中关键词 -> 嵌入判断 -> 插话（最多一群）
            for gid in picked:
                g = self._groups[gid]
                # 取 context 最后一条非 bot 消息文本（从 recent_speakers 取最近一位的 last_text）
                speakers = g["context"].recent_speakers(1)
                if not speakers:
                    continue
                speaker_uid, _, last_text = speakers[0]
                if not last_text:
                    continue
                # 关键词命中
                if not any(kw and kw in last_text for kw in high_kws):
                    continue
                # 嵌入判断
                embs = await self._embed([last_text])
                if not embs:
                    continue
                emb = embs[0]
                s, _level = WakeEngine.interest_score(emb, interest)
                if s < glance_min_score:
                    continue
                # 冷却允许：state 不在 COOLDOWN，或 state_until 已过
                self._check_state_expiry(g, now)
                if g["state"] == GroupState.COOLDOWN:
                    continue
                # v0.2.8 配额检查前置（用目标群的 g["quota"]）：超限跳过此群
                if not g["quota"].check(
                    now,
                    int(cfg.get("max_proactive_per_hour", 5)),
                    int(cfg.get("max_proactive_per_day", 20)),
                ):
                    continue

                # v0.2.8 统一走 _dispatch_proactive（注入路径 + 旧路径降级）
                # call_on_bot_sent=False：瞥眼不调 on_bot_sent 全流程（保持原有语义，
                # 避免为瞥眼群设置 EXPECTING_REPLY/跟踪/级联瞥眼），仅手动消耗疲劳
                def _build_glance_prompt(_persona=persona_text, _target=last_text):
                    return build_glance_reply_prompt(_persona, _target)

                sent = await self._dispatch_proactive(
                    group_id=gid,
                    inject_text=last_text,
                    hint="像路人随口一句，不超过 30 字，不复读不提问。",
                    reply_type="glance",
                    fallback_prompt_builder=_build_glance_prompt,
                    is_proactive=True,
                    call_on_bot_sent=False,
                    # v0.2.8：Sender 用瞥眼命中的目标消息发送者
                    sender_id=speaker_uid,
                )
                if sent:
                    # v0.2 疲劳消耗：瞥眼插话按 glance 类型计（不调 on_bot_sent 全流程，
                    # 避免为瞥眼群设置 EXPECTING_REPLY/跟踪/级联瞥眼）
                    try:
                        self._fatigue.consume("glance")
                    except Exception:
                        pass
                # 每次瞥眼最多插话一个群
                self._log(
                    "info", f"[ProSocial] glance_once: 插话 group={gid} score={s:.3f}"
                )
                break
        except Exception as e:
            self._log("warning", f"[ProSocial] glance_once 异常 from={from_group}: {e}")
