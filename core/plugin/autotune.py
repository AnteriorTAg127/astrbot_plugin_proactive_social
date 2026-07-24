"""模块 C：LLM 诊断调参（autotune）—— TuneMixin。

职责：LLM 驱动的整体性调参（analyze / apply），维护 ``TUNE_DENYLIST`` 安全约束
与 ``_STYLE_GUIDANCE`` 风格引导，协调 ``ConfigStore`` 标量写入、
``InterestManager`` 关键词增删、人设变更触发的后台兴趣重建，
提供 ``TuneRateLimiter`` 速率限制状态查询。

依赖的实例属性（由 ``ProSocialPlugin`` / 其他 mixin 提供，经 MRO 在 ``self`` 上访问）：
``self.scheduler``、``self._llm_fn`` / ``self._embed_fn``、``self._config_getter()``、
``self._config_store``、``self.interest_mgr``、``self._tune_limiter``、
``self._last_tune_suggestion``、``self._tune_history``、``self._log(level, msg)``、
``self.context``。
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
        source: str = "manual",
        record_id: int | None = None,
        approved_by: str | None = None,
    ) -> dict:
        """v0.2.9 F1/F2/F4：LLM 诊断调参核心（全视野 + denylist + 速率限制）。

        - ``analyze``：``collect_tune_stats`` → 构造 prompt → ``_llm_fn`` → 解析 JSON →
          DENYLIST 过滤 → 缓存到 ``_last_tune_suggestion``（含三段）→ ``record()`` 计入配额。
        - ``apply``：v0.3.10 重构——``record_id`` 非 None 时从历史记录取 plan → patch
          （新路径），否则从参数或缓存取（旧路径，缓存可能含 ``record_id`` 自动走新路径）；
          persona_revision 合并入 persona_text → DENYLIST 过滤 → 记录 ``pre_apply_values``
          快照 → ``ConfigStore.set_many`` → 记录 ``applied_values`` 快照 / 人设变更触发
          后台 regenerate / keywords_patch 走 ``interest_mgr`` 增删 + ``apply_rejected``；
          新路径调 ``record_apply`` 更新历史，旧路径沿用 ``mark_applied``，apply 失败时
          更新历史 ``status=failed``。
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

            # v0.3.10：两轮模式开关
            two_phase = bool(cfg.get("autotune_two_phase_enabled", False))

            if two_phase:
                # 第一轮：只输出 diagnosis
                prompt = self._build_tune_prompt(
                    stats, style=style, guidance=guidance, phase="diagnosis"
                )
                try:
                    raw = await self._llm_fn(prompt)
                except Exception as e:
                    return {"ok": False, "error": f"LLM 调用失败(诊断轮): {e}"}
                parsed = self._parse_tune_response(raw)
                if not parsed:
                    return {"ok": False, "error": "LLM 输出解析失败（非 JSON）"}
                diagnosis = str(parsed.get("diagnosis", "") or "")
                if not diagnosis:
                    return {"ok": False, "error": "LLM 未输出 diagnosis"}
                # record status='pending_diagnosis'
                record_id = None
                try:
                    record_id = await self._tune_history.record(
                        action="analyze",
                        source=source,
                        patch={},
                        keywords_patch=None,
                        persona_revision=None,
                        analysis=diagnosis,
                        expected_effect="",
                        applied=False,
                        original_values=None,
                        diagnosis=diagnosis,
                        plan=None,
                        status="pending_diagnosis",
                        error_msg=None,
                    )
                except Exception as e:
                    self._log("warning", f"调参历史记录失败(诊断轮): {e}")
                # 缓存 diagnosis 供第二轮（兼容字段同时填入）
                self._last_tune_suggestion = {
                    "diagnosis": diagnosis,
                    "record_id": record_id,
                    "phase": "diagnosis_only",
                }
                self._tune_limiter.record(now)
                return {
                    "ok": True,
                    "two_phase": True,
                    "phase": "diagnosis",
                    "diagnosis": diagnosis,
                    "analysis": diagnosis,  # 向后兼容
                    "plan": None,
                    "record_id": record_id,
                    "applied": False,
                    "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
                    "message": "诊断已完成，请在历史页点「基于此诊断调参」触发第二轮",
                }

            # 单轮模式（默认）：一次输出 diagnosis + plan
            prompt = self._build_tune_prompt(
                stats, style=style, guidance=guidance, phase="full"
            )
            try:
                raw = await self._llm_fn(prompt)
            except Exception as e:
                return {"ok": False, "error": f"LLM 调用失败: {e}"}
            parsed = self._parse_tune_response(raw)
            if not parsed:
                return {"ok": False, "error": "LLM 输出解析失败（非 JSON）"}

            diagnosis = str(parsed.get("diagnosis", "") or "")
            plan = parsed.get("plan") or []
            if not isinstance(plan, list):
                plan = []

            # v0.3.10 T4：校验 plan
            current_cfg = self._config_getter()

            # 旧格式向后兼容：plan 全部项缺 reason/expected_effect_quant 时，
            # 视为旧格式（mock 测试或旧 LLM 输出），不做严格校验，仅 DENYLIST 过滤
            is_new_format = bool(plan) and any(
                isinstance(item, dict)
                and (item.get("reason") or item.get("expected_effect_quant"))
                for item in plan
            )

            # 从 plan 中提取被 DENYLIST 过滤的键（两路共用）
            denylist_dropped = [
                str(item.get("key", ""))
                for item in plan
                if isinstance(item, dict) and item.get("key") in TUNE_DENYLIST
            ]

            original_values: dict = {}
            if is_new_format:
                validated_plan, errors = self._validate_plan(plan, current_cfg)
                filtered = {item["key"]: item["suggested"] for item in validated_plan}
                if denylist_dropped:
                    errors.append(f"DENYLIST 键已过滤: {', '.join(denylist_dropped)}")
                # original_values 快照（来自 validated_plan 中保留的 original）
                for item in validated_plan:
                    key = item.get("key")
                    orig = item.get("original")
                    if key and orig is not None:
                        original_values[key] = orig
            else:
                # 旧格式 / 空 plan：DENYLIST 过滤 + 基本类型保留（向后兼容 v0.2.9 测试）
                validated_plan = []
                errors = []
                filtered = {}
                for item in plan:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key")
                    if not key or key in TUNE_DENYLIST:
                        continue
                    filtered[key] = item.get("suggested")
                    validated_plan.append(item)

            suggested_keywords = parsed.get("suggested_keywords_patch") or None
            persona_rev = parsed.get("persona_revision") or None
            expected_effect = str(parsed.get("expected_effect_overall", "") or "")

            # analysis 向后兼容：diagnosis + DENYLIST 过滤注明
            analysis = diagnosis
            if denylist_dropped:
                analysis = (analysis or "") + (
                    f"\n\n[已过滤安全敏感键: {', '.join(denylist_dropped)}]"
                )

            # 缓存建议（向后兼容 + 新字段）
            self._last_tune_suggestion = {
                "suggested_patch": filtered,  # 向后兼容 apply 分支
                "suggested_keywords_patch": suggested_keywords,
                "persona_revision": persona_rev,
                # v0.3.10 新字段
                "diagnosis": diagnosis,
                "plan": validated_plan,
                "errors": errors,
            }

            # analyze 成功后 record（计入配额；force=True 也 record）
            self._tune_limiter.record(now)

            # record 到历史（写入新字段）
            record_id = None
            try:
                record_id = await self._tune_history.record(
                    action="analyze",
                    source=source,
                    patch=filtered,
                    keywords_patch=suggested_keywords,
                    persona_revision=persona_rev,
                    analysis=analysis,  # 旧字段向后兼容
                    expected_effect=expected_effect,
                    applied=False,
                    # v0.3.10 新字段
                    original_values=original_values if original_values else None,
                    diagnosis=diagnosis,
                    plan=validated_plan if validated_plan else None,
                    status="pending" if validated_plan else "failed",
                    error_msg="; ".join(errors) if errors else None,
                )
            except Exception as e:
                self._log("warning", f"调参历史记录失败(analyze): {e}")

            # v0.3.10：参数级去重（analyze 后立即去重）
            if record_id is not None and validated_plan:
                try:
                    new_keys = [item["key"] for item in validated_plan]
                    await self._tune_history.dedupe_pending(record_id, new_keys)
                except Exception as e:
                    self._log("warning", f"参数级去重失败: {e}")

            return {
                "ok": True,
                "diagnosis": diagnosis,
                "analysis": analysis,  # 向后兼容
                "plan": validated_plan,
                "errors": errors,
                "suggested_patch": filtered,  # 向后兼容
                "suggested_keywords_patch": suggested_keywords,
                "persona_revision": persona_rev,
                "expected_effect": expected_effect,  # 向后兼容
                "expected_effect_overall": expected_effect,
                "applied": False,
                "record_id": record_id,
                "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
            }

        if action == "apply":
            # v0.3.10：优先从 record_id 取 patch（新路径）
            target_record_id = record_id
            if target_record_id is not None:
                # 新路径：从历史记录取
                try:
                    rec = await self._tune_history.get_by_id(target_record_id)
                except Exception as e:
                    return {
                        "ok": False,
                        "applied": False,
                        "error": f"查询历史记录失败: {e}",
                    }
                if not rec:
                    return {
                        "ok": False,
                        "applied": False,
                        "error": f"记录 {target_record_id} 不存在",
                    }
                status = rec.get("status")
                if status not in ("pending", "pending_diagnosis"):
                    return {
                        "ok": False,
                        "applied": False,
                        "error": (
                            f"记录 {target_record_id} 状态非 pending"
                            f"（当前: {status}），无法 apply"
                        ),
                    }
                # 从 plan 提取 patch
                plan = rec.get("plan") or []
                patch = {
                    item["key"]: item["suggested"]
                    for item in plan
                    if isinstance(item, dict) and item.get("key")
                }
                if not keywords_patch:
                    keywords_patch = rec.get("keywords_patch")
                if not persona_revision:
                    persona_revision = rec.get("persona_revision")
            else:
                # 旧路径：从缓存或参数取
                if patch is None:
                    cached = self._last_tune_suggestion or {}
                    if isinstance(cached, dict):
                        patch = cached.get("suggested_patch", {}) or {}
                        if not keywords_patch:
                            keywords_patch = cached.get("suggested_keywords_patch")
                        if not persona_revision:
                            persona_revision = cached.get("persona_revision")
                        # v0.3.10：从缓存取 record_id（若 analyze 时缓存了）
                        target_record_id = cached.get("record_id")
                    else:
                        patch = {}

            # persona_revision 合并入 persona_text 走同一路径
            if persona_revision:
                patch = dict(patch)
                patch["persona_text"] = persona_revision
            if not patch and not keywords_patch:
                return {"ok": False, "applied": False, "error": "无可应用的 patch"}

            # v0.2.9 F2 / v0.3.10：DENYLIST 过滤
            filtered = {
                k: v for k, v in patch.items() if k not in TUNE_DENYLIST
            }
            dropped = [k for k in patch if k in TUNE_DENYLIST]

            # v0.3.10：pre_apply 快照（apply 前的当前配置值，用于历史展示「原始值」）
            pre_apply_values: dict = {}
            if filtered:
                current_cfg_snapshot = self._config_store.snapshot()
                pre_apply_values = {
                    k: current_cfg_snapshot.get(k) for k in filtered
                }

            # 标量写入（ConfigStore.set_many 内置类型/范围校验，DENYLIST 已过滤）
            ok, msg = (True, "")
            if filtered:
                ok, msg = await self._config_store.set_many(filtered)
            if not ok:
                # v0.3.10：apply 失败时更新历史状态为 failed
                if target_record_id is not None:
                    try:
                        await self._tune_history.update_status(
                            target_record_id, "failed", approved_by=approved_by
                        )
                    except Exception:
                        pass
                return {
                    "ok": False,
                    "applied": False,
                    "error": msg,
                    "rate_limit": self._rate_limit_status(
                        now, cooldown, max_per_day
                    ),
                }

            # v0.3.10：applied_values 快照（apply 后的实际值）
            applied_values: dict = {}
            if filtered:
                new_cfg_snapshot = self._config_store.snapshot()
                applied_values = {k: new_cfg_snapshot.get(k) for k in filtered}

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
                            keywords_patch=keywords_patch
                            if keywords_patch
                            else None,
                            regenerate_needed=regenerate_needed,
                        )
                    )
                except Exception as e:
                    self._log("warning", f"启动后台 apply 任务失败: {e}")

            # v0.3.10：更新历史记录
            if target_record_id is not None:
                # 新路径：record_apply 更新 pre_apply/applied_values/status/approved_by
                try:
                    await self._tune_history.record_apply(
                        target_record_id,
                        pre_apply_values=pre_apply_values,
                        applied_values=applied_values,
                        approved_by=approved_by or source,
                    )
                except Exception as e:
                    self._log("warning", f"record_apply 失败: {e}")
            else:
                # 旧路径：沿用 mark_applied 逻辑
                try:
                    marked = await self._tune_history.mark_applied(source)
                    if not marked:
                        await self._tune_history.record(
                            action="apply",
                            source=source,
                            patch=filtered,
                            keywords_patch=keywords_patch,
                            persona_revision=persona_revision,
                            analysis="",
                            expected_effect="",
                            applied=True,
                        )
                except Exception as e:
                    self._log("warning", f"调参历史记录失败(apply): {e}")

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
                "record_id": target_record_id,
                "pre_apply_values": pre_apply_values,
                "applied_values": applied_values,
                "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
            }

        return {"ok": False, "error": f"未知 action: {action}"}

    async def llm_autotune_plan(
        self,
        record_id: int,
        *,
        style: str = "",
        guidance: str = "",
        source: str = "manual",
    ) -> dict:
        """v0.3.10 T5：两轮模式第二轮——基于已生成的 diagnosis 输出 plan。

        不单独计速率限制（第一轮已计）。

        Args:
            record_id: 第一轮 analyze 产生的 ``pending_diagnosis`` 记录 id
            style/guidance: 用户偏好与补充说明（与第一轮一致）
            source: 三值 manual/auto/force（仅用于日志，不写表）
        """
        if self.scheduler is None or self._llm_fn is None:
            return {"ok": False, "error": "scheduler 或 llm_fn 未就绪"}

        # 从历史取 diagnosis
        try:
            record = await self._tune_history.get_by_id(record_id)
        except Exception as e:
            return {"ok": False, "error": f"查询历史记录失败: {e}"}
        if not record:
            return {"ok": False, "error": f"记录 {record_id} 不存在"}
        if record.get("status") != "pending_diagnosis":
            return {
                "ok": False,
                "error": (
                    f"记录 {record_id} 状态非 pending_diagnosis"
                    f"（当前: {record.get('status')}）"
                ),
            }

        diagnosis = record.get("diagnosis") or ""
        if not diagnosis:
            return {"ok": False, "error": "记录无 diagnosis 内容"}

        stats = self.scheduler.collect_tune_stats()
        prompt = self._build_tune_prompt(
            stats,
            style=style,
            guidance=guidance,
            phase="plan",
            diagnosis_text=diagnosis,
        )
        try:
            raw = await self._llm_fn(prompt)
        except Exception as e:
            return {"ok": False, "error": f"LLM 调用失败(方案轮): {e}"}
        parsed = self._parse_tune_response(raw)
        if not parsed:
            return {"ok": False, "error": "LLM 输出解析失败（非 JSON）"}

        plan = parsed.get("plan") or []
        if not isinstance(plan, list):
            plan = []

        # 校验
        cfg = self._config_getter()
        validated_plan, errors = self._validate_plan(plan, cfg)
        filtered = {item["key"]: item["suggested"] for item in validated_plan}

        original_values: dict = {}
        for item in validated_plan:
            key = item.get("key")
            orig = item.get("original")
            if key and orig is not None:
                original_values[key] = orig

        suggested_keywords = parsed.get("suggested_keywords_patch") or None
        persona_rev = parsed.get("persona_revision") or None
        expected_effect = str(parsed.get("expected_effect_overall", "") or "")

        # 更新历史记录：写入 plan + status='pending'
        try:
            await self._tune_history.update_plan(record_id, validated_plan)
        except Exception as e:
            self._log("warning", f"更新 plan 失败: {e}")

        # 参数级去重
        if validated_plan:
            try:
                new_keys = [item["key"] for item in validated_plan]
                await self._tune_history.dedupe_pending(record_id, new_keys)
            except Exception as e:
                self._log("warning", f"参数级去重失败: {e}")

        # 缓存建议（向后兼容 apply 分支）
        self._last_tune_suggestion = {
            "suggested_patch": filtered,
            "suggested_keywords_patch": suggested_keywords,
            "persona_revision": persona_rev,
            "diagnosis": diagnosis,
            "plan": validated_plan,
            "errors": errors,
            "record_id": record_id,
        }

        return {
            "ok": True,
            "two_phase": True,
            "phase": "plan",
            "diagnosis": diagnosis,
            "analysis": diagnosis,  # 向后兼容
            "plan": validated_plan,
            "errors": errors,
            "suggested_patch": filtered,  # 向后兼容
            "suggested_keywords_patch": suggested_keywords,
            "persona_revision": persona_rev,
            "expected_effect": expected_effect,  # 向后兼容
            "expected_effect_overall": expected_effect,
            "applied": False,
            "record_id": record_id,
        }

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

        v0.3.7 安全加固：
        - text 字段强制 str 转换（防止 LLM 输出 dict/list/number 导致 [object Object]）
        - add/remove 交叉去重（同一 text 同时出现时优先 remove，不 add）
        - add 内部按 (kind, text) 去重
        - 无效项（非 dict / kind 非法 / text 空）静默跳过
        """
        if not isinstance(keywords_patch, dict):
            return 0
        embed_fn = self._embed_fn
        if embed_fn is None:
            return 0
        raw_adds = keywords_patch.get("add") or []
        raw_removes = keywords_patch.get("remove") or []
        if not isinstance(raw_adds, list):
            raw_adds = []
        if not isinstance(raw_removes, list):
            raw_removes = []

        valid_kinds = ("example", "high_keyword", "hate_keyword")

        def _normalize(items: list) -> list[dict]:
            """规范化每项：强制 text 为 str，过滤无效项，内部去重。"""
            seen: set[tuple[str, str]] = set()
            result: list[dict] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind")
                if kind not in valid_kinds:
                    continue
                # text 强制 str：dict/list 转 repr，number 转 str，None 转 ""
                raw_text = item.get("text", "")
                if isinstance(raw_text, (dict, list)):
                    text = str(raw_text)
                elif raw_text is None:
                    continue
                else:
                    text = str(raw_text)
                text = text.strip()
                if not text:
                    continue
                label = str(item.get("label", "") or "")
                key = (kind, text)
                if key in seen:
                    continue
                seen.add(key)
                result.append({"kind": kind, "label": label, "text": text})
            return result

        adds = _normalize(raw_adds)
        removes = _normalize(raw_removes)

        # 交叉去重：同一 (kind, text) 同时在 add 和 remove 中 → 优先 remove，不 add
        remove_keys = {(r["kind"], r["text"]) for r in removes}
        adds = [a for a in adds if (a["kind"], a["text"]) not in remove_keys]

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

            result = await self.llm_autotune("analyze", force=force, source="auto")
            if result.get("ok") and self._config_getter().get(
                "autotune_auto_apply", False
            ):
                try:
                    apply_result = await self.llm_autotune(
                        "apply", force=True, source="auto"
                    )
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
        self,
        stats: dict,
        *,
        style: str = "",
        guidance: str = "",
        phase: str = "full",
        diagnosis_text: str | None = None,
    ) -> str:
        """v0.3.10 T3：LLM 全视野调参 prompt（18 段结构 + 两段式输出）。

        按 spec.md A.6 节 18 段结构构造 prompt，注入：
        全量配置（减 DENYLIST）+ 全量 VALIDATORS 范围表 + 五步 CoT 引导 +
        兴趣数据 + 人设 + schedule + 群白名单 + adaptive 状态 + provider 名称 +
        决策统计 + 风格偏好 + 用户指导 + 调参约束 + 两段式输出格式。

        phase 参数（spec A.5）控制两轮模式：
        - 'full'（默认）：单轮模式，要求一次输出完整 {diagnosis, plan, ...}
        - 'diagnosis'：第一轮，只要求输出 {diagnosis: "..."}
        - 'plan'：第二轮，注入已生成 diagnosis（diagnosis_text），要求输出 {plan: [...]}
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

        # v0.3.10 T3：调参约束配置（autotune_max_change_ratio / autotune_max_params_per_tune）
        max_change_ratio = float(full_cfg.get("autotune_max_change_ratio", 0.3))
        max_params = int(full_cfg.get("autotune_max_params_per_tune", 5))

        # v0.3.10 T3：第 3 段「# 参数范围参考」——遍历 VALIDATORS 生成表格
        # 用 helper 局部函数生成，避免主 return 字符串过长
        def _build_param_range_section() -> str:
            type_names = {bool: "bool", int: "int", float: "float"}
            lines = [
                "| 键名 | 类型 | 下限 | 上限 |",
                "|------|------|------|------|",
            ]
            for key in sorted(ConfigStore.VALIDATORS.keys()):
                typ, lo, hi = ConfigStore.VALIDATORS[key]
                type_name = type_names.get(typ, str(typ))
                lo_str = "—" if lo is None else str(lo)
                hi_str = "—" if hi is None else str(hi)
                lines.append(f"| {key} | {type_name} | {lo_str} | {hi_str} |")
            # 无 VALIDATORS 规则的可写键（排除 LIST_KEYS / DENYLIST / schedule / group_mode）
            no_rule_keys = sorted(
                k
                for k in ConfigStore.DEFAULT_CONFIG
                if k not in ConfigStore.VALIDATORS
                and k not in ConfigStore.LIST_KEYS
                and k not in TUNE_DENYLIST
                and k not in ("schedule", "group_mode")
            )
            lines.append("")
            lines.append("**无 VALIDATORS 规则的键（无范围限制）：**")
            lines.append(", ".join(no_rule_keys))
            return "\n".join(lines)

        param_range_table = _build_param_range_section()

        # v0.3.10 T3：18 段结构按 spec.md A.6 顺序拼接
        sections: list[str] = []

        # 1. 角色与任务
        sections.append(
            "# 角色与任务\n"
            "你是「主动社交插件」的调参专家。请基于机器人完整画像、近期决策数据"
            "和用户偏好，给出整体性调参建议。"
        )

        # 2. 思考流程（五步 CoT 引导，新增，按 spec.md A.3 完整复制）
        sections.append(
            "# 思考流程（请在 diagnosis 字段中按以下五步展开）\n\n"
            "## 第一步：观察数据\n"
            "- 列出关键统计指标：triggered_rate / score_mean / threshold_mean / fatigue_value_mean\n"
            "- 列出五因子均值：s_int / s_topic / s_resp / c_cooldown / p_silence\n"
            "- 列出抑制分布：below_threshold / quota / min_interval 占比\n"
            "- 列出 adaptive_summary：每群 mult / window_rate / samples\n"
            "- 列出对话状态：avg_appropriateness / has_question / is_argument 占比\n\n"
            "## 第二步：诊断问题\n"
            "- 触发率是否在健康区间 [5%, 30%]？与用户风格目标区间对比\n"
            "- score_mean 与 threshold_mean 差距是否合理（过大→阈值偏高，过小→过于敏感）\n"
            "- 五因子是否失衡（某因子主导意味着对应通道权重失衡）\n"
            "- 抑制分布是否异常（quota 高→频率上限过低，below_threshold 高→阈值过高）\n"
            "- 疲劳是否接近上限（接近→消耗过快或恢复太慢）\n"
            "- 对话状态是否需要修正（is_argument 高→应更克制）\n\n"
            "## 第三步：假设原因\n"
            "- 对每个诊断出的问题，给出可能的原因假设\n"
            "- 引用具体数值支持假设（如「triggered_rate=3% 低于健康下限 5%，原因是 base_threshold=0.55 偏高，且 s_int=0.32 主导但 w_int=1.2 已不低，问题在阈值而非权重」）\n\n"
            "## 第四步：设计调整方案\n"
            "- 针对每个原因，设计具体的参数调整方案\n"
            "- 优先级排序：先解决最严重的问题\n"
            "- 每项调整必须满足：\n"
            "  * 在 VALIDATORS 范围内\n"
            "  * 单次变化幅度不超过 autotune_max_change_ratio（默认 30%）\n"
            "  * 单次总参数数不超过 autotune_max_params_per_tune（默认 5）\n"
            "- 不要一次调整太多参数，宁可分多次调参\n\n"
            "## 第五步：量化预期影响\n"
            "- 对每项调整，预估具体数值影响（如「触发率 +5%」「score_mean +0.1」）\n"
            "- 整体预估应用后的效果（如「预期触发率从 3% 提升至 8%，进入健康区间」）\n"
            "- 量化预估必须基于当前数据推算，不要空泛描述"
        )

        # 3. 参数范围参考（新增）
        sections.append(f"# 参数范围参考\n\n{param_range_table}")

        # 4. 机器人画像
        sections.append(
            "# 机器人画像\n\n"
            f"**人设文本（persona_text）**：\n{full_cfg.get('persona_text', '')}\n\n"
            f"**补充知识（persona_knowledge）**：\n{full_cfg.get('persona_knowledge', '')}"
        )

        # 5. Provider 信息
        sections.append(
            "# Provider 信息\n\n"
            f"- Chat provider: {chat_prov_name}\n"
            f"- Embedding provider: {embed_prov_name}"
        )

        # 6. 插件工作原理
        sections.append(
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
            "   mult 钳制 [0.5, 2.0]。5%-30% 为健康触发率区间。"
        )

        # 7. 兴趣数据
        sections.append(
            "# 兴趣数据（export_view，含 items/hate_keywords/high_interest_keywords/rejected）\n\n"
            f"{json.dumps(interest_view, ensure_ascii=False, indent=2)}"
        )

        # 8. 作息 schedule
        sections.append(
            "# 作息 schedule\n\n"
            f"{json.dumps(full_cfg.get('schedule', []), ensure_ascii=False, indent=2)}"
        )

        # 9. 群范围
        sections.append(
            "# 群范围\n\n"
            f"- group_mode: {full_cfg.get('group_mode', 'whitelist')}\n"
            f"- group_whitelist: {full_cfg.get('group_whitelist', [])}"
        )

        # 10. 自适应阈值状态
        sections.append(
            "# 自适应阈值状态（每群 mult/window_rate/samples）\n\n"
            f"{json.dumps(adaptive_summary, ensure_ascii=False, indent=2)}"
        )

        # 11. 对话状态摘要
        sections.append(
            "# 对话状态摘要（v0.3.5 F6）\n\n"
            f"{json.dumps(stats.get('conversation_state_summary', {}), ensure_ascii=False, indent=2)}"
        )

        # 12. 近期决策数据统计
        sections.append(
            "# 近期决策数据统计（最近 200 条）\n\n"
            f"{json.dumps(stats, ensure_ascii=False, indent=2)}"
        )

        # 13. 当前全量配置
        sections.append(
            "# 当前全量配置（除 DENYLIST 外均可改）\n\n"
            f"{json.dumps(current_cfg, ensure_ascii=False, indent=2)}"
        )

        # 14. 用户偏好
        sections.append(
            "# 用户偏好\n\n"
            f"**回复风格**：{style_text}\n\n"
            f"**用户补充说明**：\n{user_guidance}"
        )

        # 15. 可写键说明
        sections.append(
            "# 可写键说明\n\n"
            f"可写键 = 全量配置减 DENYLIST（共 {writable_count} 项）。\n"
            f"DENYLIST（安全敏感键，不可改）：{denylist_str}\n"
            "另外可输出：\n"
            "- suggested_keywords_patch：兴趣关键词增删\n"
            "- persona_revision：人设改写（可选，仅当人设本身需要调整时）"
        )

        # 16. 调参约束（新增，按 spec.md B 节 + tasks.md T3）
        sections.append(
            "# 调参约束\n\n"
            "请严格遵守以下限制，超出会被后端拒绝：\n\n"
            "## 幅度上限\n"
            "- 对每个参数：|suggested - original| <= autotune_max_change_ratio × |original|\n"
            f"- 当前 autotune_max_change_ratio = {max_change_ratio}，"
            f"即单次最多变化 ±{int(max_change_ratio * 100)}%\n"
            "- 特例：|original| < 0.1 时，按 VALIDATORS 范围的 1/4 限幅（边界兼底）\n\n"
            "## 数量上限\n"
            "- plan 中参数数量 <= autotune_max_params_per_tune\n"
            f"- 当前 autotune_max_params_per_tune = {max_params}\n"
            "- 超过则后端截断取前 N 项，其余丢弃\n\n"
            "## 必填字段\n"
            "- 每项 plan 必须含 `reason`（为什么改，引用具体数值）\n"
            "- 每项 plan 必须含 `expected_effect_quant`"
            "（预期量化影响，必须含数字，如「触发率 +5%」）\n"
            "- 缺任一字段该项被拒绝\n\n"
            "## 范围限制\n"
            "- suggested 值必须在 VALIDATORS 范围内（见上方「# 参数范围参考」段）\n"
            "- 超出范围该项被拒绝\n\n"
            "## 分阶段建议\n"
            "- 不要一次调整太多参数，宁可分多次调参\n"
            "- 优先解决最严重的问题"
        )

        # 17. 分析要求（12 维度，新增「每项调整必填理由 + 量化预估」要求）
        sections.append(
            "# 分析要求\n\n"
            "请基于机器人完整画像做整体性调参建议：\n\n"
            "1. **触发率**：当前 triggered_rate 是否在健康区间？与用户风格偏好的目标区间对比。\n"
            "   偏高→收紧阈值/降权重；偏低→放宽。注意区分 below_threshold 和 quota/min_interval 抑制。\n\n"
            "2. **得分分布**：score_mean vs threshold_mean 的差距是否合理？\n"
            "   差距过大（分数远低于阈值）→ 阈值偏高或权重不足；\n"
            "   差距过小（分数接近阈值）→ 触发过于敏感，波动大。\n\n"
            "3. **五因子均衡**：factors_mean 中 s_int/s_topic/s_resp/c_cooldown/p_silence\n"
            "   是否有某因子主导？主导因子意味着该通道权重失衡，应调低对应 w_* 或调高其他。\n\n"
            "4. **抑制分布**：suppressed_hist 中 quota 占比高→频率上限过低或阈值过低导致\n"
            "   频繁触发后被配额拦截；below_threshold 占比高→阈值过高；\n"
            "   min_interval 占比高→proactive_min_interval 过长或阈值过低导致频繁尝试被间隔拦截。\n\n"
            "5. **疲劳状态**：fatigue_value_mean 是否接近 fatigue_limit？\n"
            "   接近→疲劳消耗过快或恢复太慢，机器人会逐渐沉默。\n"
            "   可调：fatigue_cost_active/passive/track/glance（消耗）、\n"
            "   fatigue_recovery_rate（恢复速率）、fatigue_high/medium_modifier（倍率）、\n"
            "   fatigue_suppress_enabled（抑制开关）。\n\n"
            "6. **兴趣数据**：兴趣 items 是否合理？是否需要增删关键词？\n"
            "   人设文本是否需要调整？（仅必要时输出 persona_revision）\n\n"
            "7. **对话状态**：conversation_state_summary 中 avg_appropriateness 是否合理？\n"
            "   is_argument/is_monologue 占比高→机器人应更克制（modifier>1）；\n"
            "   has_question/bot_turn 占比高→机器人可更活跃（modifier<1）。\n\n"
            "8. **风格对齐**：建议方向必须与用户回复风格偏好一致。\n\n"
            "9. **惯性强度**：after_reply_probability（回复后继续活跃概率，默认0.7）、\n"
            "   probability_duration（持续时长秒，默认30）、proactive_temp_boost（话题提升，默认0.5）、\n"
            "   proactive_boost_duration（话题提升持续秒，默认60）。\n"
            "   机器人太粘人→降 after_reply_probability/proactive_temp_boost；\n"
            "   机器人接话断裂感强→升 after_reply_probability。\n\n"
            "10. **瞥一眼机制**：glance_enable（开关）、glance_group_count（候选群数，默认3）、\n"
            "    glance_min_score（最低触发分数，默认0.85）。\n"
            "    瞥一眼太频繁→升 glance_min_score 或降 glance_group_count；\n"
            "    瞥一眼从不触发→降 glance_min_score。\n\n"
            "11. **规则通道**：rule_question_threshold（疑问信号阈值，默认65）、\n"
            "    rule_context_threshold（上下文唤醒阈值，默认50）、\n"
            "    fusion_weight_rule（规则通道融合权重，默认0.4）。\n"
            "    规则误触发多→升阈值或降 fusion_weight_rule；\n"
            "    规则从不触发→降阈值或升 fusion_weight_rule。\n\n"
            "12. **冷却与间隔**：cooldown_messages（冷却窗口消息条数，默认4）、\n"
            "    proactive_min_interval（主动消息最小间隔秒，默认180）、\n"
            "    group_cooldown（主循环监听后群冷却秒，默认180）。\n"
            "    机器人话痨→升 proactive_min_interval；\n"
            "    机器人反应迟钝→降 proactive_min_interval（但不宜低于60秒）。\n\n"
            "**每项调整必填 reason（引用具体数值）+ expected_effect_quant（含量化数字）。**"
        )

        # phase='plan' 时，在「# 输出格式」前插入「# 已生成诊断报告」段（spec A.5）
        if phase == "plan":
            diag_text = diagnosis_text or ""
            sections.append(
                "# 已生成诊断报告\n\n"
                f"{diag_text}\n\n"
                "请基于以上诊断报告，设计具体的参数调整方案。"
            )

        # 18. 输出格式（按 phase 分支，spec A.1 + A.5）
        if phase == "diagnosis":
            sections.append(
                "# 输出格式\n\n"
                "仅输出严格 JSON（不要 ```json 标记），只含 diagnosis 字段：\n"
                "{\n"
                '  "diagnosis": "对当前参数与决策数据的诊断分析'
                '（必填，中文，按上方「# 思考流程」五步展开）"\n'
                "}\n\n"
                "仅输出 JSON，不要其他文本。本轮只输出诊断，不要输出 plan。"
            )
        elif phase == "plan":
            sections.append(
                "# 输出格式\n\n"
                "仅输出严格 JSON（不要 ```json 标记），只含 plan 字段：\n"
                "{\n"
                '  "plan": [\n'
                "    {\n"
                '      "key": "参数名",\n'
                '      "original": 0.55,\n'
                '      "suggested": 0.50,\n'
                '      "delta": -0.05,\n'
                '      "reason": "为什么改这个参数（必填，引用具体数值）",\n'
                '      "expected_effect_quant": "预期量化影响，如「触发率 +5%」'
                '「score_mean +0.1」（必填，必须含数字）"\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                "仅输出 JSON，不要其他文本。本轮只输出 plan，不要重复 diagnosis。"
            )
        else:  # phase='full'（默认）
            sections.append(
                "# 输出格式\n\n"
                "仅输出严格 JSON（不要 ```json 标记），含以下字段：\n"
                "{\n"
                '  "diagnosis": "对当前参数与决策数据的诊断分析'
                '（必填，中文，按上方「# 思考流程」五步展开）",\n'
                '  "plan": [\n'
                "    {\n"
                '      "key": "参数名",\n'
                '      "original": 0.55,\n'
                '      "suggested": 0.50,\n'
                '      "delta": -0.05,\n'
                '      "reason": "为什么改这个参数（必填，引用具体数值）",\n'
                '      "expected_effect_quant": "预期量化影响，如「触发率 +5%」'
                '「score_mean +0.1」（必填，必须含数字）"\n'
                "    }\n"
                "  ],\n"
                '  "suggested_keywords_patch": {\n'
                '    "add": [{"kind": "example"|"high_keyword"|"hate_keyword", '
                '"label": "core"|"general"|"marginal"|"hate", "text": "..."}],\n'
                '    "remove": [{"kind": ..., "label": ..., "text": "..."}]\n'
                "  },\n"
                '  "persona_revision": "可选，仅当人设本身需要调整时输出新人设文本",\n'
                '  "expected_effect_overall": "应用建议后的整体预期效果（必填，引用具体数值）"\n'
                "}\n\n"
                "仅输出 JSON，不要其他文本。"
            )

        return "\n\n".join(sections)

    def _validate_plan(self, plan: list, current_config: dict) -> tuple[list, list]:
        """v0.3.10 T4：校验 LLM 输出的 plan，返回 (validated_plan, errors)。

        校验规则（按 spec.md B 节）：
        - 幅度上限：对每项，若 |original| >= 0.1 则 max_delta = autotune_max_change_ratio * |original|；
          否则查 VALIDATORS 取 (hi-lo)/4；|delta| > max_delta 则该项拒绝
        - 数量上限：len(plan) > autotune_max_params_per_tune 则截断取前 N 项，其余丢弃
        - 必填理由：缺 reason 或 expected_effect_quant 则该项拒绝
        - DENYLIST 过滤：key in TUNE_DENYLIST 则该项拒绝
        - 范围限制：suggested 值必须在 VALIDATORS 范围内（超出则该项拒绝）

        Args:
            plan: LLM 输出的 plan 列表，每项形如
                {key, original, suggested, delta, reason, expected_effect_quant}
            current_config: 当前配置 dict（用于读取 autotune_max_change_ratio /
                autotune_max_params_per_tune）

        Returns:
            (validated_plan, errors)：validated_plan 是通过校验的项列表（保持原顺序）；
            errors 是被拒绝项的错误信息列表，每条形如 "{key}: {原因}"
        """
        # 输入容错
        if not isinstance(plan, list):
            return [], ["plan 不是列表"]
        if not plan:
            return [], []

        # 读取配置（从 current_config dict 读，不直接调 self._config_getter() 避免与 T5 冲突）
        max_change_ratio = float(current_config.get("autotune_max_change_ratio", 0.3))
        max_params = int(current_config.get("autotune_max_params_per_tune", 5))

        errors: list[str] = []
        validated_plan: list[dict] = []

        # 数量上限截断（先做，避免对超量项做无谓校验）
        working_plan = plan
        if len(plan) > max_params:
            errors.append(
                f"plan 含 {len(plan)} 项，超过上限 {max_params}，"
                f"已截断为前 {max_params} 项"
            )
            working_plan = plan[:max_params]

        # 逐项校验
        for idx, item in enumerate(working_plan):
            if not isinstance(item, dict):
                errors.append(f"{idx}: 非对象")
                continue

            key = str(item.get("key", "")).strip()
            if not key:
                errors.append(f"{idx}: 缺 key")
                continue

            original = item.get("original")
            suggested = item.get("suggested")
            delta = item.get("delta")

            # delta 缺失或非数值时尝试推算（spec 要求 delta is None；扩展为非数值即推算
            # 以避免后续 abs(delta) 崩溃，与 spec intent 一致）
            delta_inferred = False
            if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                if (
                    isinstance(original, (int, float))
                    and not isinstance(original, bool)
                    and isinstance(suggested, (int, float))
                    and not isinstance(suggested, bool)
                ):
                    delta = suggested - original
                    delta_inferred = True
                else:
                    errors.append(f"{key}: delta 缺失且无法推算")
                    continue

            # DENYLIST 检查
            if key in TUNE_DENYLIST:
                errors.append(f"{key}: 在 DENYLIST 中（安全敏感键不可改）")
                continue

            # 必填理由检查
            reason = str(item.get("reason", "")).strip()
            if not reason:
                errors.append(f"{key}: 缺 reason")
                continue
            expected_effect_quant = str(item.get("expected_effect_quant", "")).strip()
            if not expected_effect_quant:
                errors.append(f"{key}: 缺 expected_effect_quant")
                continue
            if not any(c.isdigit() for c in expected_effect_quant):
                errors.append(f"{key}: expected_effect_quant 必须含量化数字")
                continue

            # 范围限制检查（查 ConfigStore.VALIDATORS）
            if key in ConfigStore.VALIDATORS:
                typ, lo, hi = ConfigStore.VALIDATORS[key]
                if typ is bool:
                    if not isinstance(suggested, bool):
                        errors.append(f"{key}: 期望 bool 类型")
                        continue
                elif typ is int:
                    if not isinstance(suggested, int) or isinstance(suggested, bool):
                        errors.append(f"{key}: 期望 int 类型")
                        continue
                elif typ is float:
                    if not isinstance(suggested, (int, float)) or isinstance(
                        suggested, bool
                    ):
                        errors.append(f"{key}: 期望 float 类型")
                        continue
                # 范围检查（bool 类型 lo/hi 均为 None，自然跳过）
                if lo is not None and suggested < lo:
                    errors.append(f"{key}: suggested={suggested} 低于下限 {lo}")
                    continue
                if hi is not None and suggested > hi:
                    errors.append(f"{key}: suggested={suggested} 超出上限 {hi}")
                    continue

            # 幅度上限检查（仅在 VALIDATORS 有范围时做；
            # 无 VALIDATORS 的键如 persona_text 不做幅度限制）
            if key in ConfigStore.VALIDATORS:
                _v_typ, v_lo, v_hi = ConfigStore.VALIDATORS[key]
                if v_lo is not None and v_hi is not None:
                    if (
                        isinstance(original, (int, float))
                        and not isinstance(original, bool)
                        and abs(original) >= 0.1
                    ):
                        max_delta = max_change_ratio * abs(original)
                    else:
                        # |original| < 0.1 或 original 非数值 → 边界兼底
                        max_delta = (v_hi - v_lo) / 4
                    if abs(delta) > max_delta:
                        errors.append(
                            f"{key}: 变化幅度 {delta} 超出上限 {max_delta:.4f}"
                        )
                        continue

            # 通过所有检查 → 加入 validated_plan
            # 保持原字段（补全 delta 字段若之前推算过）
            if delta_inferred:
                new_item = dict(item)
                new_item["delta"] = delta
                validated_plan.append(new_item)
            else:
                validated_plan.append(item)

        return validated_plan, errors

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
        """解析 LLM 返回的 JSON（容错 ```json ... ``` fence 与首尾空白）。

        v0.3.10：兼容新旧格式。
        - 新格式：``{diagnosis, plan: [...], suggested_keywords_patch, persona_revision,
          expected_effect_overall}``
        - 旧格式：``{analysis, suggested_patch: {...}, suggested_keywords_patch,
          persona_revision, expected_effect}``
        - 返回统一 dict 含全部字段（新+旧），供调用方按需取用。
        """
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
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        # 新格式优先；旧格式 analysis 映射为 diagnosis，suggested_patch 转 plan
        diagnosis = str(data.get("diagnosis") or data.get("analysis") or "")
        plan = data.get("plan")
        suggested_patch = data.get("suggested_patch")
        if plan is None and isinstance(suggested_patch, dict):
            # 旧格式 → plan：每项补空 reason/expected_effect_quant 占位
            plan = [
                {
                    "key": k,
                    "original": None,
                    "suggested": v,
                    "delta": None,
                    "reason": "",
                    "expected_effect_quant": "",
                }
                for k, v in suggested_patch.items()
            ]
        if not isinstance(plan, list):
            plan = []
        if not isinstance(suggested_patch, dict) and plan:
            # 新格式 → suggested_patch（向后兼容 apply 分支）
            suggested_patch = {
                item.get("key"): item.get("suggested")
                for item in plan
                if isinstance(item, dict) and item.get("key")
            }
        if not isinstance(suggested_patch, dict):
            suggested_patch = {}
        expected_overall = (
            data.get("expected_effect_overall") or data.get("expected_effect") or ""
        )
        return {
            "diagnosis": diagnosis,
            "plan": plan,
            "analysis": diagnosis,  # 向后兼容
            "suggested_patch": suggested_patch,  # 向后兼容 apply 分支
            "suggested_keywords_patch": data.get("suggested_keywords_patch"),
            "persona_revision": data.get("persona_revision"),
            "expected_effect_overall": str(expected_overall),
            "expected_effect": str(expected_overall),  # 向后兼容
        }
