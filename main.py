"""模块 G：主入口 main.py — AstrBot 框架对接层（唯一 import astrbot 的运行时文件）。

职责：
1. 实例化 Star 子类 ``ProSocialPlugin``，把 ``core/`` 模块与 AstrBot 框架对接。
2. 构造注入回调（llm_fn / embed_fn / send_fn / kv_get_fn / kv_set_fn / config_getter /
   log_fn）传给 ``SocialScheduler``，封装 AstrBot 的 LLM / 嵌入 / 发送 / KV / 日志能力。
3. 群消息 handler（快速路径）+ ``on_llm_request`` 钩子（长窗口注入）+
   ``after_message_sent`` 钩子（己方发言感知）。
4. 指令组 ``/prosocial``（status / dryrun / enable / disable / persona / scores / replay，
   全部 ADMIN 权限）。
5. 注册 7 个 Web API（包装 ``core/web.py`` 的 ``build_handlers``）。
6. ``terminate()``：``scheduler.stop()``（cancel+await 所有任务，持久化）。

设计要点：
- ``AstrBotConfig`` 是 live dict 引用，``config_getter`` 直接返回 ``self.config``，
  Dashboard 写入后即时生效（决策引擎每次决策实时读取）。
- 嵌入 provider 解析：``embedding_provider_id`` 为空时用 ``get_all_embedding_providers()[0]``。
- LLM provider 解析：``chat_provider_id`` 为空时用 ``get_using_provider(None)`` 取全局默认，
  再退到 ``get_all_providers()[0]``（主动发言无 umo 上下文，不能用 ``get_current_chat_provider_id``）。
- ``send_fn``：从 umo 解析平台名，``qq_official`` / ``qq_official_webhook`` 平台跳过（PRD §6.2）。
- ``on_llm_request`` 长窗口注入：受控访问 ``scheduler._get_group(group_id)`` 取 ``GroupContext``，
  用短窗口文本嵌入作为 anchor 调 ``select_long_relevant``（可选 LLM 摘要）。
- Web API 包装：把 web.py 的 ``(status, json)`` 转为 ``json_response``；**所有响应统一 200**，
  错误经 ``{"ok":false,"error":...}`` 结构化返回，让前端 bridge 拿到完整 JSON
  （plugin-pages.md 提到 bridge 对非 2xx reject，降级为 200 + ok=false 让前端能读取结构化错误）。
- 后台任务健壮性：所有 handler / hook try/except，单点失败 log 不抛、不影响框架。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 注意：必须用相对导入（from .core.xxx）。AstrBot 把本插件作为
# data.plugins.astrbot_plugin_proactive_social.main 子包加载，插件目录不在 sys.path 顶层，
# 绝对导入 `from core.xxx` 会触发 ModuleNotFoundError: No module named 'core'。
from .core.interest import InterestManager
from .core.prompts import build_summary_prompt
from .core.ratelimit import TokenBucketRateLimiter
from .core.scheduler import SocialScheduler
from .core.web import build_handlers

# 插件名（与 metadata.yaml 一致，用于 Web API 路由前缀与数据目录）
_PLUGIN_NAME = "astrbot_plugin_proactive_social"

# 不支持主动发送的平台（PRD §6.2）—— send_fn 检测到这些平台时跳过
_NO_PROACTIVE_PLATFORMS = {"qq_official", "qq_official_webhook"}

# Web API 可编辑配置键白名单（PRD F7），其他键不允许经 Web 修改
_CONFIG_EDITABLE_KEYS: frozenset[str] = frozenset(
    {
        "dry_run",
        "base_threshold",
        "personal_threshold",
        "hate_similarity_threshold",
        "w_int",
        "w_topic",
        "w_resp",
        "w_cooldown",
        "w_silence",
        "core_interest_modifier",
        "general_interest_modifier",
        "edge_interest_modifier",
        "expecting_modifier",
        "batch_interval_min",
        "batch_interval_max",
        "cooldown_messages",
        "expecting_duration",
        "personal_track_timeout",
        "track_irrelevant_msgs",
        "schedule",
        "poll_interval",
        "poll_jitter",
        "monitoring_duration",
        "group_cooldown",
        "glance_enable",
        "glance_group_count",
        "glance_min_score",
        "hot_group_msg_limit",
        "silent_group_minutes",
        # v0.2 双通道融合 / 疲劳 / 惯性 / 等待窗口（§5）
        "enable_rule_channel",
        "enable_vector_channel",
        "fusion_weight_rule",
        "dynamic_fusion_enabled",
        "dynamic_alpha_wake",
        "dynamic_alpha_short_expect",
        "rule_direct_wakeup_words",
        "rule_context_wakeup_words",
        "rule_context_threshold",
        "rule_question_enabled",
        "rule_question_threshold",
        "rule_score_normalize",
        "fatigue_recovery_rate",
        "fatigue_limit",
        "fatigue_cost_active",
        "fatigue_cost_passive",
        "fatigue_cost_track",
        "fatigue_cost_glance",
        "fatigue_high_modifier",
        "fatigue_medium_modifier",
        "fatigue_suppress_enabled",
        "after_reply_probability",
        "probability_duration",
        "wait_window_duration_ms",
        "wait_window_max_extra",
        "proactive_temp_boost",
        "proactive_boost_duration",
    }
)

# 配置项类型/范围校验规则（key -> (type, min, max)）；min/max 为 None 表示不校验
# schedule 单独按 list 校验，不在此表中
_CONFIG_VALIDATORS: dict[str, tuple[type, float | None, float | None]] = {
    "dry_run": (bool, None, None),
    "base_threshold": (float, 0.0, 2.0),
    "personal_threshold": (float, 0.0, 2.0),
    "hate_similarity_threshold": (float, 0.0, 1.0),
    "w_int": (float, 0.0, 5.0),
    "w_topic": (float, 0.0, 5.0),
    "w_resp": (float, 0.0, 5.0),
    "w_cooldown": (float, 0.0, 5.0),
    "w_silence": (float, 0.0, 5.0),
    "core_interest_modifier": (float, 0.0, 3.0),
    "general_interest_modifier": (float, 0.0, 3.0),
    "edge_interest_modifier": (float, 0.0, 3.0),
    "expecting_modifier": (float, 0.0, 2.0),
    "batch_interval_min": (float, 0.1, 60.0),
    "batch_interval_max": (float, 0.1, 60.0),
    "cooldown_messages": (int, 0, 1000),
    "expecting_duration": (int, 0, 3600),
    "personal_track_timeout": (int, 0, 3600),
    "track_irrelevant_msgs": (int, 0, 100),
    "poll_interval": (int, 1, 86400),
    "poll_jitter": (int, 0, 86400),
    "monitoring_duration": (int, 1, 86400),
    "group_cooldown": (int, 0, 86400),
    "glance_enable": (bool, None, None),
    "glance_group_count": (int, 1, 50),
    "glance_min_score": (float, 0.0, 1.0),
    "hot_group_msg_limit": (int, 1, 10000),
    "silent_group_minutes": (int, 0, 1440),
    # v0.2 双通道融合 / 疲劳 / 惯性 / 等待窗口校验
    # 注意：rule_direct_wakeup_words / rule_context_wakeup_words 为 list 类型，
    # 不加入此表，在 set_config_view 中按 list 特判（同 schedule）。
    "enable_rule_channel": (bool, None, None),
    "enable_vector_channel": (bool, None, None),
    "fusion_weight_rule": (float, 0.0, 1.0),
    "dynamic_fusion_enabled": (bool, None, None),
    "dynamic_alpha_wake": (float, 0.0, 1.0),
    "dynamic_alpha_short_expect": (float, 0.0, 1.0),
    "rule_context_threshold": (int, 0, 150),
    "rule_question_enabled": (bool, None, None),
    "rule_question_threshold": (int, 0, 100),
    "rule_score_normalize": (float, 1.0, 1000.0),
    "fatigue_recovery_rate": (float, 0.0, 10.0),
    "fatigue_limit": (float, 0.0, 100.0),
    "fatigue_cost_active": (float, 0.0, 10.0),
    "fatigue_cost_passive": (float, 0.0, 10.0),
    "fatigue_cost_track": (float, 0.0, 10.0),
    "fatigue_cost_glance": (float, 0.0, 10.0),
    "fatigue_high_modifier": (float, 0.0, 3.0),
    "fatigue_medium_modifier": (float, 0.0, 3.0),
    "fatigue_suppress_enabled": (bool, None, None),
    "after_reply_probability": (float, 0.0, 1.0),
    "probability_duration": (int, 0, 3600),
    "wait_window_duration_ms": (int, 0, 60000),
    "wait_window_max_extra": (int, 0, 100),
    "proactive_temp_boost": (float, 0.0, 1.0),
    "proactive_boost_duration": (int, 0, 3600),
}


@register(_PLUGIN_NAME, "", "主动社交：向量决策驱动的多群主动插话插件", "v0.2.0")
class ProSocialPlugin(Star):
    """主动社交插件入口（模块 G）。

    继承 ``Star``（含 ``PluginKVStoreMixin``，提供 ``get_kv_data`` / ``put_kv_data``）。
    唯一 import astrbot 的运行时文件，把 ``core/`` 模块与 AstrBot 框架对接。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 数据目录：data/plugin_data/astrbot_plugin_proactive_social/
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
        # 兴趣管理器（启动时仅创建，加载在 scheduler.start 内触发）
        self.interest_mgr = InterestManager(self.data_dir, self._log)
        # 限流器（速率在 scheduler 主循环里实时同步配置）
        rate_per_min = int(self.config.get("embedding_rate_limit_per_min", 30))
        self.rate_limiter = TokenBucketRateLimiter(rate_per_min)
        # scheduler 在 initialize 中构造（需要 self 的回调闭包）
        self.scheduler: SocialScheduler | None = None
        # llm_fn / embed_fn 存为属性，供 persona reload 与 on_llm_request 钩子复用
        self._llm_fn = None
        self._embed_fn = None

    # ------------------------------------------------------------------ #
    # 日志回调（注入 core 模块用）
    # ------------------------------------------------------------------ #
    def _log(self, level: str, msg: str) -> None:
        """统一日志回调：(level, msg) -> None，level ∈ info/warning/error/debug。"""
        fn = getattr(
            logger,
            level if level in ("info", "warning", "error", "debug") else "info",
            logger.info,
        )
        fn(f"[ProSocial] {msg}")

    # ------------------------------------------------------------------ #
    # initialize：构造回调 + scheduler + Web API + 启动
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """AstrBot 加载完成后调用：构造注入回调、scheduler、注册 Web API、启动调度。"""
        try:
            self._llm_fn = self._make_llm_fn()
            self._embed_fn = self._make_embed_fn()

            self.scheduler = SocialScheduler(
                config_getter=self._config_getter,
                interest_mgr=self.interest_mgr,
                send_fn=self._make_send_fn(),
                llm_fn=self._llm_fn,
                embed_fn=self._embed_fn,
                rate_limiter=self.rate_limiter,
                kv_get_fn=self._kv_get,
                kv_set_fn=self._kv_set,
                log_fn=self._log,
                data_dir=self.data_dir,
            )

            # 注册 Web API（7 个）
            self._register_web_apis()

            # 启动调度器（start 内会 load metrics/decision_log、ensure interest、起主循环）
            await self.scheduler.start()
            self._log("info", "initialize 完成")
        except Exception as e:
            # 不抛——避免插件加载失败拖垮 AstrBot；但 scheduler 未起则插件无功能
            self._log("error", f"initialize 失败: {e}")

    def _config_getter(self) -> dict:
        """返回 live 配置 dict 引用（AstrBotConfig 继承 dict，Dashboard 写入后即时生效）。"""
        return self.config

    async def _kv_get(self, key: str, default=None):
        """KV 读取回调，包 self.get_kv_data（async）。"""
        return await self.get_kv_data(key, default)

    async def _kv_set(self, key: str, value) -> None:
        """KV 写入回调，包 self.put_kv_data（async）。"""
        await self.put_kv_data(key, value)

    # ------------------------------------------------------------------ #
    # 注入回调构造
    # ------------------------------------------------------------------ #
    def _make_llm_fn(self):
        """构造 llm_fn(prompt) -> str：解析 chat provider 并调 llm_generate。"""

        async def llm_fn(prompt: str) -> str:
            try:
                prov_id = str(self.config.get("chat_provider_id", "") or "")
                if not prov_id:
                    # 主动发言无 umo 上下文，用全局默认 chat provider
                    try:
                        prov = self.context.get_using_provider(None)
                        if prov is not None:
                            prov_id = prov.meta().id
                    except Exception:
                        prov = None
                if not prov_id:
                    # 再退到第一个 chat provider
                    provs = self.context.get_all_providers()
                    if provs:
                        try:
                            prov_id = provs[0].meta().id
                        except Exception:
                            prov_id = ""
                if not prov_id:
                    self._log("warning", "llm_fn: 无可用 chat provider，跳过生成")
                    return ""
                resp = await self.context.llm_generate(
                    chat_provider_id=prov_id, prompt=prompt
                )
                return resp.completion_text or ""
            except Exception as e:
                self._log("warning", f"llm_fn 调用失败: {e}")
                return ""

        return llm_fn

    def _make_embed_fn(self):
        """构造 embed_fn(texts) -> list[list[float]]：解析 embedding provider 并批量嵌入。"""

        async def embed_fn(texts: list[str]) -> list[list[float]]:
            if not texts:
                return []
            try:
                prov_id = str(self.config.get("embedding_provider_id", "") or "")
                prov = None
                if prov_id:
                    prov = self.context.get_provider_by_id(prov_id)
                if prov is None:
                    # 回退到第一个可用 embedding provider
                    provs = self.context.get_all_embedding_providers()
                    if not provs:
                        self._log("warning", "embed_fn: 无可用 embedding provider")
                        return []
                    prov = provs[0]
                # 优先批量嵌入；失败回退到逐条
                try:
                    embs = await prov.get_embeddings(texts)
                    if embs:
                        return embs
                except Exception as e:
                    self._log(
                        "debug", f"embed_fn: get_embeddings 批量失败，回退逐条: {e}"
                    )
                # 逐条回退（单条失败用空列表占位，长度对齐由调用方处理）
                result: list[list[float]] = []
                for t in texts:
                    try:
                        result.append(await prov.get_embedding(t))
                    except Exception:
                        result.append([])
                return result
            except Exception as e:
                self._log("warning", f"embed_fn 调用失败: {e}")
                return []

        return embed_fn

    def _make_send_fn(self):
        """构造 send_fn(group_umo, text) -> bool：跳过 qq_official，否则调 send_message。"""

        async def send_fn(group_umo: str, text: str) -> bool:
            try:
                if not group_umo:
                    self._log("warning", "send_fn: umo 为空，跳过发送")
                    return False
                # umo 格式通常为 platform_name:...，取首段判断平台
                platform_name = group_umo.split(":", 1)[0]
                if platform_name in _NO_PROACTIVE_PLATFORMS:
                    self._log(
                        "warning", f"send_fn: 平台 {platform_name} 不支持主动发送，跳过"
                    )
                    return False
                chain = MessageChain().message(text)
                ok = await self.context.send_message(group_umo, chain)
                return bool(ok)
            except Exception as e:
                self._log("warning", f"send_fn 调用失败: {e}")
                return False

        return send_fn

    # ------------------------------------------------------------------ #
    # 群消息 handler（快速路径）
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息快速路径：提取字段 -> scheduler.on_message，不 yield、不做重活。"""
        try:
            # 跳过 bot 自身消息（避免回声，PRD §2.3）
            self_id = event.get_self_id()
            if self_id and event.get_sender_id() == self_id:
                return
            if self.scheduler is None:
                return
            group_id = event.get_group_id() or ""
            umo = event.unified_msg_origin or ""
            user_id = event.get_sender_id() or ""
            nickname = event.get_sender_name() or ""
            text = event.message_str or ""
            ts = getattr(event.message_obj, "timestamp", None) or int(time.time())
            is_wake = bool(getattr(event, "is_at_or_wake_command", False))
            await self.scheduler.on_message(
                group_id=group_id,
                umo=umo,
                user_id=user_id,
                nickname=nickname,
                text=text,
                ts=float(ts),
                is_wake=is_wake,
            )
        except Exception as e:
            self._log("error", f"on_group_message 异常: {e}")
        # 不 yield、不 stop_event：让框架继续处理被动 @ 的 LLM 回复

    # ------------------------------------------------------------------ #
    # on_llm_request 钩子：长窗口上下文注入（仅被动 @ 时）
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """被动 @ 时注入长窗口相关性上下文到 extra_user_content_parts（不污染 system_prompt）。"""
        try:
            if self.scheduler is None or self._embed_fn is None:
                return
            # 仅被动 @ 时注入
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return
            group_id = event.get_group_id() or ""
            if not group_id or not self.scheduler.group_enabled(group_id):
                return
            # 受控访问 scheduler._get_group 取 GroupContext（_get_group 是惰性创建方法，
            # 此处仅借用读取长窗口，不修改 _groups 内部状态）
            g = self.scheduler._get_group(group_id)
            ctx = g.get("context")
            if ctx is None:
                return

            cfg = self.config
            top_n = int(cfg.get("long_window_top_n", 6))
            long_summarize = bool(cfg.get("long_window_summarize", False))

            # 用短窗口文本嵌入作为 anchor（1 次嵌入调用；被动回复偶尔一次，不走限流）
            short_text = ctx.short_window_text()
            if not short_text:
                return
            anchor_embs = await self._embed_fn([short_text])
            anchor_emb = anchor_embs[0] if anchor_embs else None

            long_texts = (
                ctx.select_long_relevant(anchor_emb, top_n) if anchor_emb else []
            )
            if not long_texts:
                return
            long_window_text = "\n".join(long_texts)

            if long_summarize:
                # 用 LLM 生成 3~5 句摘要替代原文（附录 B）
                summary_prompt = build_summary_prompt(long_window_text, short_text)
                summary = await self._llm_fn(summary_prompt)
                context_text = f"相关历史摘要：\n{summary}" if summary else ""
            else:
                context_text = f"相关历史背景：\n{long_window_text}"

            if context_text:
                req.extra_user_content_parts.append(TextPart(text=context_text))
        except Exception as e:
            self._log("warning", f"on_llm_request 注入失败: {e}")

    # ------------------------------------------------------------------ #
    # after_message_sent 钩子：感知己方发言
    # ------------------------------------------------------------------ #
    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """从 result.chain 提取 Plain 文本 -> scheduler.on_bot_sent（记录嵌入、转 EXPECTING_REPLY）。"""
        try:
            if self.scheduler is None:
                return
            result = event.get_result()
            if result is None:
                return
            chain = getattr(result, "chain", []) or []
            # 拼接所有 Plain 组件的 text
            text = "".join(
                c.text
                for c in chain
                if hasattr(c, "text") and isinstance(getattr(c, "text", None), str)
            )
            if not text:
                return
            group_id = event.get_group_id() or ""
            if not group_id:
                return
            await self.scheduler.on_bot_sent(
                group_id=group_id,
                text=text,
                ts=time.time(),
                reply_type="passive",
                is_proactive=False,
            )
        except Exception as e:
            self._log("warning", f"after_message_sent 异常: {e}")

    # ------------------------------------------------------------------ #
    # 指令组 /prosocial（全部 ADMIN 权限）
    # ------------------------------------------------------------------ #
    # 注意：register_permission_type 只作用于当前函数的 handler_md，不传播到子指令。
    # 因此每个子指令单独加 @filter.permission_type(ADMIN)。
    @filter.command_group("prosocial")
    def prosocial(self):
        """指令组声明（函数体留空）。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看调度器/状态机/跟踪列表/今日指标/回放进度。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            status = self.scheduler.get_status()
            yield event.plain_result(self._format_status(status))
        except Exception as e:
            yield event.plain_result(f"获取状态失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("dryrun")
    async def cmd_dryrun(self, event: AstrMessageEvent, arg: str = ""):
        """运行时切换 DRY_RUN：/prosocial dryrun on|off。"""
        try:
            arg = (arg or "").strip().lower()
            if arg not in ("on", "off"):
                yield event.plain_result("用法: /prosocial dryrun on|off")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            # 受控访问 scheduler._dry_run_override（运行时覆盖，不污染配置文件）
            self.scheduler._dry_run_override = arg == "on"
            yield event.plain_result(
                f"DRY_RUN 已{'开启' if arg == 'on' else '关闭'}（运行时覆盖）"
            )
        except Exception as e:
            yield event.plain_result(f"切换 dryrun 失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("enable")
    async def cmd_enable(self, event: AstrMessageEvent):
        """在当前群启用主动唤醒（快捷开关，需群在白名单范围内或 mode=all）。"""
        try:
            group_id = event.get_group_id() or ""
            if not group_id:
                yield event.plain_result("仅在群内可用")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            await self.scheduler.set_group_enabled(group_id, True)
            yield event.plain_result("已在本群启用主动唤醒")
        except Exception as e:
            yield event.plain_result(f"启用失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("disable")
    async def cmd_disable(self, event: AstrMessageEvent):
        """在当前群停用主动唤醒（快捷开关）。"""
        try:
            group_id = event.get_group_id() or ""
            if not group_id:
                yield event.plain_result("仅在群内可用")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            await self.scheduler.set_group_enabled(group_id, False)
            yield event.plain_result("已在本群停用主动唤醒")
        except Exception as e:
            yield event.plain_result(f"停用失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("persona")
    async def cmd_persona(self, event: AstrMessageEvent, arg: str = ""):
        """查看兴趣分级摘要或重新生成：/prosocial persona show|reload。"""
        try:
            arg = (arg or "").strip().lower()
            if arg == "show":
                summary = self.interest_mgr.summary()
                yield event.plain_result(self._format_persona(summary))
            elif arg == "reload":
                if self._llm_fn is None or self._embed_fn is None:
                    yield event.plain_result("调度器未启动，无法重载")
                    return
                yield event.plain_result(
                    "开始重新生成兴趣语料（1 次 LLM + 批量嵌入），请稍候..."
                )
                try:
                    persona_text = str(self.config.get("persona_text", ""))
                    persona_knowledge = str(self.config.get("persona_knowledge", ""))
                    await self.interest_mgr.regenerate(
                        persona_text, persona_knowledge, self._llm_fn, self._embed_fn
                    )
                    yield event.plain_result("兴趣语料已重新生成")
                except Exception as e:
                    yield event.plain_result(f"重新生成失败: {e}")
            else:
                yield event.plain_result("用法: /prosocial persona show|reload")
        except Exception as e:
            yield event.plain_result(f"persona 指令失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("scores")
    async def cmd_scores(self, event: AstrMessageEvent, n: str = "10"):
        """查看最近 N（默认 10）条批次决策得分。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            try:
                limit = int(n)
            except (TypeError, ValueError):
                limit = 10
            if limit <= 0:
                limit = 10
            # 受控访问 scheduler._decision_log.recent（决策日志读取）
            decisions = self.scheduler._decision_log.recent(limit)
            yield event.plain_result(self._format_scores(decisions))
        except Exception as e:
            yield event.plain_result(f"获取决策记录失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("replay")
    async def cmd_replay(
        self, event: AstrMessageEvent, name: str = "", speed: str = "1.0"
    ):
        """历史回放：/prosocial replay <名称> [倍速] 或 /prosocial replay stop。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            name = (name or "").strip()
            if not name:
                # 列出可用回放文件
                files = self.scheduler._replay_engine.list_files()
                if not files:
                    yield event.plain_result(
                        "无可用回放文件（放于 "
                        "data/plugin_data/astrbot_plugin_proactive_social/replay/*.jsonl）"
                    )
                else:
                    yield event.plain_result("可用回放文件:\n" + "\n".join(files))
                return
            if name == "stop":
                self.scheduler.stop_replay()
                yield event.plain_result("已请求停止回放")
                return
            try:
                sp = float(speed)
            except (TypeError, ValueError):
                sp = float(self.config.get("replay_speed", 1.0))
            # 回放是长任务，后台执行，立即回复
            asyncio.create_task(self.scheduler.replay(name, sp))
            yield event.plain_result(f"开始回放 {name}（倍速 {sp}，强制不发送）")
        except Exception as e:
            yield event.plain_result(f"replay 指令失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("fatigue")
    async def cmd_fatigue(self, event: AstrMessageEvent):
        """查看全局疲劳值/级别/影响因子（v0.2）。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            snap = self.scheduler._fatigue.snapshot()
            mod = self.scheduler._fatigue.threshold_modifier()
            suppress = self.scheduler._fatigue.should_suppress(False)
            lines = [
                f"疲劳值: {snap.get('value', 0)} / {snap.get('limit', 0)}",
                f"比率: {snap.get('ratio', 0):.2f} | 级别: {snap.get('level', 'none')}",
                f"阈值倍率 A_modifier: {mod:.2f}",
                f"高疲劳抑制非强制唤醒: {'是' if suppress else '否'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"fatigue 指令失败: {e}")

    # ------------------------------------------------------------------ #
    # Web API 注册与 WebBridge 实现
    # ------------------------------------------------------------------ #
    def _register_web_apis(self):
        """注册 7 个 Web API，route 加插件名前缀。"""
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
        """返回可编辑参数子集。"""
        return {
            k: self.config.get(k) for k in _CONFIG_EDITABLE_KEYS if k in self.config
        }

    async def set_config_view(self, patch: dict) -> tuple[bool, str]:
        """校验并写入配置子集，save_config 持久化。返回 (ok, error)。"""
        if not isinstance(patch, dict):
            return False, "patch 必须是 JSON 对象"
        for k, v in patch.items():
            if k not in _CONFIG_EDITABLE_KEYS:
                return False, f"不允许修改的配置项: {k}"
            if k == "schedule":
                if not isinstance(v, list):
                    return False, "schedule 必须是列表"
                continue
            if k in ("rule_direct_wakeup_words", "rule_context_wakeup_words"):
                if not isinstance(v, list):
                    return False, f"{k} 必须是列表"
                continue
            rule = _CONFIG_VALIDATORS.get(k)
            if rule is None:
                continue
            typ, lo, hi = rule
            if typ is bool:
                if not isinstance(v, bool):
                    return False, f"{k} 必须是布尔值"
            elif typ is int:
                # bool 是 int 子类，需排除
                if isinstance(v, bool) or not isinstance(v, int):
                    return False, f"{k} 必须是整数"
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    return False, f"{k} 超出范围 [{lo}, {hi}]"
            elif typ is float:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return False, f"{k} 必须是数值"
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    return False, f"{k} 超出范围 [{lo}, {hi}]"
            # 校验通过，写入 live config
            self.config[k] = v
        try:
            self.config.save_config()
        except Exception as e:
            return False, f"save_config 失败: {e}"
        return True, ""

    def get_groups_view(self) -> dict:
        """群管理面板数据：mode/whitelist/各群运行时状态。"""
        mode = str(self.config.get("group_mode", "whitelist"))
        whitelist = list(self.config.get("group_whitelist", []) or [])
        groups: list = []
        if self.scheduler is not None:
            status = self.scheduler.get_status()
            groups = status.get("groups", [])
        return {"mode": mode, "whitelist": whitelist, "groups": groups}

    async def set_groups_view(self, patch: dict) -> tuple[bool, str]:
        """更新 mode/whitelist/group_toggles，save_config 持久化。返回 (ok, error)。"""
        if not isinstance(patch, dict):
            return False, "patch 必须是 JSON 对象"
        if "mode" in patch:
            if patch["mode"] not in ("whitelist", "all"):
                return False, "mode 必须是 whitelist 或 all"
            self.config["group_mode"] = patch["mode"]
        if "whitelist" in patch:
            wl = patch["whitelist"]
            if not isinstance(wl, list) or not all(isinstance(x, str) for x in wl):
                return False, "whitelist 必须是字符串列表"
            self.config["group_whitelist"] = wl
        if "group_toggles" in patch:
            toggles = patch["group_toggles"]
            if not isinstance(toggles, dict):
                return False, "group_toggles 必须是对象"
            if self.scheduler is not None:
                for gid, enabled in toggles.items():
                    if not isinstance(enabled, bool):
                        return False, f"group_toggles[{gid}] 必须是布尔值"
                    await self.scheduler.set_group_enabled(str(gid), enabled)
        try:
            self.config.save_config()
        except Exception as e:
            return False, f"save_config 失败: {e}"
        return True, ""

    # ------------------------------------------------------------------ #
    # 指令输出格式化
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_status(status: dict) -> str:
        lines = [
            f"运行: {status.get('running', False)} | "
            f"活跃时段: {status.get('in_active_hours', False)} | "
            f"DRY_RUN: {status.get('dry_run', False)}",
            f"回放中: {status.get('replay_active', False)} | "
            f"兴趣已加载: {status.get('interest_loaded', False)} | "
            f"决策记录数: {status.get('decision_count', 0)}",
        ]
        m = status.get("metrics", {}) or {}
        lines.append(
            f"今日指标: LLM={m.get('llm_calls', 0)} "
            f"嵌入={m.get('embedding_calls', 0)} "
            f"主动发送={m.get('proactive_sends', 0)} "
            f"触发={m.get('proactive_triggered', 0)}"
        )
        # v0.2 全局疲劳摘要（紧跟今日指标行，便于一眼看到 bot 疲劳状态）
        f = status.get("fatigue", {}) or {}
        lines.append(
            f"全局疲劳: {f.get('value', 0)}/{f.get('limit', 0)} "
            f"({f.get('level', 'none')})"
        )
        cm = status.get("current_monitoring", []) or []
        lines.append(f"当前监听群: {', '.join(cm) if cm else '无'}")
        lines.append("各群状态:")
        for g in status.get("groups", []) or []:
            lines.append(
                f"  {g.get('id')}: {g.get('state')} 启用={g.get('enabled')} "
                f"跟踪={g.get('tracker_count', 0)} msg/min={g.get('msg_per_min', 0)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_persona(summary: dict) -> str:
        if not summary.get("loaded", False):
            return "兴趣数据未加载"
        lines = [
            f"人设哈希: {summary.get('persona_hash', '')} | "
            f"维度: {summary.get('dim', 0)}"
        ]
        levels = summary.get("levels", {}) or {}
        for lv in ("core", "general", "marginal", "hate"):
            info = levels.get(lv, {}) or {}
            topics = info.get("topics", []) or []
            lines.append(
                f"[{lv}] 权重={info.get('weight', 0)} 数量={info.get('count', 0)} "
                f"主题: {', '.join(topics) if topics else '无'}"
            )
        hk = summary.get("hate_keywords", []) or []
        hik = summary.get("high_interest_keywords", []) or []
        lines.append(f"高唤醒关键词: {', '.join(hik) if hik else '无'}")
        lines.append(f"反感关键词: {', '.join(hk) if hk else '无'}")
        return "\n".join(lines)

    @staticmethod
    def _format_scores(decisions: list[dict]) -> str:
        if not decisions:
            return "无决策记录"
        lines = [f"最近 {len(decisions)} 条决策（新→旧）:"]
        for i, d in enumerate(decisions, 1):
            f = d.get("factors", {}) or {}
            lines.append(
                f"{i}. [{d.get('ts', 0):.0f}] 群={d.get('group_id', '')} "
                f"score={d.get('score', 0):.3f} thr={d.get('threshold', 0):.3f} "
                f"hit={d.get('hit_level', 'none')} "
                f"int={f.get('s_int', 0):.2f} topic={f.get('s_topic', 0):.2f} "
                f"resp={f.get('s_resp', 0):.2f} cd={f.get('c_cooldown', 0):.2f} "
                f"sil={f.get('p_silence', 0):.2f} "
                f"触发={d.get('triggered', False)} "
                f"原因={d.get('suppressed_reason', '') or 'below_threshold'} "
                f"DRY={d.get('dry_run', False)} "
                f"a={d.get('score_a', 0):.2f} b={d.get('score_b', 0):.2f} "
                f"α={d.get('alpha', 0):.2f} ch={d.get('channel', '')}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # terminate
    # ------------------------------------------------------------------ #
    async def terminate(self):
        """插件卸载/停用时调用：scheduler.stop()（cancel+await 所有任务，持久化）。"""
        try:
            if self.scheduler is not None:
                await self.scheduler.stop()
        except Exception as e:
            self._log("warning", f"terminate 异常: {e}")
