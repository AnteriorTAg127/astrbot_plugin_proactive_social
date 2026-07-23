"""多群调度器 / 状态机 / 瞥一眼 / 回放编排（模块 E 产出，对应 PRD F2/F4/F5/F6/F8）。

SocialScheduler 是插件主编排器，整合 InterestManager / GroupContext / GroupBuffer /
PersonalTracker / WakeEngine / TokenBucketRateLimiter / DecisionLog / MetricsStore /
ReplayEngine，驱动消息批次决策、状态机转移、多群轮询、瞥一眼与历史回放。

设计要点：
- **不 import astrbot**：LLM/嵌入/发送/KV/日志/配置能力全部经注入回调获得，保证可离线测试。
- **实时配置**：阈值/权重/间隔/作息/冷却/瞥眼/dry_run/群白名单每次决策实时读 config_getter()，
  不缓存（窗口大小 buffer_max_size 等结构参数创建时固定可接受）。
- **AND 语义**：group_enabled = (mode=all 或 群在白名单) 且 KV 未显式停用。
- **状态机懒检查**：EXPECTING_REPLY/COOLDOWN 到期不另起定时器，在 on_message/run_batch
  开头调用 _check_state_expiry 检查 state_until 过期则回 IDLE，避免额外任务。
- **降级**：嵌入连续失败 3 次设 degraded，5 分钟后重试恢复；降级时 run_batch 走 rule_fallback。
- **瞥眼最多一群**：glance_once 命中并插话后立即 break。
- **回放强制不发送**：replay 期间 _replay_active=True，run_batch 视同 DRY_RUN（suppressed_reason="dry_run"）。
- **后台任务 try/except 包裹**：单点失败 log 不抛，循环不退出。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .buffer import GroupBuffer
from .context import GroupContext
from .engine import WakeEngine
from .fatigue import FatigueManager
from .fusion import FusionEngine
from .inertia import InertiaManager, WaitWindow
from .interest import InterestManager
from .metrics import DecisionLog, MetricsStore
from .models import (
    BatchDecision,
    BatchRecord,
    GroupState,
    LogicalMessage,
    ScoreFactors,
    TrackerEntry,
)
from .prompts import build_glance_reply_prompt, build_reply_prompt
from .ratelimit import TokenBucketRateLimiter
from .replay import ReplayEngine
from .reply_keyword import ReplyKeywordManager
from .rule_engine import RuleEngine
from .tracker import PersonalTracker

# 嵌入降级阈值：连续失败次数
_EMBED_FAIL_THRESHOLD = 3
# 嵌入降级恢复重试间隔（秒）
_EMBED_DEGRADED_RECOVER_SEC = 300.0
# msg_timestamps deque 上限（约 60 秒高频或 100 条）
_MSG_TS_MAX = 100
# cooldown_window deque 上限（防内存增长）
_COOLDOWN_WIN_MAX = 200
# on_bot_sent 防重窗口：同 text 距上次 < 此秒数跳过 fatigue/inertia（v0.2）
_BOT_SENT_DEDUP_SEC = 2.0


class SocialScheduler:
    """主编排器：多群调度 + 决策管线 + 状态机 + 瞥眼 + 回放。"""

    def __init__(
        self,
        *,
        config_getter: Callable[[], dict],
        interest_mgr: InterestManager,
        send_fn: Callable[[str, str], Awaitable[bool]],
        llm_fn: Callable[[str], Awaitable[str]],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
        rate_limiter: TokenBucketRateLimiter,
        kv_get_fn: Callable[[str, Any], Awaitable[Any]],
        kv_set_fn: Callable[[str, Any], Awaitable[None]],
        log_fn: Callable[[str, str], None],
        data_dir: Path,
    ):
        # 注入回调
        self._config_getter = config_getter
        self._interest_mgr = interest_mgr
        self._send_fn = send_fn
        self._llm_fn = llm_fn
        self._embed_fn = embed_fn
        self._rate_limiter = rate_limiter
        self._kv_get = kv_get_fn
        self._kv_set = kv_set_fn
        self._log = log_fn
        self._data_dir = data_dir

        # 每群运行时状态：{group_id -> dict}
        self._groups: dict[str, dict] = {}
        # group_id -> umo 缓存（on_message 收集，主动发送用）
        self._umo_map: dict[str, str] = {}

        # 主循环任务
        self._main_task: asyncio.Task | None = None

        # 决策日志 + 指标 + 回放引擎
        self._decision_log = DecisionLog()
        self._metrics = MetricsStore()
        self._replay_engine = ReplayEngine(data_dir, log_fn)

        # 回放控制：_replay_stop 由 stop_replay() 置 True 中断 run；_replay_active 标记回放期间
        self._replay_stop: bool = False
        self._replay_active: bool = False

        # 运行时 dry_run 覆盖（None=用配置；True/False=指令覆盖）
        self._dry_run_override: bool | None = None

        # 群启用 KV 缓存：start() 异步预加载，set_group_enabled 时同步更新
        # 设计说明：group_enabled 是同步方法但 KV 是 async，此矛盾通过预加载缓存解决——
        # 启动时一次性读 KV "group_enable" 到内存，group_enabled 同步读缓存，
        # set_group_enabled 异步写 KV 并同步更新缓存。
        self._group_enable_cache: dict[str, bool] | None = None

        # 嵌入降级状态
        self._embed_fail_count: int = 0
        self._embed_degraded: bool = False
        self._embed_degraded_until: float = 0.0

        # v0.2 全局疲劳管理器（bot 级单例，所有群共享）
        self._fatigue = FatigueManager(config_getter)
        # v0.2 on_bot_sent 防重：group_id -> 上次 bot 文本；同 text <2s 跳过 consume/inertia
        self._last_bot_text: dict[str, str] = {}
        self._last_bot_text_ts: dict[str, float] = {}
        # v0.2.5 jieba 不可用时仅警告一次（避免每次 on_bot_sent 都刷屏）
        self._rk_unavailable_warned: bool = False

    # ------------------------------------------------------------------ #
    # 群状态懒创建
    # ------------------------------------------------------------------ #

    def _get_group(self, group_id: str) -> dict:
        """惰性创建群运行时状态。配置的窗口/buffer 大小创建时固定（不实时跟随配置变化）。"""
        g = self._groups.get(group_id)
        if g is not None:
            return g
        cfg = self._config_getter()
        short_size = int(cfg.get("short_window_size", 8))
        long_size = int(cfg.get("long_window_size", 20))
        buffer_max = int(cfg.get("buffer_max_size", 200))
        g = {
            "umo": self._umo_map.get(group_id, ""),
            "context": GroupContext(short_size=short_size, long_size=long_size),
            "buffer": GroupBuffer(max_size=buffer_max, log_fn=self._log),
            "tracker": PersonalTracker(),
            "state": GroupState.IDLE,
            "state_until": 0.0,
            "last_bot_emb": None,
            "last_bot_ts": 0.0,
            # msg_timestamps：仅用户消息，用于 hot_group / 速率计算
            "msg_timestamps": deque(maxlen=_MSG_TS_MAX),
            # cooldown_window：(ts, is_bot) 元组，用于 _cooldown_ratio
            "cooldown_window": deque(maxlen=_COOLDOWN_WIN_MAX),
            "last_active_ts": 0.0,
            "batch_task": None,
            # v0.2 惯性管理器（每群一个，回复后阈值倍率 + 主动话题生命周期）
            "inertia": InertiaManager(self._config_getter),
            # v0.2 等待窗口（回复后收集同触发用户连续消息；None=未开窗）
            "wait_window": None,
            # v0.2.5 回复关键词缓存（按目标用户+TTL，bot 回复后提取，run_batch 中匹配加分）
            "reply_keyword_cache": None,
        }
        self._groups[group_id] = g
        return g

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """启动主循环；预加载 metrics / decision_log / group_enable 缓存。"""
        try:
            # 预加载群启用缓存（解决同步 group_enabled 与异步 KV 的矛盾）
            self._group_enable_cache = await self._kv_get("group_enable", {})
            if not isinstance(self._group_enable_cache, dict):
                self._group_enable_cache = {}
        except Exception as e:
            self._log(
                "warning", f"[ProSocial] scheduler.start: 预加载 group_enable 失败: {e}"
            )
            self._group_enable_cache = {}

        try:
            await self._metrics.load(self._kv_get)
        except Exception as e:
            self._log("warning", f"[ProSocial] scheduler.start: 加载 metrics 失败: {e}")

        try:
            data = await self._kv_get("decision_log", [])
            if isinstance(data, list):
                self._decision_log.load(data)
        except Exception as e:
            self._log(
                "warning", f"[ProSocial] scheduler.start: 加载 decision_log 失败: {e}"
            )

        # v0.2 预加载全局疲劳 KV（容错：缺失/非法则从 0 起算）
        try:
            fv = await self._kv_get("fatigue", None)
            if isinstance(fv, dict):
                self._fatigue.restore(
                    float(fv.get("value", 0.0)), float(fv.get("last_ts", 0.0))
                )
        except Exception as e:
            self._log("warning", f"[ProSocial] scheduler.start: 加载 fatigue 失败: {e}")

        self._main_task = asyncio.create_task(self._main_loop())
        self._log("info", "[ProSocial] scheduler 已启动")

    async def _main_loop(self) -> None:
        """主循环：确保兴趣 -> 活跃时段检查 -> 轮询选群 -> 监听 -> 冷却 -> 抖动睡眠。

        全程 try/except，单轮异常 log 后 continue；CancelledError 向上传播结束循环。
        """
        while True:
            try:
                cfg = self._config_getter()

                # 同步限流器速率（配置实时变更时跟随）
                try:
                    self._rate_limiter.set_rate(
                        int(cfg.get("embedding_rate_limit_per_min", 30))
                    )
                except Exception:
                    pass

                # 1. 确保兴趣已加载（用 config 的 persona）
                try:
                    persona_text = str(cfg.get("persona_text", ""))
                    persona_knowledge = str(cfg.get("persona_knowledge", ""))
                    example_count = int(cfg.get("interest_example_count", 3))
                    keyword_count = int(cfg.get("interest_keyword_count", 12))
                    await self._interest_mgr.ensure_loaded(
                        persona_text,
                        persona_knowledge,
                        self._llm_fn,
                        self._embed_fn,
                        example_count=example_count,
                        keyword_count=keyword_count,
                    )
                except Exception as e:
                    self._log(
                        "warning", f"[ProSocial] main_loop: 兴趣加载失败，继续: {e}"
                    )

                # 2. 活跃时段检查
                if not self.in_active_hours():
                    await asyncio.sleep(60)
                    continue

                # 3. 轮询选群
                now = time.time()
                poll_interval = int(cfg.get("poll_interval", 300))
                poll_jitter = int(cfg.get("poll_jitter", 120))
                monitoring_duration = int(cfg.get("monitoring_duration", 120))
                group_cooldown = int(cfg.get("group_cooldown", 180))
                silent_minutes = int(cfg.get("silent_group_minutes", 10))

                candidate = self._pick_poll_candidate(now, silent_minutes)
                if candidate is None:
                    # 无符合群 -> sleep poll_interval（带抖动）后继续
                    await asyncio.sleep(
                        max(
                            1, poll_interval + random.randint(-poll_jitter, poll_jitter)
                        )
                    )
                    continue

                # 4. 选中群设 ACTIVE_MONITORING
                g = self._groups[candidate]
                g["state"] = GroupState.ACTIVE_MONITORING
                g["state_until"] = now + monitoring_duration
                self._log("info", f"[ProSocial] main_loop: 进入监听 group={candidate}")

                # 5. 等待 monitoring_duration（期间 on_message/run_batch 并行工作；
                #    简化实现：不提前退出，到期后转冷却。注释说明：触发回复后不提前退出）
                try:
                    await asyncio.sleep(monitoring_duration)
                except asyncio.CancelledError:
                    raise

                # 6. 退出后该群 COOLDOWN
                now = time.time()
                g["state"] = GroupState.COOLDOWN
                g["state_until"] = now + group_cooldown

                # 7. 轮询间隔带抖动
                await asyncio.sleep(
                    max(1, poll_interval + random.randint(-poll_jitter, poll_jitter))
                )

            except asyncio.CancelledError:
                self._log("info", "[ProSocial] main_loop: 收到取消信号，退出")
                raise
            except Exception as e:
                self._log("error", f"[ProSocial] main_loop: 单轮异常，继续: {e}")
                await asyncio.sleep(5)

    def _pick_poll_candidate(self, now: float, silent_minutes: int) -> str | None:
        """从 _groups 中选一个符合条件的群进入主动监听。

        条件：group_enabled、state 不为 ACTIVE_MONITORING/GLANCING、不在冷却期
        （now >= state_until）、有近期活跃度（last_active_ts 在 silent_minutes 内）。
        选取最近活跃度最高的群。
        """
        best: str | None = None
        best_ts: float = 0.0
        silent_sec = silent_minutes * 60
        for gid, g in self._groups.items():
            if not self.group_enabled(gid):
                continue
            if g["state"] in (GroupState.ACTIVE_MONITORING, GroupState.GLANCING):
                continue
            # state_until > 0 表示在 COOLDOWN/EXPECTING_REPLY 等带时长状态中
            if g["state_until"] > 0 and now < g["state_until"]:
                continue
            last_ts = g["last_active_ts"]
            if last_ts <= 0:
                continue
            if now - last_ts > silent_sec:
                continue  # 沉默群
            if last_ts > best_ts:
                best_ts = last_ts
                best = gid
        return best

    async def stop(self) -> None:
        """cancel 所有任务并 await；持久化 decision_log 与 metrics。"""
        self._replay_stop = True

        # 取消主循环
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._log("warning", f"[ProSocial] stop: main_task 结束异常: {e}")
        self._main_task = None

        # 取消所有群批次任务
        for gid, g in list(self._groups.items()):
            t = g.get("batch_task")
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self._log(
                        "warning",
                        f"[ProSocial] stop: group={gid} batch_task 结束异常: {e}",
                    )
            g["batch_task"] = None

        # 持久化
        try:
            await self._kv_set("decision_log", self._decision_log.to_list())
        except Exception as e:
            self._log("warning", f"[ProSocial] stop: 持久化 decision_log 失败: {e}")
        try:
            await self._kv_set("metrics", self._metrics.snapshot())
        except Exception as e:
            self._log("warning", f"[ProSocial] stop: 持久化 metrics 失败: {e}")
        # v0.2 持久化全局疲劳（value + last_ts，重启不丢失；非必须，丢失从 0 起算）
        try:
            fv, fts = self._fatigue.state()
            await self._kv_set("fatigue", {"value": float(fv), "last_ts": float(fts)})
        except Exception as e:
            self._log("warning", f"[ProSocial] stop: 持久化 fatigue 失败: {e}")

        self._log("info", "[ProSocial] scheduler 已停止")

    # ------------------------------------------------------------------ #
    # 消息入口（handler 快速路径）
    # ------------------------------------------------------------------ #

    async def on_message(
        self,
        *,
        group_id: str,
        umo: str,
        user_id: str,
        nickname: str,
        text: str,
        ts: float,
        is_wake: bool,
    ) -> None:
        """handler 快速路径：记录窗口/活跃度 -> 入缓冲 -> 调度批次定时器。

        不做任何嵌入/LLM 调用（那些在批次任务里）。
        """
        try:
            # 1. 缓存 umo
            self._umo_map[group_id] = umo

            # 2. 惰性创建群状态
            g = self._get_group(group_id)
            g["umo"] = umo

            # 3. 更新活跃度（仅用户消息计入 msg_timestamps）
            g["msg_timestamps"].append(ts)
            g["last_active_ts"] = ts
            # 冷却窗口（用户消息 is_bot=False）
            g["cooldown_window"].append((ts, False))

            # 4. 记录到上下文窗口
            g["context"].add_message(
                LogicalMessage(
                    user_id=user_id,
                    nickname=nickname,
                    text=text,
                    ts=ts,
                    group_id=group_id,
                )
            )

            # 4.5 v0.2 惯性：检测主动话题是否有人回应（必须在 is_wake 返回之前，
            #     因为 @ 也算回应）
            try:
                g["inertia"].on_user_message(now=ts)
            except Exception:
                pass

            # 5. 唤醒消息(@机器人) -> 框架处理被动回复，插件不重复（PRD §6.4）
            if is_wake:
                return

            # 6. 群未启用 -> 仅记录窗口，不决策
            if not self.group_enabled(group_id):
                return

            # 7. 总开关关 -> return（注：回放期间不在此拦截，回放经 on_message 喂入，
            #    replay_active 仅在 run_batch 内部影响是否发送）
            cfg = self._config_getter()
            if not bool(cfg.get("enable", True)):
                return

            # 7.5 v0.2 等待窗口路由：回复后收集同触发用户连续消息合并回复
            ww = g.get("wait_window")
            if ww is not None and ww.active:
                try:
                    ww.add(
                        now_ms=ts * 1000.0,
                        user_id=user_id,
                        text=text,
                        is_at=is_wake,
                    )
                    if ww.should_close(now_ms=ts * 1000.0):
                        merged_text = ww.merged_text()
                        trigger_user = ww.trigger_user
                        ww.close()
                        g["wait_window"] = None
                        if merged_text:
                            asyncio.create_task(
                                self._send_wait_window_reply(
                                    group_id, merged_text, trigger_user
                                )
                            )
                except Exception as e:
                    self._log(
                        "warning",
                        f"[ProSocial] on_message: wait_window 异常 group={group_id}: {e}",
                    )
                # 触发用户的消息路由进窗口后不再入普通缓冲
                if ww is not None and ww.trigger_user == user_id:
                    return

            # 8. 入缓冲区（v0.2 传入 is_wake 供批次级 mentions_bot 判定）
            g["buffer"].append(user_id, nickname, text, ts, group_id, is_wake=is_wake)

            # 9. 调度批次任务（若无活跃任务则创建）
            t = g.get("batch_task")
            if t is None or t.done():
                g["batch_task"] = asyncio.create_task(self._schedule_batch(group_id))
        except Exception as e:
            self._log("error", f"[ProSocial] on_message 异常 group={group_id}: {e}")

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
                from .models import RuleSignal

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
                    from .models import FusionResult

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
                from .models import FusionResult

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

            # 11. 判定唤醒（v0.2 融合判定）
            if suppressed_reason:
                triggered = False
            elif personal_triggered:
                # 个人快通道：bypass score 阈值
                triggered = True
            elif batch_emb is None:
                # 降级路径：保持 v0.1 rule_fallback 语义（不引入融合）
                triggered = bool(rule_hit)
            else:
                # v0.2 融合判定：final_score >= threshold 且未被疲劳抑制
                is_forced = (
                    hit_level == "core"
                    or rule_signal.mentions_bot
                    or rule_signal.hit_type == "direct"
                )
                if self._fatigue.should_suppress(is_forced, now):
                    suppressed_reason = "fatigue"
                    triggered = False
                else:
                    triggered = fusion.final_score >= fusion.threshold

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
            )
            self._decision_log.add(d)
            try:
                await self._kv_set("decision_log", self._decision_log.to_list())
            except Exception as e:
                self._log(
                    "warning", f"[ProSocial] run_batch: 持久化 decision_log 失败: {e}"
                )

            # 14. 触发 -> 生成并发送
            if triggered:
                # F8: 主动回复时注入长窗口上下文
                extra_ctx = ""
                if bool(cfg.get("long_window_inject_proactive", True)):
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
                                        from .prompts import build_summary_prompt

                                        summary = await self._llm(
                                            build_summary_prompt(
                                                long_window_text, short_text
                                            )
                                        )
                                        extra_ctx = (
                                            f"相关历史摘要：\n{summary}"
                                            if summary
                                            else ""
                                        )
                                    else:
                                        extra_ctx = (
                                            f"相关历史背景：\n{long_window_text}"
                                        )
                    except Exception as e:
                        self._log(
                            "warning",
                            f"[ProSocial] run_batch: 长窗口注入失败 group={group_id}: {e}",
                        )

                # LLM 生成
                persona_text = str(cfg.get("persona_text", ""))
                short_window_text = g["context"].short_window_text()
                prompt = build_reply_prompt(
                    persona_text=persona_text,
                    short_window=short_window_text,
                    extra_context=extra_ctx,
                    batch_text=batch_text,
                )
                await self._metrics.incr("llm_calls", self._kv_set)
                reply = await self._llm(prompt)
                if not reply:
                    self._log(
                        "warning", f"[ProSocial] run_batch: LLM 无回复 group={group_id}"
                    )
                    return

                # 发送
                umo = g.get("umo") or self._umo_map.get(group_id, "")
                ok = await self._send(umo, reply)
                await self._metrics.incr("proactive_sends", self._kv_set)
                if ok:
                    await self._metrics.incr("proactive_triggered", self._kv_set)
                    # v0.2: reply_type 区分 active（批处理触发）/ track（个人跟踪），
                    # is_proactive=True（scheduler 主动决策发起，非被动接话）
                    reply_type = "track" if personal_triggered else "active"
                    # v0.2.5 集成点 3：因关键词触发（集成点 2）的回复清除关键词缓存，防重复
                    # 注：on_bot_sent 会基于新回复重建缓存；此处清除是防止 on_bot_sent 失败时旧缓存残留
                    if keyword_triggered:
                        g["reply_keyword_cache"] = None
                    await self.on_bot_sent(
                        group_id=group_id,
                        text=reply,
                        ts=time.time(),
                        reply_type=reply_type,
                        is_proactive=True,
                    )
                    # v0.2 等待窗口：发送成功后开窗收集同触发用户后续消息
                    # 降级方案：不起 100ms 轮询 task，依赖 on_message 触发 should_close 检查
                    try:
                        ww_duration_ms = int(cfg.get("wait_window_duration_ms", 3000))
                        if ww_duration_ms > 0:
                            ww = WaitWindow(
                                duration_ms=ww_duration_ms,
                                max_extra=int(cfg.get("wait_window_max_extra", 3)),
                            )
                            # 触发用户 ID：取本批首个消息的 user_id
                            trigger_uid = msgs[0].user_id if msgs else ""
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
                    self._log(
                        "warning", f"[ProSocial] run_batch: 发送失败 group={group_id}"
                    )
            else:
                # 15. 未触发 -> debug
                self._log(
                    "debug",
                    f"[ProSocial] run_batch: 未触发 group={group_id} "
                    f"final={fusion.final_score:.3f} thr={fusion.threshold:.3f} "
                    f"hit={hit_level} reason={suppressed_reason or 'below_threshold'}",
                )
        except Exception as e:
            self._log("error", f"[ProSocial] run_batch 异常 group={group_id}: {e}")

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

    async def _send_wait_window_reply(
        self, group_id: str, merged_text: str, trigger_user: str
    ) -> None:
        """等待窗口关闭后：把合并文本交 LLM 生成连贯回复并发送（v0.2）。

        被动接话语义（is_proactive=False，reply_type="passive"），发送成功后走
        on_bot_sent 完成疲劳消耗/惯性/跟踪/瞥眼。
        """
        try:
            g = self._get_group(group_id)
            cfg = self._config_getter()
            persona_text = str(cfg.get("persona_text", ""))
            short_window_text = g["context"].short_window_text()
            prompt = build_reply_prompt(
                persona_text=persona_text,
                short_window=short_window_text,
                extra_context="",
                batch_text=merged_text,
            )
            await self._metrics.incr("llm_calls", self._kv_set)
            reply = await self._llm(prompt)
            if not reply:
                self._log(
                    "warning",
                    f"[ProSocial] _send_wait_window_reply: LLM 无回复 group={group_id}",
                )
                return
            umo = g.get("umo") or self._umo_map.get(group_id, "")
            ok = await self._send(umo, reply)
            await self._metrics.incr("proactive_sends", self._kv_set)
            if ok:
                await self._metrics.incr("proactive_triggered", self._kv_set)
                await self.on_bot_sent(
                    group_id=group_id,
                    text=reply,
                    ts=time.time(),
                    reply_type="passive",
                    is_proactive=False,
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
                _, _, last_text = speakers[0]
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
                # 生成简短回复并发送
                umo = g.get("umo") or self._umo_map.get(gid, "")
                prompt = build_glance_reply_prompt(persona_text, last_text)
                await self._metrics.incr("llm_calls", self._kv_set)
                reply = await self._llm(prompt)
                if not reply:
                    continue
                ok = await self._send(umo, reply)
                await self._metrics.incr("proactive_sends", self._kv_set)
                if ok:
                    await self._metrics.incr("proactive_triggered", self._kv_set)
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

    # ------------------------------------------------------------------ #
    # 群启用判定（AND 语义）
    # ------------------------------------------------------------------ #

    def group_enabled(self, group_id: str) -> bool:
        """AND 语义：(mode=all 或 群在白名单) 且 KV 未显式停用。实时读取配置。

        KV 部分读预加载缓存（start() 时加载）；缓存未就绪时只判定 mode_ok。
        """
        cfg = self._config_getter()
        mode = str(cfg.get("group_mode", "whitelist"))
        whitelist = cfg.get("group_whitelist", []) or []
        if mode == "all":
            mode_ok = True
        else:
            mode_ok = group_id in whitelist

        # KV 缓存：未就绪时视为启用（start 后会补齐）
        if self._group_enable_cache is None:
            return mode_ok
        kv_enabled = self._group_enable_cache.get(group_id, True)
        return mode_ok and bool(kv_enabled)

    async def set_group_enabled(self, group_id: str, enabled: bool) -> None:
        """更新群快捷开关：写缓存 + 写 KV。"""
        if self._group_enable_cache is None:
            self._group_enable_cache = {}
        self._group_enable_cache[group_id] = bool(enabled)
        try:
            await self._kv_set("group_enable", self._group_enable_cache)
        except Exception as e:
            self._log("warning", f"[ProSocial] set_group_enabled: 写 KV 失败: {e}")

    # ------------------------------------------------------------------ #
    # 状态面板
    # ------------------------------------------------------------------ #

    def get_status(self) -> dict:
        """返回状态面板数据。"""
        now = time.time()
        groups_list = []
        current_monitoring: list[str] = []
        for gid, g in self._groups.items():
            state = g["state"]
            if state == GroupState.ACTIVE_MONITORING:
                current_monitoring.append(gid)
            # 最近 60 秒消息数
            msg_per_min = sum(1 for t in g["msg_timestamps"] if now - t <= 60.0)
            groups_list.append(
                {
                    "id": gid,
                    "state": state.value,
                    "state_until": g["state_until"],
                    "tracker_count": len(g["tracker"].all()),
                    "msg_per_min": msg_per_min,
                    "enabled": self.group_enabled(gid),
                    # v0.2 每群惯性快照（after_reply/proactive/awaiting/计数）
                    "inertia": g["inertia"].snapshot(now),
                }
            )

        # dry_run 当前状态
        cfg = self._config_getter()
        if self._replay_active:
            dry_run = True
        elif self._dry_run_override is not None:
            dry_run = self._dry_run_override
        else:
            dry_run = bool(cfg.get("dry_run", False))

        return {
            "running": self._main_task is not None and not self._main_task.done(),
            "in_active_hours": self.in_active_hours(),
            "current_monitoring": current_monitoring,
            "groups": groups_list,
            "metrics": self._metrics.snapshot(),
            "interest_loaded": self._interest_mgr.get() is not None,
            "replay_active": self._replay_active and not self._replay_stop,
            "dry_run": dry_run,
            "decision_count": len(self._decision_log),
            # v0.2 全局疲劳快照（value/limit/ratio/level，供 Dashboard 仪表盘）
            "fatigue": self._fatigue.snapshot(now),
        }

    def in_active_hours(self, now: float | None = None) -> bool:
        """判断当前是否在配置的活跃时段内。

        简化实现：in_active_hours 判断时用原始时段，不加 schedule_jitter
        （jitter 仅影响 _main_loop 的轮询时机，避免每次调用结果不同）。
        schedule 为空 -> False（全天不活跃，仅被动）。
        """
        now = now if now is not None else time.time()
        local = datetime.fromtimestamp(now)
        now_min = local.hour * 60 + local.minute

        cfg = self._config_getter()
        schedule = cfg.get("schedule", []) or []
        for seg in schedule:
            if not isinstance(seg, dict):
                continue
            start = seg.get("start")
            end = seg.get("end")
            s = _parse_hhmm(start) if isinstance(start, str) else None
            e = _parse_hhmm(end) if isinstance(end, str) else None
            if s is None or e is None:
                continue
            # 不处理跨午夜（end < start）的时段；默认配置无跨午夜
            if s <= e:
                if s <= now_min < e:
                    return True
            else:
                # 跨午夜：如 22:00-02:00
                if now_min >= s or now_min < e:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # 回放
    # ------------------------------------------------------------------ #

    async def replay(self, name: str, speed: float) -> None:
        """历史回放：按倍速喂入 on_message，强制不发送（replay_active 视同 dry_run）。"""
        try:
            self._replay_stop = False
            self._replay_active = True
            path = self._data_dir / "replay" / f"{name}.jsonl"
            feed_fn = self._make_replay_feed()
            self._log("info", f"[ProSocial] replay: 开始 {path} speed={speed}")
            stats = await self._replay_engine.run(
                path, speed, feed_fn, lambda: self._replay_stop
            )
            self._log(
                "info",
                f"[ProSocial] replay: 完成 total={stats.get('total', 0)} "
                f"fed={stats.get('fed', 0)} skipped={stats.get('skipped', 0)}",
            )
        except Exception as e:
            self._log("error", f"[ProSocial] replay 异常: {e}")
        finally:
            self._replay_active = False

    def stop_replay(self) -> None:
        """停止回放（置 _replay_stop，ReplayEngine 下一轮检查时中断）。"""
        self._replay_stop = True

    def _make_replay_feed(self) -> Callable[[dict], Awaitable[None]]:
        """构造回放 feed 函数：把回放消息当作普通消息喂入 on_message（is_wake=False）。"""

        async def feed(msg: dict) -> None:
            await self.on_message(
                group_id=str(msg.get("group_id", "")),
                umo=str(msg.get("group_id", "")),  # 回放无真实 umo，用 group_id 占位
                user_id=str(msg.get("user_id", "")),
                nickname=str(msg.get("nickname", "")),
                text=str(msg.get("text", "")),
                ts=float(msg.get("ts", 0.0)),
                is_wake=False,
            )

        return feed

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """包 embed_fn，失败计数；连续失败 3 次降级，5 分钟后重试恢复。"""
        now = time.time()
        if self._embed_degraded:
            if now < self._embed_degraded_until:
                return None
            # 到重试时间，解除降级尝试一次
            self._embed_degraded = False
            self._embed_fail_count = 0

        if not texts:
            return None
        try:
            embs = await self._embed_fn(texts)
            # 成功：重置
            self._embed_fail_count = 0
            self._embed_degraded = False
            return embs
        except Exception as e:
            self._embed_fail_count += 1
            self._log(
                "warning", f"[ProSocial] _embed 失败 ({self._embed_fail_count}): {e}"
            )
            if self._embed_fail_count >= _EMBED_FAIL_THRESHOLD:
                self._embed_degraded = True
                self._embed_degraded_until = now + _EMBED_DEGRADED_RECOVER_SEC
                self._log("warning", "[ProSocial] _embed 连续失败，进入降级模式 5 分钟")
            return None

    async def _llm(self, prompt: str) -> str | None:
        """包 llm_fn，失败 log warning 返回 None。"""
        try:
            return await self._llm_fn(prompt)
        except Exception as e:
            self._log("warning", f"[ProSocial] _llm 失败: {e}")
            return None

    async def _send(self, umo: str, text: str) -> bool:
        """包 send_fn；replay_active 时返回 False 不发送；失败 log warning 返回 False。"""
        if self._replay_active:
            return False  # 回放强制不发送
        if not umo:
            self._log("warning", "[ProSocial] _send: umo 为空，跳过发送")
            return False
        try:
            ok = await self._send_fn(umo, text)
            return bool(ok)
        except Exception as e:
            self._log("warning", f"[ProSocial] _send 失败: {e}")
            return False

    def _cooldown_ratio(self, g: dict, cfg: dict) -> float:
        """最近 cooldown_messages 条消息窗口内 bot 占比（0~1）。"""
        window: deque = g["cooldown_window"]
        n = int(cfg.get("cooldown_messages", 4))
        if n <= 0 or not window:
            return 0.0
        items = list(window)[-n:]
        if not items:
            return 0.0
        bot_count = sum(1 for _ts, is_bot in items if is_bot)
        return bot_count / len(items)

    def _check_state_expiry(self, g: dict, now: float) -> None:
        """懒检查：state_until 过期则按当前 state 回退到 IDLE。

        - EXPECTING_REPLY -> IDLE
        - COOLDOWN -> IDLE
        - ACTIVE_MONITORING -> IDLE（_main_loop 也会处理，此处为兜底）
        - GLANCING -> IDLE
        """
        if g["state_until"] <= 0:
            return
        if now < g["state_until"]:
            return
        # 过期：带时长的状态回 IDLE
        if g["state"] in (
            GroupState.EXPECTING_REPLY,
            GroupState.COOLDOWN,
            GroupState.ACTIVE_MONITORING,
            GroupState.GLANCING,
        ):
            g["state"] = GroupState.IDLE
            g["state_until"] = 0.0


def _parse_hhmm(s: str) -> int | None:
    """解析 'HH:MM' 为当日分钟数（0~1439）；非法返回 None。"""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m
