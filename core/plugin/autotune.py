"""模块 C：LLM 诊断调参（autotune）—— TuneMixin。

职责：LLM 驱动的整体性调参（analyze / apply），维护 ``TUNE_DENYLIST`` 安全约束
与 ``_STYLE_GUIDANCE`` 风格引导，协调 ``ConfigStore`` 标量写入、
``InterestManager`` 关键词增删、人设变更触发的后台兴趣重建，
提供 ``TuneRateLimiter`` 速率限制状态查询。

依赖的实例属性（由 ``ProSocialPlugin`` / 其他 mixin 提供，经 MRO 在 ``self`` 上访问）：
``self.scheduler``、``self._llm_fn`` / ``self._embed_fn``、``self._config_getter()``、
``self._config_store``、``self.interest_mgr``、``self._tune_limiter``、
``self._last_tune_suggestion``、``self._log(level, msg)``、``self.context``。
"""

from __future__ import annotations

import asyncio
import json
import time

from ..storage.config_store import ConfigStore

# v0.2.9 F2：LLM 调参安全敏感键 denylist——这些键 LLM 不可改（会破坏运行）。
# 其余 DEFAULT_CONFIG 全部键（约 70 项）均允许 LLM 经 llm_autotune 修改（denylist 模式）。
# 与 scheduler._tune_config_subset() 配合：scheduler 返回全量快照，main apply 阶段过滤 DENYLIST。
TUNE_DENYLIST = frozenset(
    {
        "enable",
        "dry_run",
        "group_whitelist",
        "group_mode",
        "chat_provider_id",
        "embedding_provider_id",
    }
)

# v0.2.8 F3：回复风格偏好 → LLM 调参方向引导（v0.2.9 沿用）
_STYLE_GUIDANCE = {
    "proactive": (
        "偏主动——用户希望机器人更活跃、更频繁地插话参与群聊。\n"
        "调参方向：适当降低 base_threshold / personal_threshold，"
        "提高 w_int / w_topic 等感知权重，放宽 fatigue_limit，"
        "目标触发率偏向 20%-30% 区间。但不可导致话痨（仍受 max_proactive_per_hour/day 兜底）。"
    ),
    "balanced": (
        "平衡——用户希望机器人自然参与，不话痨也不沉默。\n"
        "调参方向：维持当前阈值结构，仅微调失衡因子，"
        "目标触发率 10%-20% 区间。"
    ),
    "passive": (
        "偏被动——用户希望机器人克制，只在高度相关或被@时才插话。\n"
        "调参方向：适当提高 base_threshold / personal_threshold，"
        "降低 w_silence（减少纯沉默触发），收紧 fatigue_limit，"
        "目标触发率 5%-10% 区间。"
    ),
}


