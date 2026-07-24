"""WebBridge 模块（模块 D）：Web API 注册与 WebBridge 鸭子接口实现。

职责：
1. ``_register_web_apis`` / ``_wrap_web_handler``：注册 16 个 Web API 路由，
   把 ``core/web.py`` 的 ``(params, body) -> (status, json)`` 适配为
   AstrBot ``register_web_api`` 期望的 handler（统一 200 + 结构化错误）。
2. WebBridge 鸭子接口实现（16 个方法）：``get_status`` / ``get_decisions`` /
   ``get_config_view`` / ``set_config_view`` / ``get_groups_view`` / ``set_groups_view`` /
   ``get_providers_view`` / ``get_interests_view`` / ``set_interests_view`` /
   ``get_export_view`` / ``get_tune_history_view`` / ``clear_tune_history_view`` /
   ``approve_tune`` / ``reject_tune`` / ``restore_tune`` / ``run_autotune_plan``
   （后 4 个为 v0.3.10 T7 批准工作流 API）。

设计要点：
- Mixin 不定义 ``__init__``，依赖宿主类（``ProSocialPlugin``）提供 ``self.context`` /
  ``self.scheduler`` / ``self._config_store`` / ``self._SPECIAL_KEYS`` / ``self.config`` /
  ``self._config_getter`` / ``self._llm_fn`` / ``self._embed_fn`` / ``self._log`` /
  ``self.interest_mgr`` 等实例属性。
- astrbot 相关 import（``json_response`` / ``request`` / ``logger``）延迟到方法内部。
"""

from __future__ import annotations

import asyncio
import time

from .web import build_handlers

# 插件名（与 metadata.yaml 一致，用于 Web API 路由前缀）
# 直接定义为常量，避免从 main 导入造成循环依赖
_PLUGIN_NAME = "astrbot_plugin_proactive_social"


