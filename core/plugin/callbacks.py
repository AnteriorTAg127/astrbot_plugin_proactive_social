"""模块 B：CallbacksMixin — 注入回调构造与统一日志。

职责：
1. ``_log``：统一日志回调，包装 AstrBot ``logger``，加 ``[ProSocial]`` 前缀。
2. ``_make_llm_fn`` / ``_make_embed_fn`` / ``_make_send_fn`` / ``_make_inject_fn``：
   构造 scheduler 所需的 4 个注入回调，封装 AstrBot 的 LLM / 嵌入 / 发送 / 管线注入能力。

设计要点：
- ``CallbacksMixin`` 不定义 ``__init__``，避免干扰 ``ProSocialPlugin`` 主类初始化。
- astrbot 相关 import 由 main.py 顶层处理，mixin 内部用延迟 import，避免循环依赖。
- 依赖的实例属性（由 ``ProSocialPlugin`` 提供）：``self.config`` / ``self.context`` /
  ``self._config_getter`` / ``self._platform_self_ids`` / ``self._pending_hints`` /
  ``self._user_nicknames``。
"""

from __future__ import annotations

import time
import uuid

# 不支持主动发送的平台（PRD §6.2）—— send_fn 检测到这些平台时跳过（与 main.py 同名常量一致）
_NO_PROACTIVE_PLATFORMS = {"qq_official", "qq_official_webhook"}


class CallbacksMixin:
    """注入回调构造 mixin（模块 B）。

    提供 ``_log`` 统一日志回调 + 4 个回调工厂方法（llm_fn / embed_fn / send_fn /
    inject_fn），由 ``ProSocialPlugin`` 多继承混入。``__init__`` 不定义，避免干扰
    主类初始化。

    依赖的实例属性（由 ``ProSocialPlugin`` 提供）：
    - ``self.config`` / ``self.context``：AstrBot 配置与上下文。
    - ``self._config_getter``：合并 ConfigStore 缓存与 AstrBotConfig 的方法。
    - ``self._platform_self_ids`` / ``self._pending_hints`` / ``self._user_nicknames``：
      平台 self_id / hint 暂存 / 昵称缓存（on_group_message 中收集）。
    """

    def _log(self, level: str, msg: str) -> None:
        """统一日志回调：(level, msg) -> None，level ∈ info/warning/error/debug。"""
        from astrbot.api import logger

        fn = getattr(
            logger,
            level if level in ("info", "warning", "error", "debug") else "info",
            logger.info,
        )
        fn(f"[ProSocial] {msg}")

    # ------------------------------------------------------------------ #
    # 注入回调构造
    # ------------------------------------------------------------------ #
    def _make_llm_fn(self):
        """构造 llm_fn(prompt) -> str：解析 chat provider 并调 llm_generate。"""

        async def llm_fn(prompt: str) -> str:
            # 警告：此路径直连 context.llm_generate()，绕过 AstrBot pipeline，
            # 不会被 STR/dashboard 统计追踪（provider_stats 无记录、对话历史不自动保存）。
            # 主动回复应优先走 inject_fn 管线注入；此处仅降级/后台分析（autotune/interest）使用。
            self._log(
                "warning",
                f"llm_fn 直连调用（不被 STR 追踪），prompt 长度={len(prompt or '')}",
            )
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
                from astrbot.api.event import MessageChain

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

        async def inject_fn(
            umo: str, text: str, hint: str, group_id: str, sender_id: str = ""
        ) -> bool:
            try:
                # 1. 解析 umo（platform_id:message_type:session_id）
                parts = umo.split(":", 2)
                if len(parts) != 3:
                    self._log(
                        "warning",
                        f"inject_fn 降级：umo 格式非法（期望 a:b:c）umo={umo!r}",
                    )
                    return False
                platform_id, mtype, session_id = parts
                if mtype != "GroupMessage":
                    self._log(
                        "warning",
                        f"inject_fn 降级：umo 非群消息 mtype={mtype!r} umo={umo!r}",
                    )
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
                except Exception as e:
                    self._log(
                        "warning",
                        f"inject_fn 降级：遍历平台实例异常 platform_id={platform_id!r}: {e}",
                    )
                    return False
                if platform_inst is None:
                    self._log(
                        "warning",
                        f"inject_fn 降级：未找到平台实例 platform_id={platform_id!r}"
                        f"（已加载平台数={len(self.context.platform_manager.get_insts())}）",
                    )
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
