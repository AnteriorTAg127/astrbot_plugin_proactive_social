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
- ``ConfigStore``（v0.2.1）：普通参数由 ConfigStore 管理（默认值 + KV 持久化覆盖 +
  内存缓存），``_config_getter`` 合并 ConfigStore 缓存与 ``AstrBotConfig`` 特殊选择器
  （``chat_provider_id``），scheduler 每次决策实时读取（热更新：set_many 改缓存后立即生效）。
- ``on_astrbot_loaded`` 钩子：从 KV 加载配置覆盖项到 ConfigStore 缓存。
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
from .core.web import build_handlers

# 插件名（与 metadata.yaml 一致，用于 Web API 路由前缀与数据目录）
_PLUGIN_NAME = "astrbot_plugin_proactive_social"

# 不支持主动发送的平台（PRD §6.2）—— send_fn 检测到这些平台时跳过
_NO_PROACTIVE_PLATFORMS = {"qq_official", "qq_official_webhook"}


@register(_PLUGIN_NAME, "", "主动社交：向量决策驱动的多群主动插话插件", "v0.2.6")
class ProSocialPlugin(Star):
    """主动社交插件入口（模块 G）。

    继承 ``Star``（含 ``PluginKVStoreMixin``，提供 ``get_kv_data`` / ``put_kv_data``）。
    唯一 import astrbot 的运行时文件，把 ``core/`` 模块与 AstrBot 框架对接。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # ConfigStore：普通参数的默认值 + KV 持久化覆盖 + 内存缓存（v0.2.1，PRD F15.1）。
        # __init__ 时用 DEFAULT_CONFIG 填充缓存，保证同步可读；KV 覆盖在 on_astrbot_loaded 加载。
        self._config_store = ConfigStore()
        # 特殊选择器键（chat_provider_id 等）仍由 AstrBotConfig 原生承载，不走 ConfigStore
        self._SPECIAL_KEYS = SPECIAL_KEYS
        # 数据目录：data/plugin_data/astrbot_plugin_proactive_social/
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
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
            # F1: 在构造 scheduler 之前先加载 KV 配置，消除重载竞态
            try:
                await self._config_store.load(self.get_kv_data)
                self._log("info", "KV 配置已加载（initialize 阶段）")
            except Exception as e:
                self._log("warning", f"加载 KV 配置失败，使用默认值: {e}")

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

        F1: 配置 KV 加载已移至 initialize() 中（在构造 scheduler 之前），
        此处仅保留 interest_rejected 加载。
        """
        # 加载兴趣 rejected 列表（F20）
        try:
            raw = await self.get_kv_data("interest_rejected")
            if raw:
                self.interest_mgr.set_rejected(json.loads(raw))
                self._log("info", "兴趣 rejected 列表已加载")
        except Exception as e:
            self._log("warning", f"加载兴趣 rejected 列表失败: {e}")

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
        """F8: 长窗口注入已迁移至 scheduler.run_batch，此钩子保留但不再注入。"""
        return

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
                    cfg = self._config_getter()
                    persona_text = str(cfg.get("persona_text", ""))
                    persona_knowledge = str(cfg.get("persona_knowledge", ""))
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
        ok, msg = await self._config_store.set_many(patch, self.put_kv_data)
        if not ok:
            return False, msg
        # F4: 人设变更触发兴趣重新生成
        if any(k in patch for k in ("persona_text", "persona_knowledge")):
            try:
                new_cfg = self._config_getter()
                persona_text = str(new_cfg.get("persona_text", ""))
                persona_knowledge = str(new_cfg.get("persona_knowledge", ""))
                example_count = int(new_cfg.get("interest_example_count", 3))
                keyword_count = int(new_cfg.get("interest_keyword_count", 12))
                await self.interest_mgr.regenerate(
                    persona_text,
                    persona_knowledge,
                    self._llm_fn,
                    self._embed_fn,
                    example_count=example_count,
                    keyword_count=keyword_count,
                )
                self._log("info", "人设变更，兴趣数据已重新生成")
            except Exception as e:
                self._log("warning", f"人设变更后兴趣重建失败: {e}")
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
        # 事务性写入 ConfigStore（校验 + 缓存 + KV）
        if updates:
            ok, msg = await self._config_store.set_many(updates, self.put_kv_data)
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
                await self.put_kv_data(
                    "interest_rejected",
                    json.dumps(self.interest_mgr.get_rejected(), ensure_ascii=False),
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
            "version": "v0.2.6",
            "export_time": time.time(),
        }

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
