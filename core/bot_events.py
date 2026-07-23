"""机器人发言事件处理（BotEventsMixin，对应 PRD F2/F4）。

将 scheduler.py 的 on_bot_sent 钩子逻辑拆出为独立 Mixin，经多继承注入
SocialScheduler。on_bot_sent 是 after_message_sent 钩子和 run_batch 主动发送的
统一入口，负责记录己方发言嵌入、状态转 EXPECTING_REPLY、建跟踪候选、疲劳消耗、
惯性更新、回复关键词提取与瞥眼调度。

设计要点：
- **不定义 __init__**：依赖 SocialScheduler 经 MRO 提供的实例属性与方法。
- **防重**：同 text 距上次 <2s 跳过 consume/inertia，避免主动发送后框架
  after_message_sent 再触发一次 on_bot_sent 时重复消耗疲劳与重复开惯性窗口。
- **不 import astrbot**：嵌入/发送/日志等能力经 SocialScheduler 注入回调获得。
"""

from __future__ import annotations

import asyncio

from .models import GroupState, TrackerEntry
from .reply_keyword import ReplyKeywordManager

# on_bot_sent 防重窗口：同 text 距上次 < 此秒数跳过 fatigue/inertia（v0.2）
_BOT_SENT_DEDUP_SEC = 2.0


class BotEventsMixin:
    """机器人发言事件处理 Mixin。

    依赖 SocialScheduler 经 MRO 提供的实例属性与方法：
    - ``self._get_group(group_id)``：惰性创建群运行时状态。
    - ``self._config_getter()``：实时读取配置 dict。
    - ``self._embed(texts)``：嵌入回调（包 embed_fn + 降级）。
    - ``self._fatigue``：全局疲劳管理器（FatigueManager）。
    - ``self._log(level, msg)``：日志回调。
    - ``self._last_bot_text`` / ``self._last_bot_text_ts``：防重缓存 dict。
    - ``self._rk_unavailable_warned``：jieba 不可用仅警告一次的标志。
    - ``self._replay_active``：回放期间标志。
    - ``self.glance_once(group_id)``：瞥眼协程（由 BatchPipelineMixin 提供）。
    """

    async def on_bot_sent(
        self,
        *,
        group_id: str,
        text: str,
        ts: float,
        reply_type: str = "passive",
        is_proactive: bool = False,
    ) -> None:
        """after_message_sent 钩子：记录己方发言嵌入、转 EXPECTING_REPLY、建跟踪、瞥眼。

        v0.2 增参 reply_type（active/passive/track/glance）与 is_proactive；
        内部消耗疲劳 self._fatigue.consume(reply_type) 并触发惯性 g["inertia"].on_reply()。
        防重：同 text 距上次 <2s 跳过 consume/inertia（run_batch 主动发送后框架
        after_message_sent 会再触发一次 on_bot_sent，避免重复消耗疲劳与重复开惯性窗口）。
        """
        try:
            g = self._get_group(group_id)
            cfg = self._config_getter()

            # v0.2 防重判定：同 text 且距上次 <2s 视为重复（proactive send_message 再触发）
            prev_text = self._last_bot_text.get(group_id, "")
            prev_ts = self._last_bot_text_ts.get(group_id, 0.0)
            is_duplicate = prev_text == text and (ts - prev_ts) < _BOT_SENT_DEDUP_SEC
            # 无论是否重复都更新最近文本/时间，供下次比较
            self._last_bot_text[group_id] = text
            self._last_bot_text_ts[group_id] = ts

            # 1. 记录己方发言嵌入
            embs = await self._embed([text])
            g["last_bot_emb"] = embs[0] if embs else None
            g["last_bot_ts"] = ts

            # 2. 记录到窗口
            g["context"].add_bot_message(text, ts)
            # 冷却窗口记 bot 消息（用于 cooldown_ratio）
            g["cooldown_window"].append((ts, True))

            # 3. 状态转 EXPECTING_REPLY（懒检查回 IDLE，不另起定时器）
            g["state"] = GroupState.EXPECTING_REPLY
            g["state_until"] = ts + float(cfg.get("expecting_duration", 30))

            # 4. 建跟踪候选：最近 2 个发言者
            if g["last_bot_emb"] is not None:
                try:
                    speakers = g["context"].recent_speakers(2)
                    for uid, nick, speaker_text in speakers:
                        g["tracker"].add(
                            TrackerEntry(
                                user_id=uid,
                                nickname=nick,
                                bot_last_emb=g["last_bot_emb"],
                                last_own_text=speaker_text,
                                created_ts=ts,
                            )
                        )
                except Exception as e:
                    self._log("warning", f"[ProSocial] on_bot_sent: 建跟踪失败: {e}")

            # 5. v0.2 疲劳消耗 + 惯性 on_reply（防重时跳过，避免重复消耗/重复开窗）
            if not is_duplicate:
                try:
                    self._fatigue.consume(reply_type, now=ts)
                except Exception as e:
                    self._log(
                        "warning", f"[ProSocial] on_bot_sent: fatigue.consume 失败: {e}"
                    )
                try:
                    g["inertia"].on_reply(now=ts, is_proactive=is_proactive)
                except Exception as e:
                    self._log(
                        "warning",
                        f"[ProSocial] on_bot_sent: inertia.on_reply 失败: {e}",
                    )

            # 7. v0.2.5 回复关键词提取（防重时跳过，避免重复提取；jieba 不可用仅警告一次）
            # on_bot_sent 是 after_message_sent 钩子和 run_batch 主动发送的统一入口，
            # 在此处提取保证被动 @ 回复和主动唤醒回复都能为下一轮提供关键词缓存。
            if not is_duplicate and bool(cfg.get("reply_keyword_enabled", True)):
                if not ReplyKeywordManager.available():
                    if not self._rk_unavailable_warned:
                        self._log(
                            "warning",
                            "[ProSocial] reply_keyword: jieba 未安装，"
                            "基于回复分词的连续对话匹配已禁用（pip install jieba 启用）",
                        )
                        self._rk_unavailable_warned = True
                else:
                    try:
                        # target_user_id: 取最近一位非 bot 发言者（与 tracker 建候选逻辑一致）
                        speakers = g["context"].recent_speakers(1)
                        target_uid = speakers[0][0] if speakers else ""
                        if target_uid:
                            g["reply_keyword_cache"] = ReplyKeywordManager.extract(
                                text=text,
                                target_user_id=target_uid,
                                now=ts,
                                cfg=cfg,
                            )
                    except Exception as e:
                        self._log(
                            "warning",
                            f"[ProSocial] on_bot_sent: reply_keyword 提取失败 group={group_id}: {e}",
                        )

            # 6. 安排瞥眼任务（glance 类型不再调度瞥眼，防级联；回放期间不瞥眼）
            if (
                bool(cfg.get("glance_enable", True))
                and not self._replay_active
                and reply_type != "glance"
            ):
                try:
                    asyncio.create_task(self.glance_once(group_id))
                except Exception as e:
                    self._log("warning", f"[ProSocial] on_bot_sent: 安排瞥眼失败: {e}")
        except Exception as e:
            self._log("error", f"[ProSocial] on_bot_sent 异常 group={group_id}: {e}")
