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
6. ``terminate()``：``scheduler.stop()``（cancel+await 所有任务，持久化）+
   ``config_store.close()``（关闭 SQLite 连接）。

设计要点：
- ``ConfigStore``（v0.2.7）：普通参数由 ConfigStore 管理（默认值 + SQLite 持久化覆盖 +
  内存缓存），``_config_getter`` 合并 ConfigStore 缓存与 ``AstrBotConfig`` 特殊选择器
  （``chat_provider_id``），scheduler 每次决策实时读取（热更新：set_many 改缓存后立即生效）。
  配置存储使用独立 SQLite 数据库（config.db），不再依赖 AstrBot KV 存储，
  彻底解决插件重载后配置丢失的问题。
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
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 注意：必须用相对导入（from .core.xxx）。AstrBot 把本插件作为
# data.plugins.astrbot_plugin_proactive_social.main 子包加载，插件目录不在 sys.path 顶层，
# 绝对导入 `from core.xxx` 会触发 ModuleNotFoundError: No module named 'core'。
from .core.config_store import SPECIAL_KEYS, ConfigStore
from .core.interest import InterestManager
from .core.ratelimit import TokenBucketRateLimiter
from .core.scheduler import SocialScheduler
from .core.tune_controller import TuneRateLimiter
from .core.web import build_handlers

# 插件名（与 metadata.yaml 一致，用于 Web API 路由前缀与数据目录）
_PLUGIN_NAME = "astrbot_plugin_proactive_social"

# 不支持主动发送的平台（PRD §6.2）—— send_fn 检测到这些平台时跳过
_NO_PROACTIVE_PLATFORMS = {"qq_official", "qq_official_webhook"}


