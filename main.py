"""模块 G：主入口 main.py — AstrBot 框架对接层（唯一 import astrbot 的运行时文件）。

v0.3.0 重构：采用 mixin 多继承模式，将原 main.py 的 8 类职责拆到 6 个
core 子模块（formatting / migration / callbacks / autotune / web_bridge / commands），
本文件仅保留框架对接核心：实例化、生命周期、事件钩子、配置读写。

职责（保留）：
1. 实例化 ``ProSocialPlugin(CommandsMixin, WebBridgeMixin, TuneMixin, CallbacksMixin, Star)``。
2. ``initialize()`` / ``terminate()`` 生命周期：构造 scheduler、注册 Web API、启停调度。
3. 群消息 handler（快速路径）+ ``on_llm_request`` 钩子（长窗口注入）+
   ``after_message_sent`` 钩子（己方发言感知）。
4. ``_config_getter`` / ``_kv_get`` / ``_kv_set``：配置与 KV 读写回调。

已迁出的职责（经 mixin 经 MRO 在 self 上访问）：
- CallbacksMixin：``_log`` / ``_make_llm_fn`` / ``_make_embed_fn`` / ``_make_send_fn`` / ``_make_inject_fn``
- TuneMixin：LLM 诊断调参（``llm_autotune`` / ``run_autotune`` 等 11 个方法）
- WebBridgeMixin：Web API 注册与 WebBridge 鸭子接口（12 个方法）
- CommandsMixin：``/prosocial`` 指令组（9 个 ADMIN 子指令）
- formatting.py：``format_status`` / ``format_persona`` / ``format_scores``（模块级函数）
- migration.py：``migrate_kv_to_sqlite``（模块级 async 函数）
"""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.decision.interest import InterestManager

# 注意：必须用相对导入（from .core.xxx）。AstrBot 把本插件作为
# data.plugins.astrbot_plugin_proactive_social.main 子包加载，插件目录不在 sys.path 顶层，
# 绝对导入 `from core.xxx` 会触发 ModuleNotFoundError: No module named 'core'。
from .core.plugin.autotune import TuneMixin
from .core.plugin.callbacks import CallbacksMixin
from .core.plugin.commands import CommandsMixin
from .core.plugin.web_bridge import WebBridgeMixin
from .core.scheduler import SocialScheduler
from .core.storage.config_store import SPECIAL_KEYS, ConfigStore
from .core.storage.migration import migrate_kv_to_sqlite
from .core.storage.ratelimit import TokenBucketRateLimiter
from .core.storage.tune_controller import TuneRateLimiter
from .core.storage.tune_history import TuneHistoryStore

# 插件名（与 metadata.yaml 一致，用于 Web API 路由前缀与数据目录）
_PLUGIN_NAME = "astrbot_plugin_proactive_social"

# 不支持主动发送的平台（PRD §6.2）—— send_fn 检测到这些平台时跳过
_NO_PROACTIVE_PLATFORMS = {"qq_official", "qq_official_webhook"}