class TuneMixin:
    """LLM 诊断调参 Mixin（v0.2.9 F1/F2/F3/F4/F5）。

    提供 11 个方法：``_writable_keys``（classmethod）、``run_autotune``、
    ``llm_autotune``（核心 analyze / apply）、``_build_tune_prompt``、
    ``_tune_current_config``、``_format_tune_status``、``_parse_tune_response``
    （staticmethod）、``_rate_limit_status``、``_bg_regenerate_persona``、
    ``_apply_keywords_patch``、``_autotune_trigger``。

    依赖的实例属性见模块级 docstring。本 Mixin 不定义 ``__init__``，
    经 MRO 在 ``ProSocialPlugin`` 中与 ``CallbacksMixin`` / ``WebBridgeMixin`` /
    ``CommandsMixin`` / ``Star`` 组合。
    """

    # v0.2.9 兼容：作为类属性暴露，使 ``ProSocialPlugin.TUNE_DENYLIST`` 经继承可访问。
    TUNE_DENYLIST = TUNE_DENYLIST

    @classmethod
    def _writable_keys(cls) -> set[str]:
        """v0.2.9 F2：可写键 = DEFAULT_CONFIG - DENYLIST（约 70 项）。

        ConfigStore 已在顶部 import，无循环依赖；动态计算保证与 DEFAULT_CONFIG 同步。
        """
        return set(ConfigStore.DEFAULT_CONFIG) - TUNE_DENYLIST

    async def run_autotune(self, body: dict) -> dict:
        """WebBridge 鸭子接口：``POST /prosocial/autotune`` 入口（v0.2.9 扩展）。

        body 字段：action (``analyze``|``apply``)、patch（apply 缓存补丁）、
        style（proactive/balanced/passive）、guidance（用户补充）、force（跳过限速）、
        keywords_patch（关键词增删）、persona_revision（人设改写）。
        返回扁平 dict 透传前端。
        """
        if not isinstance(body, dict):
            return {"ok": False, "error": "body 必须是 JSON 对象"}
        action = body.get("action")
        if action not in ("analyze", "apply"):
            return {"ok": False, "error": "action 必须是 analyze 或 apply"}
        patch = body.get("patch") if action == "apply" else None
        style = str(body.get("style", "")) if action == "analyze" else ""
        guidance = str(body.get("guidance", "")) if action == "analyze" else ""
        force = bool(body.get("force", False))
        keywords_patch = body.get("keywords_patch")
        persona_revision = body.get("persona_revision")
        return await self.llm_autotune(
            action,
            patch,
            style=style,
            guidance=guidance,
            force=force,
            keywords_patch=keywords_patch,
            persona_revision=persona_revision,
        )

    async def llm_autotune(
        self,
        action: str,
        patch: dict | None = None,
        *,
        style: str = "",
        guidance: str = "",
        force: bool = False,
        keywords_patch: dict | None = None,
        persona_revision: str | None = None,
    ) -> dict:
        """v0.2.9 F1/F2/F4：LLM 诊断调参核心（全视野 + denylist + 速率限制）。

        - ``analyze``：``collect_tune_stats`` → 构造 prompt → ``_llm_fn`` → 解析 JSON →
          DENYLIST 过滤 → 缓存到 ``_last_tune_suggestion``（含三段）→ ``record()`` 计入配额。
        - ``apply``：patch 来自参数或缓存 → persona_revision 合并入 persona_text →
          DENYLIST 过滤 → ``ConfigStore.set_many`` / 人设变更触发后台 regenerate /
          keywords_patch 走 ``interest_mgr`` 增删 + ``apply_rejected``。
        - ``force=True`` 跳过 ``allow()``（仅 analyze 走此路径），analyze 成功后仍 ``record()``。
        - 速率限制仅作用于 analyze（apply 不调 LLM，无成本）。
        """
        if self.scheduler is None or self._llm_fn is None:
            return {"ok": False, "error": "scheduler 或 llm_fn 未就绪"}

        now = time.time()
        cfg = self._config_getter()
        cooldown = float(cfg.get("autotune_cooldown_hours", 3.0))
        max_per_day = int(cfg.get("autotune_max_per_day", 4))

        # v0.2.9 F4：速率限制仅作用于 analyze（apply 不调 LLM）
        if action == "analyze" and not force:
            ok, reason = self._tune_limiter.allow(now, cooldown, max_per_day)
            if not ok:
                return {
                    "ok": False,
                    "error": "rate_limited",
                    "reason": reason,
                    "limit": max_per_day,
                    "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
                }

        if action == "analyze":
            stats = self.scheduler.collect_tune_stats()
            prompt = self._build_tune_prompt(stats, style=style, guidance=guidance)
            try:
                raw = await self._llm_fn(prompt)
            except Exception as e:
                return {"ok": False, "error": f"LLM 调用失败: {e}"}
            parsed = self._parse_tune_response(raw)
            if not parsed:
                return {"ok": False, "error": "LLM 输出解析失败（非 JSON）"}
            suggested = parsed.get("suggested_patch", {}) or {}
            # v0.2.9 F2：DENYLIST 过滤——丢弃安全敏感键，注明
            filtered = {k: v for k, v in suggested.items() if k not in TUNE_DENYLIST}
            dropped = [k for k in suggested if k in TUNE_DENYLIST]
            analysis = str(parsed.get("analysis", "") or "")
            if dropped:
                analysis += f"\n\n[已过滤安全敏感键: {', '.join(dropped)}]"
            suggested_keywords = parsed.get("suggested_keywords_patch") or None
            persona_rev = parsed.get("persona_revision") or None
            # 缓存建议（含三段，供 apply 复用）
            self._last_tune_suggestion = {
                "suggested_patch": filtered,
                "suggested_keywords_patch": suggested_keywords,
                "persona_revision": persona_rev,
            }
            # analyze 成功后 record（计入配额；force=True 也 record）
            self._tune_limiter.record(now)
            return {
                "ok": True,
                "analysis": analysis,
                "suggested_patch": filtered,
                "suggested_keywords_patch": suggested_keywords,
                "persona_revision": persona_rev,
                "expected_effect": str(parsed.get("expected_effect", "") or ""),
                "applied": False,
                "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
            }

        if action == "apply":
            # 从缓存或参数取 patch + 可选 keywords_patch / persona_revision
            if patch is None:
                cached = self._last_tune_suggestion or {}
                if isinstance(cached, dict):
                    patch = cached.get("suggested_patch", {}) or {}
                    if not keywords_patch:
                        keywords_patch = cached.get("suggested_keywords_patch")
                    if not persona_revision:
                        persona_revision = cached.get("persona_revision")
                else:
                    patch = {}
            # persona_revision 合并入 persona_text 走同一路径
            if persona_revision:
                patch = dict(patch)
                patch["persona_text"] = persona_revision
            if not patch and not keywords_patch:
                return {"ok": False, "error": "无可应用的 patch（无参数且无缓存建议）"}
            # v0.2.9 F2：DENYLIST 过滤
            filtered = {k: v for k, v in patch.items() if k not in TUNE_DENYLIST}
            dropped = [k for k in patch if k in TUNE_DENYLIST]
            # 标量写入（ConfigStore.set_many 内置类型/范围校验，DENYLIST 已过滤）
            ok, msg = (True, "")
            if filtered:
                ok, msg = await self._config_store.set_many(filtered)
            if not ok:
                return {
                    "ok": False,
                    "applied": False,
                    "error": msg,
                    "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
                }
            # v0.3.5 F5：人设/数量变更 + 关键词 patch 放后台执行，API 立即返回
            regenerate_needed = any(
                k in filtered
                for k in (
                    "persona_text",
                    "persona_knowledge",
                    "interest_example_count",
                    "interest_keyword_count",
                )
            )
            background_pending = bool(regenerate_needed or keywords_patch)
            if background_pending:
                try:
                    asyncio.create_task(
                        self._bg_apply_keywords_and_regenerate(
                            keywords_patch=keywords_patch if keywords_patch else None,
                            regenerate_needed=regenerate_needed,
                        )
                    )
                except Exception as e:
                    self._log("warning", f"启动后台 apply 任务失败: {e}")
            # 应用成功后清空缓存，避免重复 apply
            self._last_tune_suggestion = None
            return {
                "ok": True,
                "applied": True,
                "updated": len(filtered),
                "dropped": dropped,
                "regenerate": regenerate_needed,
                "keywords_updated": 0,  # 后台执行中，实际数稍后可查 interests API
                "background": background_pending,
                "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
            }

        return {"ok": False, "error": f"未知 action: {action}"}

    def _rate_limit_status(self, now: float, cooldown: float, max_per_day: int) -> dict:
        """v0.2.9 F4：返回当前速率限制状态块（供响应附带，前端展示用）。

        从 ``TuneRateLimiter.state()`` 取 history 与 last_call 自行计算 used / next_available，
        避免扩展 tune_controller 的公开方法。
        """
        try:
            state = self._tune_limiter.state()
        except Exception:
            state = {"history": [], "last_call": None}
        history = state.get("history") or []
        last_call = state.get("last_call")
        used = len([t for t in history if t >= now - 86400])
        next_available = 0.0
        if last_call is not None and cooldown > 0:
            next_available = max(0.0, last_call + cooldown * 3600 - now)
        return {
            "used": used,
            "limit": max_per_day,
            "next_available": int(next_available),
            "cooldown_hours": cooldown,
        }

    async def _bg_regenerate_persona(self) -> None:
        """v0.2.9 F2：人设变更后后台重建兴趣数据（不阻塞 apply 响应）。

        复用 v0.2.6 F4 / set_config_view 的 regenerate 调用模式。
        """
        try:
            cfg = self._config_getter()
            persona_text = str(cfg.get("persona_text", ""))
            persona_knowledge = str(cfg.get("persona_knowledge", ""))
            example_count = int(cfg.get("interest_example_count", 3))
            keyword_count = int(cfg.get("interest_keyword_count", 12))
            await self.interest_mgr.regenerate(
                persona_text,
                persona_knowledge,
                self._llm_fn,
                self._embed_fn,
                example_count=example_count,
                keyword_count=keyword_count,
            )
            self._log("info", "人设变更，兴趣数据已重新生成")
        except Exception as exc:
            self._log("warning", f"人设变更后兴趣重建失败: {exc}")

    async def _bg_apply_keywords_and_regenerate(
        self,
        *,
        keywords_patch: dict | None,
        regenerate_needed: bool,
    ) -> None:
        """v0.3.5 F5：后台执行关键词 patch + 人设 regenerate（不阻塞 apply 响应）。

        复用 ``_apply_keywords_patch``（已改为 batch_update 单次重算）与
        ``_bg_regenerate_persona``。
        """
        try:
            if keywords_patch:
                try:
                    await self._apply_keywords_patch(keywords_patch)
                except Exception as e:
                    self._log("warning", f"后台 _apply_keywords_patch 失败: {e}")
            if regenerate_needed:
                try:
                    await self._bg_regenerate_persona()
                except Exception as e:
                    self._log("warning", f"后台 _bg_regenerate_persona 失败: {e}")
        except Exception as e:
            self._log("warning", f"_bg_apply_keywords_and_regenerate 失败: {e}")

    async def _apply_keywords_patch(self, keywords_patch: dict) -> int:
        """v0.3.5 F5：应用关键词增删 patch（批量重算，从 N 次嵌入 API 降到 1 次）。

        结构：``{"add": [{kind, label, text}], "remove": [...]}``，
        kind ∈ ``example``|``high_keyword``|``hate_keyword``。
        调 ``interest_mgr.batch_update`` 批量内存操作 + 单次 ``_recompute_centroids``。
        完成后调 ``apply_rejected`` 兜底过滤。返回成功操作项数。
        """
        if not isinstance(keywords_patch, dict):
            return 0
        embed_fn = self._embed_fn
        if embed_fn is None:
            return 0
        adds = list(keywords_patch.get("add") or [])
        removes = list(keywords_patch.get("remove") or [])
        if not adds and not removes:
            return 0
        try:
            count, msg = await self.interest_mgr.batch_update(adds, removes, embed_fn)
            if msg:
                self._log("warning", f"keywords_patch batch_update 部分失败: {msg}")
        except Exception as e:
            self._log("warning", f"keywords_patch batch_update 异常: {e}")
            return 0
        # 重算质心确保 rejected 列表生效（batch_update 已重算，此步兜底过滤）
        try:
            await self.interest_mgr.apply_rejected(embed_fn)
        except Exception as e:
            self._log("warning", f"keywords_patch apply_rejected 失败: {e}")
        return count

    async def _autotune_trigger(self, force: bool = False) -> dict:
        """v0.3.5 F4：scheduler 自动触发回调。

        force=False（默认）：走普通 allow 速率限制（用于手动 /prosocial tune 触发场景）。
        force=True：跳过 allow 速率限制，但仍 record（用于自动触发修复限流 bug）。
        强制触发额外受 force_history 独立冷却防抖（autotune_force_cooldown_hours）。

        调 ``llm_autotune("analyze", force=force)``；若 ``autotune_auto_apply=true`` 则成功后
        调 ``llm_autotune("apply", force=True)``。失败/被限写日志，不抛异常。
        """
        try:
            # v0.3.5 F4：强制触发路径——独立冷却防抖
            if force:
                now = time.time()
                cfg = self._config_getter()
                force_cooldown = float(cfg.get("autotune_force_cooldown_hours", 1.0))
                if not self._tune_limiter.allow_force(now, force_cooldown):
                    self._log(
                        "info",
                        f"[ProSocial] autotune_force_skipped: force_cooldown ({force_cooldown}h)",
                    )
                    return {"ok": False, "error": "force_cooldown"}
                # 允许强制触发，记录 force_history
                self._tune_limiter.record_force(now)

            result = await self.llm_autotune("analyze", force=force)
            if result.get("ok") and self._config_getter().get(
                "autotune_auto_apply", False
            ):
                try:
                    apply_result = await self.llm_autotune("apply", force=True)
                    result["apply_result"] = apply_result
                except Exception as e:
                    self._log("warning", f"_autotune_trigger auto_apply 失败: {e}")
                    result["apply_error"] = str(e)
            if not result.get("ok"):
                if result.get("error") == "rate_limited":
                    self._log("info", "[ProSocial] autotune_skipped: rate_limited")
                elif result.get("error") == "force_cooldown":
                    self._log(
                        "info", "[ProSocial] autotune_force_skipped: force_cooldown"
                    )
                else:
                    self._log(
                        "warning",
                        f"[ProSocial] autotune 失败: {result.get('error')}",
                    )
            return result
        except Exception as e:
            self._log("warning", f"_autotune_trigger 失败: {e}")
            return {"ok": False, "error": str(e)}

    def _build_tune_prompt(
        self, stats: dict, *, style: str = "", guidance: str = ""
    ) -> str:
        """v0.2.9 F1：LLM 全视野调参 prompt。

        注入：全量配置（减 DENYLIST）+ 兴趣数据 + 人设 + schedule + 群白名单 +
        adaptive 状态 + provider 名称 + 决策统计 + 风格偏好 + 用户指导。
        输出格式含三段：suggested_patch / suggested_keywords_patch / persona_revision。
        """
        current_cfg = self._tune_current_config()
        full_cfg = self._config_getter()

        # v0.2.9 F1：provider 名称解析（chat_provider_id / embedding_provider_id）
        chat_prov_name = "（默认）"
        chat_pid = str(full_cfg.get("chat_provider_id", "") or "")
        if chat_pid:
            try:
                prov = self.context.get_provider_by_id(chat_pid)
                if prov is not None:
                    chat_prov_name = str(prov.meta().name or chat_pid)
                else:
                    chat_prov_name = f"（未找到 {chat_pid}）"
            except Exception:
                chat_prov_name = chat_pid
        embed_prov_name = "（默认）"
        embed_pid = str(full_cfg.get("embedding_provider_id", "") or "")
        if embed_pid:
            try:
                prov = self.context.get_provider_by_id(embed_pid)
                if prov is not None:
                    embed_prov_name = str(prov.meta().name or embed_pid)
                else:
                    embed_prov_name = f"（未找到 {embed_pid}）"
            except Exception:
                embed_prov_name = embed_pid

        # v0.2.9 F1：兴趣数据 export_view（items/hate_keywords/high_interest_keywords/rejected）
        interest_view = self.interest_mgr.export_view()

        # v0.2.9 F1：adaptive 摘要（每群 mult/window_rate/samples）
        adaptive_summary = stats.get("adaptive_summary", []) or []

        # 风格偏好 + 用户指导
        style_key = style if style in _STYLE_GUIDANCE else "balanced"
        style_text = _STYLE_GUIDANCE[style_key]
        user_guidance = guidance.strip() if guidance else "（用户未提供补充说明）"

        # DENYLIST 与可写键说明
        denylist_str = ", ".join(sorted(TUNE_DENYLIST))
        writable_count = len(self._writable_keys())

        return (
            "# 角色与任务\n"
            "你是「主动社交插件」的调参专家。请基于机器人完整画像、近期决策数据"
            "和用户偏好，给出整体性调参建议。\n\n"
            "# 机器人画像\n\n"
            f"**人设文本（persona_text）**：\n{full_cfg.get('persona_text', '')}\n\n"
            f"**补充知识（persona_knowledge）**：\n{full_cfg.get('persona_knowledge', '')}\n\n"
            "# Provider 信息\n\n"
            f"- Chat provider: {chat_prov_name}\n"
            f"- Embedding provider: {embed_prov_name}\n\n"
            "# 插件工作原理\n\n"
            "这是一个群聊主动社交插件。机器人监听群消息，通过双通道融合评分"
            "决定是否主动插话：\n\n"
            "## 评分管线\n\n"
            "1. **向量通道（embedding）**：计算近期消息与兴趣关键词的余弦相似度\n"
            "   - s_int：兴趣相关度（消息 vs 兴趣向量质心）\n"
            "   - s_topic：话题连续度（消息 vs 上下文摘要）\n"
            "   - s_resp：回应匹配度（消息是否像在@机器人或接话）\n\n"
            "2. **规则通道（rule）**：基于规则的启发式因子\n"
            "   - c_cooldown：冷却因子（距上次发言的条数，越远越可能触发）\n"
            "   - p_silence：沉默时长因子（群内安静越久越可能触发）\n\n"
            "3. **融合公式**：\n"
            "   final_score = w_int×s_int + w_topic×s_topic + w_resp×s_resp +\n"
            "                 w_cooldown×c_cooldown + w_silence×p_silence + modifiers\n"
            "   modifiers = core_interest_modifier（核心兴趣加成）+\n"
            "               edge_interest_modifier（边缘兴趣加成）+\n"
            "               expecting_modifier（被@/接话加成）\n\n"
            "4. **阈值与触发**：\n"
            "   effective_threshold = base_threshold × fatigue_mult × inertia_mult × adaptive_mult\n"
            "   final_score >= effective_threshold → 触发主动回复\n\n"
            "## 反馈机制\n\n"
            "5. **疲劳控制**：每次主动发送消耗疲劳值，疲劳越高 threshold 越高（越难触发）\n"
            "   - fatigue_cost_active：主动回复消耗\n"
            "   - fatigue_cost_passive：被动回复消耗\n"
            "   - fatigue_limit：疲劳上限（到顶后几乎不触发）\n\n"
            "6. **个人跟踪**：对特定用户的关键词触发单独判定（personal_threshold）\n\n"
            "7. **频率兜底**：max_proactive_per_hour / max_proactive_per_day 超限后\n"
            '   suppressed_reason="quota"（调参失误的最终防线）\n\n'
            "8. **自适应阈值**：adaptive_threshold_enabled=true 时，按近期触发率\n"
            "   自动调整 multiplier——触发率>30% 则 mult×1.1 收紧，<5% 则 mult×0.9 放宽，\n"
            "   mult 钳制 [0.5, 2.0]。5%-30% 为健康触发率区间。\n\n"
            "# 兴趣数据（export_view，含 items/hate_keywords/high_interest_keywords/rejected）\n\n"
            f"{json.dumps(interest_view, ensure_ascii=False, indent=2)}\n\n"
            "# 作息 schedule\n\n"
            f"{json.dumps(full_cfg.get('schedule', []), ensure_ascii=False, indent=2)}\n\n"
            "# 群范围\n\n"
            f"- group_mode: {full_cfg.get('group_mode', 'whitelist')}\n"
            f"- group_whitelist: {full_cfg.get('group_whitelist', [])}\n\n"
            "# 自适应阈值状态（每群 mult/window_rate/samples）\n\n"
            f"{json.dumps(adaptive_summary, ensure_ascii=False, indent=2)}\n\n"
            "# 对话状态摘要（v0.3.5 F6）\n\n"
            f"{json.dumps(stats.get('conversation_state_summary', {}), ensure_ascii=False, indent=2)}\n\n"
            "# 近期决策数据统计（最近 200 条）\n\n"
            f"{json.dumps(stats, ensure_ascii=False, indent=2)}\n\n"
            "# 当前全量配置（除 DENYLIST 外均可改）\n\n"
            f"{json.dumps(current_cfg, ensure_ascii=False, indent=2)}\n\n"
            "# 用户偏好\n\n"
            f"**回复风格**：{style_text}\n\n"
            f"**用户补充说明**：\n{user_guidance}\n\n"
            "# 可写键说明\n\n"
            f"可写键 = 全量配置减 DENYLIST（共 {writable_count} 项）。\n"
            f"DENYLIST（安全敏感键，不可改）：{denylist_str}\n"
            "另外可输出：\n"
            "- suggested_keywords_patch：兴趣关键词增删\n"
            "- persona_revision：人设改写（可选，仅当人设本身需要调整时）\n\n"
            "# 分析要求\n\n"
            "请基于机器人完整画像做整体性调参建议：\n\n"
            "1. **触发率**：当前 triggered_rate 是否在健康区间？与用户风格偏好的目标区间对比。\n"
            "   偏高→收紧阈值/降权重；偏低→放宽。注意区分 below_threshold 和 quota 抑制。\n\n"
            "2. **得分分布**：score_mean vs threshold_mean 的差距是否合理？\n"
            "   差距过大（分数远低于阈值）→ 阈值偏高或权重不足；\n"
            "   差距过小（分数接近阈值）→ 触发过于敏感，波动大。\n\n"
            "3. **五因子均衡**：factors_mean 中 s_int/s_topic/s_resp/c_cooldown/p_silence\n"
            "   是否有某因子主导？主导因子意味着该通道权重失衡，应调低对应 w_* 或调高其他。\n\n"
            "4. **抑制分布**：suppressed_hist 中 quota 占比高→频率上限过低或阈值过低导致\n"
            "   频繁触发后被配额拦截；below_threshold 占比高→阈值过高。\n\n"
            "5. **疲劳状态**：fatigue_value_mean 是否接近 fatigue_limit？\n"
            "   接近→疲劳消耗过快或恢复太慢，机器人会逐渐沉默。\n\n"
            "6. **兴趣数据**：兴趣 items 是否合理？是否需要增删关键词？\n"
            "   人设文本是否需要调整？（仅必要时输出 persona_revision）\n\n"
            "7. **对话状态**：conversation_state_summary 中 avg_appropriateness 是否合理？\n"
            "   is_argument/is_monologue 占比高→机器人应更克制（modifier>1）；\n"
            "   has_question/bot_turn 占比高→机器人可更活跃（modifier<1）。\n\n"
            "8. **风格对齐**：建议方向必须与用户回复风格偏好一致。\n\n"
            "# 输出格式\n\n"
            "仅输出严格 JSON（不要 ```json 标记），含三段：\n"
            "{\n"
            '  "analysis": "对当前参数与决策数据的诊断分析（必填，中文，逐维度点评，引用具体数值）",\n'
            '  "suggested_patch": {"配置键": 新值, ...},  // 标量配置；可写键 = 全量配置减 DENYLIST 6 项\n'
            '  "suggested_keywords_patch": {  // 可选，兴趣关键词增删\n'
            '    "add": [{"kind": "example"|"high_keyword"|"hate_keyword", "label": "core"|"general"|"marginal"|"hate", "text": "..."}],\n'
            '    "remove": [{"kind": ..., "label": ..., "text": "..."}]\n'
            "  },\n"
            '  "persona_revision": "可选，仅当人设本身需要调整时输出新人设文本",\n'
            '  "expected_effect": "应用建议后的预期效果（必填）"\n'
            "}\n\n"
            "仅输出 JSON，不要其他文本。"
        )

    def _tune_current_config(self) -> dict:
        """v0.2.9 F1：返回当前全量配置减 DENYLIST（供 LLM 诊断时对照当前参数）。"""
        cfg = self._config_getter()
        return {k: v for k, v in cfg.items() if k not in TUNE_DENYLIST}

    def _format_tune_status(self) -> str:
        """v0.2.9 F5：格式化调参状态信息（``/prosocial tune status``）。"""
        cfg = self._config_getter()
        now = time.time()
        cooldown = float(cfg.get("autotune_cooldown_hours", 3.0))
        max_per_day = int(cfg.get("autotune_max_per_day", 4))
        try:
            state = self._tune_limiter.state()
        except Exception:
            state = {"history": [], "last_call": None}
        history = state.get("history") or []
        last_call = state.get("last_call")
        used = len([t for t in history if t >= now - 86400])
        next_available = 0.0
        if last_call is not None and cooldown > 0:
            next_available = max(0.0, last_call + cooldown * 3600 - now)

        lines = [
            "📋 LLM 调参状态",
            f"速率限制：今日已用 {used}/{max_per_day}",
        ]
        if next_available > 0:
            hours = int(next_available // 3600)
            minutes = int((next_available % 3600) // 60)
            lines.append(f"下次可用：{hours}小时{minutes}分钟后")
        else:
            lines.append("下次可用：现在")

        auto_trigger = bool(cfg.get("autotune_auto_trigger_enabled", True))
        auto_apply = bool(cfg.get("autotune_auto_apply", False))
        lines.append(f"自动触发：{'开启' if auto_trigger else '关闭'}")
        lines.append(f"自动应用：{'开启' if auto_apply else '关闭'}")

        cached = self._last_tune_suggestion
        if isinstance(cached, dict) and cached:
            patch = cached.get("suggested_patch", {}) or {}
            lines.append("")
            lines.append(f"上次建议（缓存 {len(patch)} 项标量）：")
            for k, v in list(patch.items())[:5]:
                lines.append(f"  {k}: {v}")
            if len(patch) > 5:
                lines.append(f"  ...（共 {len(patch)} 项）")
            if cached.get("suggested_keywords_patch"):
                lines.append("（含关键词增删建议）")
            if cached.get("persona_revision"):
                lines.append("（含人设改写建议）")
        else:
            lines.append("")
            lines.append("上次建议：无缓存")

        return "\n".join(lines)

    @staticmethod
    def _parse_tune_response(raw: str) -> dict | None:
        """解析 LLM 返回的 JSON（容错 ```json ... ``` fence 与首尾空白）。"""
        if not raw:
            return None
        text = raw.strip()
        # 剥 ```json ... ``` / ``` ... ``` fence
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