@register(_PLUGIN_NAME, "", "主动社交：向量决策驱动的多群主动插话插件", "v0.2.9")
class ProSocialPlugin(Star):
    """主动社交插件入口（模块 G）。

    继承 ``Star``（含 ``PluginKVStoreMixin``，提供 ``get_kv_data`` / ``put_kv_data``）。
    唯一 import astrbot 的运行时文件，把 ``core/`` 模块与 AstrBot 框架对接。
    """

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

    @classmethod
    def _writable_keys(cls) -> set[str]:
        """v0.2.9 F2：可写键 = DEFAULT_CONFIG - DENYLIST（约 70 项）。

        ConfigStore 已在顶部 import，无循环依赖；动态计算保证与 DEFAULT_CONFIG 同步。
        """
        return set(ConfigStore.DEFAULT_CONFIG) - cls.TUNE_DENYLIST

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
            # 从 SQLite 加载配置覆盖项（不再依赖 AstrBot KV 存储）
            try:
                await self._config_store.load()
                self._log("info", "SQLite 配置已加载")
            except Exception as e:
                self._log("warning", f"加载 SQLite 配置失败，使用默认值: {e}")

            # v0.2.7 数据迁移：从旧 AstrBot KV 迁移到 SQLite（仅首次执行）
            await self._migrate_kv_to_sqlite()

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

    async def _migrate_kv_to_sqlite(self) -> None:
        """v0.2.7 数据迁移：从旧 AstrBot KV 迁移到 SQLite（仅首次执行）。

        v0.2.7 之前所有数据（config / group_enable / decision_log / metrics /
        fatigue / interest_rejected）存在 AstrBot KV。v0.2.7 迁移到独立 SQLite
        后，若不迁移旧数据，配置回到默认值（group_mode=whitelist）会导致群未启用、
        不采集消息。此方法在首次启动时将旧 KV 数据读出写入 SQLite，迁移完成后
        标记 ``_kv_migrated`` 避免重复执行。
        """
        try:
            migrated = await self._config_store.get_kv("_kv_migrated")
            if migrated:
                return
        except Exception:
            return

        self._log("info", "v0.2.7 首次启动，从旧 AstrBot KV 迁移数据到 SQLite...")

        # 1. 迁移配置（旧 KV "config" 键存的是整段 JSON 字符串，SQLite 存为 "main" 键）
        try:
            old_config_raw = await self.get_kv_data("config", None)
            if old_config_raw:
                stored = (
                    json.loads(old_config_raw)
                    if isinstance(old_config_raw, str)
                    else old_config_raw
                )
                if isinstance(stored, dict):
                    # 合并合法键到缓存（绕过 set_many 校验，旧数据已通过 v0.2.6 校验）
                    cache = self._config_store.get()
                    for k in self._config_store.DEFAULT_CONFIG:
                        if k in stored:
                            cache[k] = stored[k]
                    # 写 SQLite "main" 键（与 load()/set_many() 的键名一致）
                    await self._config_store.set_kv("main", cache)
                    self._log("info", "配置已从旧 KV 迁移到 SQLite")
        except Exception as e:
            self._log("warning", f"迁移 config 失败（继续用默认值）: {e}")

        # 2. 迁移其他 KV 数据（group_enable/decision_log/metrics/fatigue）
        for key in ("group_enable", "decision_log", "metrics", "fatigue"):
            try:
                old_val = await self.get_kv_data(key, None)
                if old_val is None:
                    continue
                # AstrBot KV 自动序列化：对象读回来仍是对象；但 interest_rejected
                # 之前存的是 json.dumps 字符串，需 json.loads（下面单独处理）
                if isinstance(old_val, str):
                    try:
                        old_val = json.loads(old_val)
                    except json.JSONDecodeError:
                        continue
                await self._config_store.set_kv(key, old_val)
                self._log("info", f"{key} 已从旧 KV 迁移到 SQLite")
            except Exception as e:
                self._log("warning", f"迁移 {key} 失败: {e}")

        # 3. interest_rejected（旧 KV 存的是 json.dumps 字符串）
        try:
            old_rej = await self.get_kv_data("interest_rejected", None)
            if old_rej:
                rej = json.loads(old_rej) if isinstance(old_rej, str) else old_rej
                await self._config_store.set_kv("interest_rejected", rej)
                # 同时更新内存中的 interest_mgr
                self.interest_mgr.set_rejected(rej)
                self._log("info", "interest_rejected 已从旧 KV 迁移到 SQLite")
        except Exception as e:
            self._log("warning", f"迁移 interest_rejected 失败: {e}")

        # 4. 标记迁移完成
        try:
            await self._config_store.set_kv("_kv_migrated", True)
            self._log("info", "v0.2.7 KV→SQLite 迁移完成")
        except Exception as e:
            self._log("warning", f"标记迁移完成失败: {e}")

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
                prov_id = str(
                    self._config_getter().get("embedding_provider_id", "") or ""
                )
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

    def _make_inject_fn(self):
        """v0.2.8 F1：构造主动回复管线注入回调 ``inject_fn(umo, text, hint, group_id) -> bool``。

        解析 umo → 定位平台实例 → 构造 ``AstrBotMessage``（type=GROUP_MESSAGE、
        message_id=``prosocial:`` 前缀、sender 为虚拟用户）→ ``platform_inst.handle_msg(abm)``
        进入 AstrBot 标准消息管线（waking_check → 插件 handler → LLM stage → trace + 历史自动记录）。

        hint 暂存于 ``self._pending_hints``，``on_llm_request`` 钩子按 message_id 取出后注入
        ``req.extra_user_content_parts``（``TextPart.mark_as_temp`` 不写入历史）。

        降级：umo 非法 / 非 GroupMessage / 平台未找到 / self_id 未缓存 / handle_msg 异常 → False，
        scheduler._dispatch_proactive 收到 False 后会回退旧路径（llm_fn + send_fn）。
        """
        import uuid

        async def inject_fn(
            umo: str, text: str, hint: str, group_id: str, sender_id: str = ""
        ) -> bool:
            try:
                # 1. 解析 umo（platform_id:message_type:session_id）
                parts = umo.split(":", 2)
                if len(parts) != 3:
                    return False
                platform_id, mtype, session_id = parts
                if mtype != "GroupMessage":
                    return False

                # 2. 定位平台实例（按 meta().id 匹配；umo 首段就是 platform_id）
                platform_inst = None
                try:
                    for p in self.context.platform_manager.get_insts():
                        try:
                            if p.meta().id == platform_id:
                                platform_inst = p
                                break
                        except Exception:
                            continue
                except Exception:
                    return False
                if platform_inst is None:
                    return False

                # 3. 取缓存的 self_id（on_group_message 中从真实事件收集）
                self_id = self._platform_self_ids.get(platform_id)
                if not self_id:
                    self._log(
                        "warning",
                        f"尚无平台 {platform_id} 的 self_id 缓存，等待真实消息后重试",
                    )
                    return False

                # 4. 构造 AstrBotMessage 并注入管线
                # 延迟 import 避免插件加载阶段对 astrbot 内部路径的硬依赖
                from astrbot.api.message_components import At, Plain
                from astrbot.core.platform.astrbot_message import (
                    AstrBotMessage,
                    Group,
                    MessageMember,
                )
                from astrbot.core.platform.message_type import MessageType

                msg_id = f"prosocial:{uuid.uuid4().hex[:12]}"
                # hint 暂存，on_llm_request 钩子按 msg_id 取出并注入 extra_user_content_parts
                self._pending_hints[msg_id] = hint
                # v0.2.8：Sender 用触发主动回复的真实用户 ID（scheduler 传入），
                # nickname 从 on_group_message 缓存取；缺省回退虚拟「群聊动态」。
                # 合成消息内容为普通群聊文本（非 / 指令），不会触发 admin 指令 handler，
                # 且触发用户必为真实发言者（非 bot 自身），无权限升级/回声风险。
                sender_uid = sender_id or "prosocial"
                sender_nick = self._user_nicknames.get(sender_uid, "") or "群聊动态"
                try:
                    abm = AstrBotMessage()
                    abm.type = MessageType.GROUP_MESSAGE
                    abm.self_id = str(self_id)
                    abm.session_id = session_id
                    abm.message_id = msg_id
                    abm.group = Group(group_id=str(group_id))
                    abm.sender = MessageMember(user_id=sender_uid, nickname=sender_nick)
                    abm.message = [At(qq=str(self_id)), Plain(text)]
                    abm.message_str = text
                    abm.raw_message = {}
                    abm.timestamp = int(time.time())

                    await platform_inst.handle_msg(abm)
                    return True
                except Exception as e:
                    # 注入失败：清理 hint 缓存避免泄漏；scheduler 降级走旧路径
                    self._pending_hints.pop(msg_id, None)
                    self._log("warning", f"[prosocial] 管线注入失败: {e}")
                    return False
            except Exception as e:
                self._log("warning", f"[prosocial] inject_fn 异常: {e}")
                return False

        return inject_fn

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
                    cfg = self._config_getter()
                    persona_text = str(cfg.get("persona_text", ""))
                    persona_knowledge = str(cfg.get("persona_knowledge", ""))
                    # v0.2.8 F4：从 cfg 读兴趣生成数量传入 regenerate（原漏传恒用默认 3/12，
                    # 且 _compute_persona_hash 现已纳入数量，需用配置值才能命中新缓存）
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
                sp = float(self._config_getter().get("replay_speed", 1.0))
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @prosocial.command("tune")
    async def cmd_tune(self, event: AstrMessageEvent, arg: str = ""):
        """v0.2.9 F3/F5：LLM 诊断调参（全视野 + 速率限制）。

        ``/prosocial tune``                 分析（默认平衡风格，受速率限制）
        ``/prosocial tune proactive``       分析（偏主动风格，目标 20%-30%）
        ``/prosocial tune passive``         分析（偏被动风格，目标 5%-10%）
        ``/prosocial tune balanced``        分析（平衡风格，目标 10%-20%）
        ``/prosocial tune force``           强制分析（跳过速率限制，仍计数）
        ``/prosocial tune force proactive`` 强制分析 + 指定风格
        ``/prosocial tune status``          查看速率限制状态 + 上次建议摘要
        ``/prosocial tune apply``           应用上次 analyze 缓存的建议 patch。
        均需 ADMIN 权限。LLM 调用慢，直接 await 等待回复（指令同步阻塞可接受）。
        补充指导文本请通过 Dashboard 调参面板填写（CLI 不便输入多行文本）。
        """
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            raw_arg = (arg or "").strip()
            # v0.2.9 F5：status 子命令——显示速率限制状态 + 上次建议摘要
            if raw_arg == "status":
                yield event.plain_result(self._format_tune_status())
                return
            if raw_arg == "apply":
                result = await self.llm_autotune("apply")
                if result.get("ok"):
                    yield event.plain_result(
                        f"✅ 已应用 {result.get('updated', 0)} 项参数"
                    )
                else:
                    yield event.plain_result(
                        f"❌ 应用失败：{result.get('error', '未知')}"
                    )
                return
            # v0.2.9 F4：force 子命令——跳过速率限制（仍 record 计数）
            # 格式："force" 或 "force proactive/passive/balanced"
            force = False
            style_arg = raw_arg
            if raw_arg == "force" or raw_arg.startswith("force "):
                force = True
                style_arg = raw_arg[5:].strip()  # 去掉 "force" 前缀
            # 解析风格参数（proactive/passive/balanced），无效值回退 balanced
            style = (
                style_arg
                if style_arg in ("proactive", "passive", "balanced")
                else "balanced"
            )
            result = await self.llm_autotune("analyze", style=style, force=force)
            if not result.get("ok"):
                # v0.2.9 F4：被速率限制时回显 retry_after / 已用配额
                if result.get("error") == "rate_limited":
                    rate = result.get("rate_limit", {}) or {}
                    used = rate.get("used", 0)
                    limit = rate.get("limit", 0)
                    next_avail = int(rate.get("next_available", 0))
                    hours = next_avail // 3600
                    minutes = (next_avail % 3600) // 60
                    yield event.plain_result(
                        f"⏳ 触发速率限制（{result.get('reason', '')}）："
                        f"今日已用 {used}/{limit}，"
                        f"下次可用约 {hours}小时{minutes}分钟后"
                        f"\n（ADMIN 可用 /prosocial tune force 强制分析）"
                    )
                else:
                    yield event.plain_result(
                        f"❌ 分析失败：{result.get('error', '未知')}"
                    )
                return
            analysis = result.get("analysis", "") or ""
            patch = result.get("suggested_patch", {}) or {}
            keywords_patch = result.get("suggested_keywords_patch") or None
            persona_rev = result.get("persona_revision") or None
            expected = result.get("expected_effect", "") or ""
            patch_str = (
                "\n".join(f"  {k}: {v}" for k, v in patch.items())
                if patch
                else "  （无建议）"
            )
            extra = ""
            if keywords_patch:
                extra += "\n\n（含关键词增删建议）"
            if persona_rev:
                extra += "\n（含人设改写建议）"
            yield event.plain_result(
                f"📊 诊断结果\n\n分析：\n{analysis}\n\n建议参数：\n{patch_str}"
                f"\n\n预期效果：{expected}{extra}"
                f"\n\n应用建议：/prosocial tune apply"
            )
        except Exception as e:
            yield event.plain_result(f"tune 指令失败: {e}")

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

        body.action == "reject"  : 加 rejected 项并持久化到 KV "interest_rejected"
        body.action == "apply"   : 调 apply_rejected 重算质心
        body.action == "add"     : 调 add_item 添加关键词/示例句子
        body.action == "update"  : 调 update_item 更新关键词/示例句子
        body.action == "remove"  : 调 remove_item 移除关键词/示例句子（不进 rejected）
        """
        if not isinstance(body, dict):
            return False, "请求体必须是 JSON 对象"
        action = body.get("action")
        embed_fn = self._make_embed_fn()
        if action == "reject":
            kind = body.get("kind")
            if kind not in ("example", "keyword"):
                return False, "kind 必须是 example 或 keyword"
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

    # ------------------------------------------------------------------ #
    # v0.2.8 F3：LLM 诊断调参（WebBridge 鸭子接口 + 内部分析/应用）
    # ------------------------------------------------------------------ #

    async def run_autotune(self, body: dict) -> dict:
        """WebBridge 鸭子接口：``POST /prosocial/autotune`` 入口（v0.2.9 扩展）。

        body 字段：
        - action: ``"analyze"`` | ``"apply"``
        - patch: apply 时可选 patch（缺省用 ``self._last_tune_suggestion`` 缓存）
        - style: analyze 风格偏好（proactive/balanced/passive）
        - guidance: analyze 用户自定义补充指导
        - force: bool，跳过速率限制（v0.2.9 F4，仅 analyze 生效）
        - keywords_patch: apply 关键词增删（v0.2.9 F2，结构见 ``_apply_keywords_patch``）
        - persona_revision: apply 人设改写文本（v0.2.9 F2，合并入 persona_text 走重建路径）

        返回扁平 dict（透传给前端）：``{ok, analysis?, suggested_patch?,
        suggested_keywords_patch?, persona_revision?, expected_effect?, applied,
        updated?, regenerate?, keywords_updated?, rate_limit, error?}``。
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

        - ``action="analyze"``：``scheduler.collect_tune_stats()`` → 构造全视野 prompt →
          ``self._llm_fn`` → 解析 JSON（容错 fence）→ DENYLIST 过滤 suggested_patch →
          缓存到 ``self._last_tune_suggestion``（含三段：suggested_patch /
          suggested_keywords_patch / persona_revision）→ ``record()`` 计入速率配额 →
          返回 ``{ok, analysis, suggested_patch, suggested_keywords_patch,
          persona_revision, expected_effect, applied: False, rate_limit}``。
        - ``action="apply"``：patch 来自参数或缓存 → persona_revision 合并入 persona_text →
          DENYLIST 过滤 → 标量走 ``ConfigStore.set_many`` / persona 变更触发后台 regenerate /
          keywords_patch 走 ``interest_mgr.add_item`` + ``remove_item`` + ``apply_rejected`` →
          返回 ``{ok, applied, updated, regenerate, keywords_updated, rate_limit, error?}``。
        - ``force=True``：跳过 ``TuneRateLimiter.allow()``（仅 analyze 走此路径；apply 不调
          LLM 故不限速），analyze 成功后仍 ``record()`` 计入配额。
        - 速率限制仅作用于 analyze（apply 不调 LLM，无成本，避免阻塞 analyze→apply 流水）。
        - scheduler 未就绪 / llm_fn 未就绪 → ``{ok: False, error: ...}``。
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
            filtered = {
                k: v for k, v in suggested.items() if k not in self.TUNE_DENYLIST
            }
            dropped = [k for k in suggested if k in self.TUNE_DENYLIST]
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
            filtered = {k: v for k, v in patch.items() if k not in self.TUNE_DENYLIST}
            dropped = [k for k in patch if k in self.TUNE_DENYLIST]
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
            # v0.2.9 F2：人设/数量变更触发后台兴趣重建（不阻塞 apply 响应）
            regenerate_needed = any(
                k in filtered
                for k in (
                    "persona_text",
                    "persona_knowledge",
                    "interest_example_count",
                    "interest_keyword_count",
                )
            )
            if regenerate_needed:
                try:
                    asyncio.create_task(self._bg_regenerate_persona())
                except Exception as e:
                    self._log("warning", f"启动兴趣重建后台任务失败: {e}")
            # v0.2.9 F2：应用 keywords_patch（add/remove + apply_rejected 重算质心）
            keywords_updated = 0
            if keywords_patch:
                keywords_updated = await self._apply_keywords_patch(keywords_patch)
            # 应用成功后清空缓存，避免重复 apply
            self._last_tune_suggestion = None
            return {
                "ok": True,
                "applied": True,
                "updated": len(filtered),
                "dropped": dropped,
                "regenerate": regenerate_needed,
                "keywords_updated": keywords_updated,
                "rate_limit": self._rate_limit_status(now, cooldown, max_per_day),
            }

        return {"ok": False, "error": f"未知 action: {action}"}

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

    def _rate_limit_status(self, now: float, cooldown: float, max_per_day: int) -> dict:
        """v0.2.9 F4：返回当前速率限制状态块（供响应附带，前端展示用）。

        从 ``TuneRateLimiter.state()`` 取 history 与 last_call 自行计算
        used / next_available，避免扩展 tune_controller 的公开方法。
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

    async def _apply_keywords_patch(self, keywords_patch: dict) -> int:
        """v0.2.9 F2：应用关键词增删 patch。

        结构：``{"add": [{kind, label, text}], "remove": [{kind, label, text}]}``
        - kind: ``example`` | ``high_keyword`` | ``hate_keyword``
        - label: ``core`` | ``general`` | ``marginal`` | ``hate``（example 用）
        - text: 关键词/示例文本

        循环调 ``interest_mgr.add_item`` / ``remove_item``（每调用即重算质心），
        完成后再调 ``apply_rejected`` 确保 rejected 列表生效。返回成功操作的项数。
        """
        if not isinstance(keywords_patch, dict):
            return 0
        embed_fn = self._embed_fn
        if embed_fn is None:
            return 0
        valid_kinds = ("example", "high_keyword", "hate_keyword")
        count = 0
        for item in keywords_patch.get("add") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            label = str(item.get("label", "") or "")
            text = str(item.get("text", "") or "")
            if kind not in valid_kinds or not text:
                continue
            try:
                ok, _ = await self.interest_mgr.add_item(kind, label, text, embed_fn)
                if ok:
                    count += 1
            except Exception as e:
                self._log("warning", f"keywords_patch add 失败: {e}")
        for item in keywords_patch.get("remove") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            label = str(item.get("label", "") or "")
            text = str(item.get("text", "") or "")
            if kind not in valid_kinds or not text:
                continue
            try:
                ok, _ = await self.interest_mgr.remove_item(kind, label, text, embed_fn)
                if ok:
                    count += 1
            except Exception as e:
                self._log("warning", f"keywords_patch remove 失败: {e}")
        # 重算质心确保 rejected 列表生效（add/remove 已各自重算，此步兜底过滤）
        try:
            await self.interest_mgr.apply_rejected(embed_fn)
        except Exception as e:
            self._log("warning", f"keywords_patch apply_rejected 失败: {e}")
        return count

    async def _autotune_trigger(self) -> dict:
        """v0.2.9 F3：scheduler 自动触发回调。

        调 ``llm_autotune("analyze", force=False)``（受速率限制）；
        若 ``autotune_auto_apply=true`` 则成功后调 ``llm_autotune("apply", force=True)``
        应用已缓存建议（force 跳过速率限制——analyze 已计数，避免冷却阻塞 apply）。
        失败/被限写日志，不抛异常（scheduler 后台 create_task 调用）。
        """
        try:
            result = await self.llm_autotune("analyze", force=False)
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

        注入：全量配置（~75 项减 DENYLIST 6 项）+ 兴趣数据（export_view）+
        人设文本 + schedule + 群白名单 + adaptive 状态 + provider 名称解析 +
        决策统计 + 风格偏好 + 用户指导。
        输出格式说明含三段：suggested_patch / suggested_keywords_patch / persona_revision。
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
        style_key = style if style in self._STYLE_GUIDANCE else "balanced"
        style_text = self._STYLE_GUIDANCE[style_key]
        user_guidance = guidance.strip() if guidance else "（用户未提供补充说明）"

        # DENYLIST 与可写键说明
        denylist_str = ", ".join(sorted(self.TUNE_DENYLIST))
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
            "7. **风格对齐**：建议方向必须与用户回复风格偏好一致。\n\n"
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
        return {k: v for k, v in cfg.items() if k not in self.TUNE_DENYLIST}

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