class WebBridgeMixin:
    """Web API 注册与 WebBridge 鸭子接口 Mixin（模块 D）。

    依赖宿主类（``ProSocialPlugin``）的以下实例属性与方法：
    - ``self.context``：AstrBot Context（``register_web_api`` / ``get_all_providers`` 等）
    - ``self.scheduler``：``SocialScheduler`` 实例（``get_status`` / ``_decision_log`` 等）
    - ``self._config_store``：``ConfigStore`` 实例（``snapshot`` / ``set_many`` / ``set_kv``）
    - ``self._SPECIAL_KEYS``：特殊选择器键集合
    - ``self.config``：``AstrBotConfig`` 实例
    - ``self._config_getter()``：合并 ConfigStore + AstrBotConfig 的配置读取方法
    - ``self._llm_fn`` / ``self._embed_fn``：注入回调
    - ``self._log(level, msg)``：统一日志回调
    - ``self.interest_mgr``：``InterestManager`` 实例
    - ``self._make_embed_fn()``：构造 embed_fn 的方法（``set_interests_view`` 用）
    """

    # ------------------------------------------------------------------ #
    # Web API 注册与 handler 包装
    # ------------------------------------------------------------------ #
    def _register_web_apis(self):
        """注册 16 个 Web API，route 加插件名前缀。"""
        # self 实现 WebBridge 鸭子接口（get_status/get_decisions/get_config_view/
        # set_config_view/get_groups_view/set_groups_view）
        handlers = build_handlers(self)
        for route_key, inner_handler in handlers.items():
            method, path = route_key.split(" ", 1)
            full_route = f"/{_PLUGIN_NAME}{path}"
            wrapped = self._wrap_web_handler(inner_handler)
            self.context.register_web_api(
                full_route, wrapped, [method], desc=f"ProSocial {path}"
            )

    def _wrap_web_handler(self, inner_handler):
        """把 web.py 的 (params, body) -> (status, json) 适配为 register_web_api 期望的 handler。

        AstrBot handler 无显式参数，通过 ``request`` 上下文代理读取 query / body。
        返回 ``json_response``。**所有响应统一 200**：错误经
        ``{"ok": false, "error": ...}`` 结构化返回，让前端 bridge 拿到完整 JSON
        （plugin-pages.md 提到 bridge 对非 2xx reject；降级为 200 + ok=false 让
        前端能读取结构化错误，前端两种格式都能处理）。
        """
        from astrbot.api.web import json_response, request

        async def wrapper():
            try:
                # 收集 query 参数为 dict
                params: dict = {}
                try:
                    qs = request.query
                    for k in qs.keys():
                        params[k] = qs.get(k)
                except Exception:
                    pass
                # 收集 JSON body（仅 POST/PUT/PATCH）
                body = None
                try:
                    if request.method in ("POST", "PUT", "PATCH"):
                        body = await request.json(default=None)
                except Exception:
                    body = None
                _status, json_body = await inner_handler(params, body)
            except Exception as e:
                json_body = {"ok": False, "error": f"web handler 包装异常: {e}"}
            return json_response(json_body, status_code=200)

        return wrapper

    # --- WebBridge 鸭子接口实现 ---

    def get_status(self) -> dict:
        """状态面板数据。"""
        if self.scheduler is None:
            return {"running": False, "error": "scheduler not initialized"}
        return self.scheduler.get_status()

    def get_decisions(self, limit: int) -> list[dict]:
        """最近 limit 条决策记录（新→旧）。受控访问 scheduler._decision_log。"""
        if self.scheduler is None:
            return []
        return self.scheduler._decision_log.recent(limit)

    def get_config_view(self) -> dict:
        """返回全量配置（ConfigStore 快照 + 特殊选择器），供前端展示。

        普通参数来自 ConfigStore.snapshot()（浅拷贝，外部修改不污染缓存）；
        特殊选择器（chat_provider_id 等）从 self.config 叠加。
        """
        cfg = self._config_store.snapshot()
        for k in self._SPECIAL_KEYS:
            if k in self.config:
                cfg[k] = self.config[k]
        return cfg

    async def set_config_view(self, patch: dict) -> tuple[bool, str]:
        """委托 ConfigStore.set_many 校验 + 写缓存 + 持久化 KV。返回 (ok, error)。

        特殊键（chat_provider_id / embedding_provider_id）由 ConfigStore.set_many 拒绝
        （"特殊选择器，请在主面板配置"），Web API 不处理特殊键。
        """
        if not isinstance(patch, dict):
            return False, "patch 必须是 JSON 对象"
        ok, msg = await self._config_store.set_many(patch)
        if not ok:
            return False, msg
        # F4: 人设文本/知识或兴趣生成数量变更触发兴趣重新生成（后台执行，不阻塞 API 响应）。
        # v0.2.8：interest_example_count/interest_keyword_count 也纳入触发条件
        # （_compute_persona_hash 已把数量纳入哈希，改数量必须 regenerate 才能生效）。
        if any(
            k in patch
            for k in (
                "persona_text",
                "persona_knowledge",
                "interest_example_count",
                "interest_keyword_count",
            )
        ):
            try:
                new_cfg = self._config_getter()
                persona_text = str(new_cfg.get("persona_text", ""))
                persona_knowledge = str(new_cfg.get("persona_knowledge", ""))
                example_count = int(new_cfg.get("interest_example_count", 3))
                keyword_count = int(new_cfg.get("interest_keyword_count", 12))
                llm_fn = self._llm_fn
                embed_fn = self._embed_fn
                log_fn = self._log

                async def _bg_regenerate():
                    try:
                        await self.interest_mgr.regenerate(
                            persona_text,
                            persona_knowledge,
                            llm_fn,
                            embed_fn,
                            example_count=example_count,
                            keyword_count=keyword_count,
                        )
                        log_fn("info", "人设变更，兴趣数据已重新生成")
                    except Exception as exc:
                        log_fn("warning", f"人设变更后兴趣重建失败: {exc}")

                asyncio.create_task(_bg_regenerate())
            except Exception as e:
                self._log("warning", f"启动兴趣重建后台任务失败: {e}")
        return True, ""

    def get_groups_view(self) -> dict:
        """群管理面板数据：mode/whitelist/各群运行时状态。

        group_mode / group_whitelist 现由 ConfigStore 管理（v0.2.1），
        从 _config_getter() 合并后的配置读取。
        """
        cfg = self._config_getter()
        mode = str(cfg.get("group_mode", "whitelist"))
        whitelist = list(cfg.get("group_whitelist", []) or [])
        groups: list = []
        if self.scheduler is not None:
            status = self.scheduler.get_status()
            groups = status.get("groups", [])
        return {"mode": mode, "whitelist": whitelist, "groups": groups}

    async def set_groups_view(self, patch: dict) -> tuple[bool, str]:
        """更新 mode/whitelist/group_toggles。返回 (ok, error)。

        mode / whitelist 走 ConfigStore.set_many（KV 持久化）；
        group_toggles 走 scheduler.set_group_enabled（独立 KV 键 "group_enable"）。
        """
        if not isinstance(patch, dict):
            return False, "patch 必须是 JSON 对象"
        # 收集需写入 ConfigStore 的普通键
        updates: dict = {}
        if "mode" in patch:
            if patch["mode"] not in ("whitelist", "all"):
                return False, "mode 必须是 whitelist 或 all"
            updates["group_mode"] = patch["mode"]
        if "whitelist" in patch:
            wl = patch["whitelist"]
            if not isinstance(wl, list) or not all(isinstance(x, str) for x in wl):
                return False, "whitelist 必须是字符串列表"
            updates["group_whitelist"] = wl
        # 事务性写入 ConfigStore（校验 + 缓存 + SQLite）
        if updates:
            ok, msg = await self._config_store.set_many(updates)
            if not ok:
                return False, msg
        # group_toggles 走 scheduler（独立 KV 键，不经 ConfigStore）
        if "group_toggles" in patch:
            toggles = patch["group_toggles"]
            if not isinstance(toggles, dict):
                return False, "group_toggles 必须是对象"
            if self.scheduler is not None:
                for gid, enabled in toggles.items():
                    if not isinstance(enabled, bool):
                        return False, f"group_toggles[{gid}] 必须是布尔值"
                    await self.scheduler.set_group_enabled(str(gid), enabled)
        return True, ""

    # --- F18/F20 WebBridge 扩展接口 ---

    def get_providers_view(self) -> dict:
        """返回已配置的 chat / embedding provider id 列表（F18 Embedding 选择器）。

        AstrBot 源码确认（context.py / manager.py）：
        - ``get_all_providers()`` 返回 ``provider_manager.provider_insts``，
          仅含 chat completion 类（``Provider`` 子类，CHAT_COMPLETION 类型）。
        - ``get_all_embedding_providers()`` 返回
          ``provider_manager.embedding_provider_insts``，仅含 ``EmbeddingProvider`` 子类。
        两者在 ``ProviderManager.load_provider`` 中按 ``provider_metadata.provider_type``
        分桶注册，无需再在插件侧 isinstance 判定。provider id 取 ``meta().id``。
        """
        from astrbot.api import logger

        chat_ids: list[str] = []
        embed_ids: list[str] = []
        try:
            for p in self.context.get_all_providers():
                try:
                    pid = p.meta().id
                except Exception:
                    pid = ""
                if pid:
                    chat_ids.append(pid)
            for p in self.context.get_all_embedding_providers():
                try:
                    pid = p.meta().id
                except Exception:
                    pid = ""
                if pid:
                    embed_ids.append(pid)
        except Exception as e:
            logger.warning(f"[ProSocial] 获取 provider 列表失败: {e}")
        return {"chat": chat_ids, "embedding": embed_ids}

    def get_interests_view(self) -> dict:
        """返回兴趣数据纯文本视图（F20）。未生成时返回 generated=False 空结构。"""
        if self.interest_mgr is None:
            return {
                "generated": False,
                "persona_hash": "",
                "items": [],
                "hate_keywords": [],
                "high_interest_keywords": [],
                "rejected": {"examples": [], "keywords": []},
            }
        return self.interest_mgr.export_view()

    async def set_interests_view(self, body: dict) -> tuple[bool, str]:
        """处理兴趣人工过滤操作（F20）与增删改查（F2）。

        body.action == "reject"  : 加 rejected 项并持久化到 KV "interest_rejected"，
                                    后台触发 apply_rejected 重算质心（reject 已即时移除，
                                    重算仅兜底保证质心与 active 一致）
        body.action == "restore" : v0.3.6 F2：从 rejected 移除并加回 active，
                                    持久化 _rejected，后台触发 apply_rejected 重算质心
        body.action == "apply"   : 调 apply_rejected 重算质心
        body.action == "add"     : 调 add_item 添加关键词/示例句子
        body.action == "update"  : 调 update_item 更新关键词/示例句子
        body.action == "remove"  : 调 remove_item 移除关键词/示例句子（v0.3.6 统一进 _rejected 可恢复）
        """
        if not isinstance(body, dict):
            return False, "请求体必须是 JSON 对象"
        action = body.get("action")
        embed_fn = self._make_embed_fn()
        if action == "reject":
            kind = body.get("kind")
            if kind not in ("example", "keyword", "high_keyword", "hate_keyword"):
                return False, "kind 必须是 example、keyword、high_keyword 或 hate_keyword"
            self.interest_mgr.reject(
                kind=kind,
                label=str(body.get("label", "") or ""),
                text=str(body.get("text", "") or ""),
            )
            try:
                await self._config_store.set_kv(
                    "interest_rejected",
                    self.interest_mgr.get_rejected(),
                )
            except Exception as e:
                self._log("warning", f"持久化 interest_rejected 失败: {e}")
                return False, f"持久化失败: {e}"
            # v0.3.6：reject 已即时移除 active，后台触发 apply_rejected 重算质心兜底
            self._bg_apply_rejected(embed_fn)
            return True, ""
        if action == "restore":
            # v0.3.6 F2：从 rejected 恢复到 active，持久化 _rejected，后台重算质心
            kind = body.get("kind")
            if kind not in ("example", "keyword", "high_keyword", "hate_keyword"):
                return False, "kind 必须是 example、keyword、high_keyword 或 hate_keyword"
            ok, msg = self.interest_mgr.restore(
                kind=kind,
                label=str(body.get("label", "") or ""),
                text=str(body.get("text", "") or ""),
            )
            if not ok:
                return False, msg
            try:
                await self._config_store.set_kv(
                    "interest_rejected",
                    self.interest_mgr.get_rejected(),
                )
            except Exception as e:
                self._log("warning", f"持久化 interest_rejected 失败: {e}")
                return False, f"持久化失败: {e}"
            # 后台触发质心重算（restore 已加回 active，重算让其纳入质心）
            self._bg_apply_rejected(embed_fn)
            return True, ""
        if action == "apply":
            ok, msg = await self.interest_mgr.apply_rejected(embed_fn)
            return ok, msg
        if action == "add":
            kind = body.get("kind")
            if kind not in ("example", "high_keyword", "hate_keyword"):
                return False, "kind 必须是 example、high_keyword 或 hate_keyword"
            label = str(body.get("label", "") or "")
            text = str(body.get("text", "") or "")
            return await self.interest_mgr.add_item(kind, label, text, embed_fn)
        if action == "update":
            kind = body.get("kind")
            if kind not in ("example", "high_keyword", "hate_keyword"):
                return False, "kind 必须是 example、high_keyword 或 hate_keyword"
            label = str(body.get("label", "") or "")
            old_text = str(body.get("old_text", "") or "")
            new_text = str(body.get("new_text", "") or "")
            return await self.interest_mgr.update_item(
                kind, label, old_text, new_text, embed_fn
            )
        if action == "remove":
            kind = body.get("kind")
            if kind not in ("example", "high_keyword", "hate_keyword"):
                return False, "kind 必须是 example、high_keyword 或 hate_keyword"
            label = str(body.get("label", "") or "")
            text = str(body.get("text", "") or "")
            return await self.interest_mgr.remove_item(kind, label, text, embed_fn)
        return False, "未知 action"

    def _bg_apply_rejected(self, embed_fn) -> None:
        """v0.3.6：后台触发 apply_rejected 重算质心（不阻塞 Web API 响应）。

        reject/restore 已同步修改 active items/keywords，此处仅触发质心重算
        让向量数据与 active 列表一致。失败仅 log，不影响主流程。
        """
        try:
            interest_mgr = self.interest_mgr
            log_fn = self._log

            async def _bg():
                try:
                    await interest_mgr.apply_rejected(embed_fn)
                except Exception as exc:
                    log_fn("warning", f"后台 apply_rejected 重算质心失败: {exc}")

            asyncio.create_task(_bg())
        except Exception as e:
            self._log("warning", f"启动 apply_rejected 后台任务失败: {e}")

    # --- v0.3.6 F3：调参历史 API ---

    async def get_tune_history_view(
        self, limit: int = 50, offset: int = 0,
        *, status_filter: str | None = None,
        include_archived: bool = False, hide_days: int | None = None,
    ) -> dict:
        """返回调参历史记录列表 + 统计摘要。

        limit/offset 分页；返回 {records, stats}。
        v0.3.10：透传 status_filter/include_archived/hide_days 给
        ``TuneHistoryStore.list``（按状态过滤 / 30 天归档隐藏）。
        records 每条含 id/timestamp/action/source/patch/keywords_patch/persona_revision/
        analysis/expected_effect/applied + 8 个新字段（original_values/pre_apply_values/
        applied_values/diagnosis/plan/status/approved_by/error_msg）。
        stats 含 total/analyze_count/apply_count/last_timestamp。
        """
        try:
            records = await self._tune_history.list(
                limit, offset,
                status_filter=status_filter,
                include_archived=include_archived,
                hide_days=hide_days,
            )
            stats = await self._tune_history.get_stats()
            return {"records": records, "stats": stats}
        except Exception as e:
            self._log("warning", f"get_tune_history_view 失败: {e}")
            return {
                "records": [],
                "stats": {
                    "total": 0,
                    "analyze_count": 0,
                    "apply_count": 0,
                    "last_timestamp": None,
                },
            }

    async def clear_tune_history_view(self) -> tuple[bool, str]:
        """清空调参历史。返回 (ok, error)。"""
        try:
            deleted = await self._tune_history.clear()
            self._log("info", f"已清空 {deleted} 条调参历史")
            return True, ""
        except Exception as e:
            self._log("warning", f"clear_tune_history_view 失败: {e}")
            return False, str(e)

    # --- v0.3.10 T7：批准工作流 API ---

    async def approve_tune(self, record_id: int, approved_by: str = "web") -> dict:
        """v0.3.10 T7：批准并 apply 一条 pending 记录。

        调 ``llm_autotune(action='apply', record_id=record_id, approved_by=approved_by,
        source='manual')``。返回 llm_autotune 的响应 dict。
        """
        try:
            return await self.llm_autotune(
                "apply", record_id=record_id, approved_by=approved_by, source="manual"
            )
        except Exception as e:
            self._log("warning", f"approve_tune 失败: {e}")
            return {"ok": False, "error": f"批准失败: {e}"}

    async def reject_tune(self, record_id: int, approved_by: str = "web") -> tuple[bool, str]:
        """v0.3.10 T7：拒绝一条 pending 记录。

        调 ``tune_history.update_status(record_id, 'rejected', approved_by)``。
        返回 (ok, error)。
        """
        try:
            ok = await self._tune_history.update_status(record_id, "rejected", approved_by)
            if not ok:
                return False, f"记录 {record_id} 不存在或更新失败"
            return True, ""
        except Exception as e:
            self._log("warning", f"reject_tune 失败: {e}")
            return False, str(e)

    async def restore_tune(self, record_id: int) -> tuple[bool, str]:
        """v0.3.10 T7：恢复一条 rejected 记录回 pending。

        调 ``tune_history.update_status(record_id, 'pending')``。
        返回 (ok, error)。
        """
        try:
            ok = await self._tune_history.update_status(record_id, "pending")
            if not ok:
                return False, f"记录 {record_id} 不存在或更新失败"
            return True, ""
        except Exception as e:
            self._log("warning", f"restore_tune 失败: {e}")
            return False, str(e)

    async def run_autotune_plan(self, body: dict) -> dict:
        """v0.3.10 T7：两轮模式第二轮——基于已生成 diagnosis 输出 plan。

        body 字段：record_id (必填) / style / guidance。
        调 ``llm_autotune_plan(record_id, style=, guidance=, source='manual')``。
        """
        try:
            record_id = body.get("record_id")
            if record_id is None:
                return {"ok": False, "error": "缺少 record_id"}
            try:
                record_id = int(record_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "record_id 必须是整数"}
            style = str(body.get("style", "") or "")
            guidance = str(body.get("guidance", "") or "")
            return await self.llm_autotune_plan(
                record_id, style=style, guidance=guidance, source="manual"
            )
        except Exception as e:
            self._log("warning", f"run_autotune_plan 失败: {e}")
            return {"ok": False, "error": f"方案轮失败: {e}"}

    def get_export_view(self) -> dict:
        """导出完整配置+决策记录+疲劳+兴趣的 JSON 供 AI 辅助调参（F11）。"""
        cfg = self._config_getter()
        # 移除特殊键
        export_cfg = {k: v for k, v in cfg.items() if k not in self._SPECIAL_KEYS}
        return {
            "config": export_cfg,
            "decisions": self.get_decisions(500),
            "fatigue": (
                self.scheduler._fatigue.snapshot(time.time()) if self.scheduler else {}
            ),
            "interests": self.get_interests_view(),
            "version": "v0.2.9",
            "export_time": time.time(),
        }