@register(_PLUGIN_NAME, "", "主动社交：向量决策驱动的多群主动插话插件", "v0.3.7")
class ProSocialPlugin(CommandsMixin, WebBridgeMixin, TuneMixin, CallbacksMixin, Star):
    """主动社交插件入口（模块 G）。

    v0.3.0 mixin 多继承：``CommandsMixin`` / ``WebBridgeMixin`` / ``TuneMixin`` /
    ``CallbacksMixin`` 提供已迁出的 30 个方法（经 MRO 在 self 上访问），``Star`` 为
    AstrBot 基类（含 ``PluginKVStoreMixin``，提供 ``get_kv_data`` / ``put_kv_data``）。
    本类保留框架对接核心：生命周期、事件钩子、配置读写、指令注册。

    v0.3.0 调整：``/prosocial`` 指令组注册（``@filter.command_group`` +
    ``@prosocial.command`` 装饰器）搬回本类，确保框架识别指令归属为 ``main`` 模块；
    处理逻辑仍在 ``CommandsMixin._handle_*`` 中，本类装饰方法仅委托调用。
    """

    # ------------------------------------------------------------------ #
    # /prosocial 指令组注册（装饰器在 main.py，处理逻辑在 CommandsMixin._handle_*）
    # ------------------------------------------------------------------ #
    @filter.command_group("prosocial")
    def prosocial(self):
        """/prosocial 指令组声明。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看调度器/状态机/跟踪列表/今日指标/回放进度。"""
        async for msg in self._handle_status(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("dryrun")
    async def cmd_dryrun(self, event: AstrMessageEvent, arg: str = ""):
        """运行时切换 DRY_RUN：/prosocial dryrun on|off。"""
        async for msg in self._handle_dryrun(event, arg):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("enable")
    async def cmd_enable(self, event: AstrMessageEvent):
        """在当前群启用主动唤醒。"""
        async for msg in self._handle_enable(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("disable")
    async def cmd_disable(self, event: AstrMessageEvent):
        """在当前群停用主动唤醒。"""
        async for msg in self._handle_disable(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("persona")
    async def cmd_persona(self, event: AstrMessageEvent, arg: str = ""):
        """查看兴趣分级摘要或重新生成：/prosocial persona show|reload。"""
        async for msg in self._handle_persona(event, arg):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("scores")
    async def cmd_scores(self, event: AstrMessageEvent, n: str = "10"):
        """查看最近 N（默认 10）条批次决策得分。"""
        async for msg in self._handle_scores(event, n):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("replay")
    async def cmd_replay(
        self, event: AstrMessageEvent, name: str = "", speed: str = "1.0"
    ):
        """历史回放：/prosocial replay <名称> [倍速] 或 /prosocial replay stop。"""
        async for msg in self._handle_replay(event, name, speed):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("fatigue")
    async def cmd_fatigue(self, event: AstrMessageEvent):
        """查看全局疲劳值/级别/影响因子。"""
        async for msg in self._handle_fatigue(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("tune")
    async def cmd_tune(self, event: AstrMessageEvent, arg: str = ""):
        """LLM 诊断调参：/prosocial tune [style|apply|force|status]。"""
        async for msg in self._handle_tune(event, arg):
            yield msg

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 数据目录：data/plugin_data/astrbot_plugin_proactive_social/
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
        # ConfigStore：普通参数的默认值 + SQLite 持久化覆盖 + 内存缓存（v0.2.7）。
        # __init__ 时用 DEFAULT_CONFIG 填充缓存，保证同步可读；SQLite 覆盖在 initialize 中加载。
        # 使用独立 SQLite 数据库（config.db），不再依赖 AstrBot KV 存储，
        # 彻底解决插件重载后配置丢失的问题。
        self._config_store = ConfigStore(self.data_dir / "config.db")
        # 特殊选择器键（chat_provider_id 等）仍由 AstrBotConfig 原生承载，不走 ConfigStore
        self._SPECIAL_KEYS = SPECIAL_KEYS
        # 兴趣管理器（启动时仅创建，加载在 scheduler.start 内触发）
        self.interest_mgr = InterestManager(self.data_dir, self._log)
        # 限流器（速率在 scheduler 主循环里实时同步配置）
        rate_per_min = int(
            self._config_store.get().get("embedding_rate_limit_per_min", 30)
        )
        self.rate_limiter = TokenBucketRateLimiter(rate_per_min)
        # scheduler 在 initialize 中构造（需要 self 的回调闭包）
        self.scheduler: SocialScheduler | None = None
        # llm_fn / embed_fn 存为属性，供 persona reload 与 on_llm_request 钩子复用
        self._llm_fn = None
        self._embed_fn = None
        # v0.2.8 F1：平台 bot self_id 缓存——on_group_message 收集，inject_fn 取用。
        # key 为平台实例 id（event.get_platform_id() / umo 首段 / meta().id，三者一致），
        # 不用 event.get_platform_name()（那是平台类型名 aiocqhttp，多实例场景会冲突）。
        self._platform_self_ids: dict[str, str] = {}
        # v0.2.8 F1：user_id -> nickname 缓存——on_group_message 收集，inject_fn 构造
        # 合成消息时取用，让 Sender 显示为真实触发用户而非虚拟「群聊动态」。
        self._user_nicknames: dict[str, str] = {}
        # v0.2.8 F1：注入消息 message_id -> hint 映射，on_llm_request 消费后 pop。
        self._pending_hints: dict[str, str] = {}
        # v0.2.8 F3：最近一次 llm_autotune analyze 的建议 patch，供 /prosocial tune apply 复用。
        # v0.2.9 F1/F2：扩展为含三段——suggested_patch（标量）/ suggested_keywords_patch / persona_revision。
        self._last_tune_suggestion: dict | None = None
        # v0.2.9 F4：LLM 调参速率限制器单例（冷却 + 日上限）。
        # state 在 initialize/terminate 经 ConfigStore.get_kv/set_kv 持久化到 SQLite 键 "tune_rate_state"。
        self._tune_limiter = TuneRateLimiter()
        # v0.3.6 F3：LLM 调参历史持久化（独立 SQLite，与 config.db 分离）
        self._tune_history = TuneHistoryStore(self.data_dir / "tune_history.db")

    # ------------------------------------------------------------------ #
    # initialize：构造回调 + scheduler + Web API + 启动
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """AstrBot 加载完成后调用：构造注入回调、scheduler、注册 Web API、启动调度。"""
        try:
            # v0.3.0 清理 mixin 重构遗留的孤儿 handler：
            # v0.3.0 曾在 core/commands.py 中用 @filter.command_group 注册过指令，
            # 框架 _unbind_plugin 只清理 handler_module_path == main 的 handler，
            # 导致 core.commands 模块的孤儿 handler 残留并引发指令冲突。
            try:
                from astrbot.core.star.star_handler import star_handlers_registry

                own_module = self.__class__.__module__  # ...main
                plugin_prefix = (
                    own_module.rsplit(".", 1)[0] + "."
                )  # ...astrbot_plugin_proactive_social.
                orphans = [
                    h
                    for h in star_handlers_registry
                    if h.handler_module_path.startswith(plugin_prefix)
                    and h.handler_module_path != own_module
                ]
                for h in orphans:
                    star_handlers_registry.remove(h)
                if orphans:
                    self._log(
                        "info",
                        f"已清理 {len(orphans)} 个遗留孤儿 handler（来自 mixin 重构前）",
                    )
            except Exception as e:
                self._log("warning", f"清理孤儿 handler 失败（可忽略）: {e}")

            # 从 SQLite 加载配置覆盖项（不再依赖 AstrBot KV 存储）
            try:
                await self._config_store.load()
                self._log("info", "SQLite 配置已加载")
            except Exception as e:
                self._log("warning", f"加载 SQLite 配置失败，使用默认值: {e}")

            # v0.2.7 数据迁移：从旧 AstrBot KV 迁移到 SQLite（仅首次执行）
            await migrate_kv_to_sqlite(self)

            # v0.2.9 F4：恢复 LLM 调参速率限制器状态（冷却 + 日计数连续）
            try:
                tune_state = await self._config_store.get_kv("tune_rate_state", {})
                self._tune_limiter.restore(
                    tune_state if isinstance(tune_state, dict) else {}
                )
            except Exception as e:
                self._log("warning", f"tune_limiter 恢复失败: {e}")

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
                # v0.2.8 F1：主动回复注入回调——构造 AstrBotMessage 走 platform_inst.handle_msg
                # 标准管线（追踪+历史自动记录）。None 时所有主动回复走旧路径（行为不变）。
                inject_fn=self._make_inject_fn(),
                # v0.2.9 F3：触发率越界自动调参回调——scheduler._maybe_autotune 经此触发
                # llm_autotune("analyze")。回调内自行处理速率限制（force=False）。
                autotune_trigger_fn=self._autotune_trigger,
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
        """合并 ConfigStore 缓存（普通参数）+ AstrBotConfig（特殊选择器）。

        ConfigStore.get() 返回内存缓存引用（热更新语义：set_many 改缓存后立即生效）；
        特殊选择器（chat_provider_id 等）仍从 self.config 读取（主面板原生渲染）。
        """
        cfg = dict(self._config_store.get())
        for k in self._SPECIAL_KEYS:
            if k in self.config:
                cfg[k] = self.config[k]
        return cfg

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 全部插件加载完成后：加载兴趣人工过滤列表。

        配置持久化已迁移到独立 SQLite 数据库（config.db），
        在 initialize() 中直接加载，不再依赖 AstrBot KV 存储。
        interest_rejected 也从 SQLite 加载（复用 config.db）。
        """
        # 加载兴趣 rejected 列表（F20）
        try:
            raw = await self._config_store.get_kv("interest_rejected")
            if raw:
                self.interest_mgr.set_rejected(raw)
                self._log("info", "兴趣 rejected 列表已加载")
        except Exception as e:
            self._log("warning", f"加载兴趣 rejected 列表失败: {e}")

    async def _kv_get(self, key: str, default=None):
        """KV 读取回调，包 ConfigStore.get_kv（SQLite 持久化）。"""
        return await self._config_store.get_kv(key, default)

    async def _kv_set(self, key: str, value) -> None:
        """KV 写入回调，包 ConfigStore.set_kv（SQLite 持久化）。"""
        await self._config_store.set_kv(key, value)

    # ------------------------------------------------------------------ #
    # 群消息 handler（快速路径）
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息快速路径：提取字段 -> scheduler.on_message，不 yield、不做重活。"""
        try:
            # v0.2.8 F1：合成消息自事件规避——prosocial: 前缀的 message_id 是 _make_inject_fn
            # 注入管线产生的合成事件，内容已由原始真实事件记录过，此处直接 return 避免双缓冲/双决策。
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            if msg_id.startswith("prosocial:"):
                return

            # v0.2.8 F1：缓存平台 bot self_id（inject_fn 构造 AstrBotMessage 时取用）。
            # key 用 platform_id（= umo 首段 = meta().id），不用 platform_name（类型名，多实例冲突）。
            platform_id = ""
            try:
                platform_id = event.get_platform_id() or ""
            except Exception:
                pass
            self_id = event.get_self_id()
            if platform_id and self_id:
                self._platform_self_ids[platform_id] = str(self_id)

            # 跳过 bot 自身消息（避免回声，PRD §2.3）
            if self_id and event.get_sender_id() == self_id:
                return
            if self.scheduler is None:
                return
            group_id = event.get_group_id() or ""
            umo = event.unified_msg_origin or ""
            user_id = event.get_sender_id() or ""
            nickname = event.get_sender_name() or ""
            # v0.2.8 F1：缓存 user_id -> nickname，inject_fn 构造合成消息 Sender 时取用
            if user_id and nickname:
                self._user_nicknames[user_id] = nickname
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
        """v0.2.8 F1：检测 prosocial: 前缀的注入消息，把 hint 注入 extra_user_content_parts。

        非注入消息（普通被动 @）保持现状（无操作）——长窗口注入已迁移到 scheduler.run_batch。
        """
        try:
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            hint = self._pending_hints.pop(msg_id, None)
            if not hint:
                return  # 非注入消息：保持现状
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=f"[主动社交接话提示] {hint}")
            try:
                # v4.24.0+ 标记为临时内容，不写入对话历史；低版本无此方法则忽略
                part.mark_as_temp()
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
        except Exception as e:
            self._log("warning", f"[prosocial] on_llm_request 注入 hint 失败: {e}")

    # ------------------------------------------------------------------ #
    # after_message_sent 钩子：感知己方发言
    # ------------------------------------------------------------------ #
    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """从 result.chain 提取 Plain 文本 -> scheduler.on_bot_sent（记录嵌入、转 EXPECTING_REPLY）。

        v0.2.8 F1：按触发消息 message_id 前缀分类——``prosocial:`` 前缀为主动注入消息的回复，
        走 ``reply_type="active"`` / ``is_proactive=True``（正确疲劳档位）；普通被动 @ 回复
        保持 ``passive``。注入模式下 scheduler._dispatch_proactive 已计数 proactive_sends，
        此处仅完成疲劳消耗/惯性/跟踪/瞥眼，不重复计数（on_bot_sent 内部防重）。
        """
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
            # v0.2.8 F1：prosocial: 前缀 → active 主动回复；否则 passive 被动回复
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            is_prosocial = msg_id.startswith("prosocial:")
            await self.scheduler.on_bot_sent(
                group_id=group_id,
                text=text,
                ts=time.time(),
                reply_type="active" if is_prosocial else "passive",
                is_proactive=is_prosocial,
            )
        except Exception as e:
            self._log("warning", f"after_message_sent 异常: {e}")

    # ------------------------------------------------------------------ #
    # terminate
    # ------------------------------------------------------------------ #
    async def terminate(self):
        """插件卸载/停用时调用：scheduler.stop() + 持久化调参状态 + config_store.close()。"""
        try:
            if self.scheduler is not None:
                await self.scheduler.stop()
        except Exception as e:
            self._log("warning", f"terminate scheduler.stop 异常: {e}")
        # v0.2.9 F4：持久化 LLM 调参速率限制器状态（冷却 + 日计数）
        try:
            await self._config_store.set_kv(
                "tune_rate_state", self._tune_limiter.state()
            )
        except Exception as e:
            self._log("warning", f"tune_limiter 持久化失败: {e}")
        try:
            await self._config_store.close()
        except Exception as e:
            self._log("warning", f"terminate config_store.close 异常: {e}")
        try:
            await self._tune_history.close()
        except Exception as e:
            self._log("warning", f"terminate tune_history.close 异常: {e}")
