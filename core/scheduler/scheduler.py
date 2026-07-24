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

from ..common.emoji_filter import strip_emoji
from ..common.models import GroupState, LogicalMessage
from ..decision.adaptive import AdaptiveThreshold, SendQuota
from ..decision.fatigue import FatigueManager
from ..decision.inertia import InertiaManager
from ..decision.interest import InterestManager
from ..storage.metrics import DecisionLog, MetricsStore
from ..storage.ratelimit import TokenBucketRateLimiter
from ..tracking.buffer import GroupBuffer
from ..tracking.context import GroupContext
from ..tracking.tracker import PersonalTracker
from .autotune_collector import AutotuneStatsMixin
from .batch_pipeline import BatchPipelineMixin
from .bot_events import BotEventsMixin
from .replay import ReplayEngine

# 嵌入降级阈值：连续失败次数
_EMBED_FAIL_THRESHOLD = 3
# 嵌入降级恢复重试间隔（秒）
_EMBED_DEGRADED_RECOVER_SEC = 300.0
# msg_timestamps deque 上限（v0.3.7：100→500，覆盖高频群 5 分钟消息量，
# 避免 maxlen 不足导致 60 秒窗口统计少算）
_MSG_TS_MAX = 500
# cooldown_window deque 上限（防内存增长）
_COOLDOWN_WIN_MAX = 500
# v0.3.7：cooldown_ratio 时间窗口（秒），仅统计此窗口内的消息计算 bot 占比
_COOLDOWN_TIME_WINDOW = 300.0


class SocialScheduler(BatchPipelineMixin, BotEventsMixin, AutotuneStatsMixin):
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
        # v0.2.8 主动回复管线注入回调：(umo, text, hint, group_id) -> bool
        # None 时所有主动回复走旧路径（llm_fn + send_fn），既有行为不变
        inject_fn: Callable[[str, str, str, str], Awaitable[bool]] | None = None,
        # v0.2.9 LLM 自动调参触发回调：() -> Awaitable[dict]
        # 由 main.py 内部判断速率限制（cooldown / max_per_day），返回
        # {"ok": bool, "error": "rate_limited" | ...}。None → 自动触发关闭。
        autotune_trigger_fn: Callable[[], Awaitable[dict]] | None = None,
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
        # v0.2.8 管线注入回调；None → 走旧路径（直连 llm_fn + send_fn）
        self._inject = inject_fn
        # v0.2.9 LLM 自动调参触发回调；None → 不触发自动调参（既有行为不变）
        self._autotune_trigger = autotune_trigger_fn
        # v0.2.8 自适应阈值状态缓存：start() 从 KV 加载，_get_group 创建群时恢复
        self._adaptive_state_cache: dict | None = None

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
            # v0.2.8 自适应阈值控制器 + 每群发送频率硬上限
            "adaptive": AdaptiveThreshold(),
            "quota": SendQuota(),
            # v0.3.5 F1：短批次合并尝试次数（达 max_attempts 后强制评估）
            "short_batch_attempts": 0,
            # v0.3.7：上次主动消息发送时间戳（用于 proactive_min_interval 冷却）
            "last_proactive_ts": 0.0,
        }
        # v0.2.8 从缓存恢复自适应阈值状态（start() 预加载的 KV 数据）
        if self._adaptive_state_cache is not None:
            try:
                saved = self._adaptive_state_cache.get(group_id)
                if isinstance(saved, dict):
                    g["adaptive"].restore(saved)
            except Exception:
                pass
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

        # v0.2.8 预加载自适应阈值状态缓存（_get_group 创建群时恢复）
        try:
            adaptive_state = await self._kv_get("adaptive_state", {})
            if isinstance(adaptive_state, dict):
                self._adaptive_state_cache = adaptive_state
                # 已存在的群就地恢复
                for gid, g in self._groups.items():
                    if "adaptive" in g:
                        g["adaptive"].restore(adaptive_state.get(gid, {}))
        except Exception as e:
            self._log(
                "warning", f"[ProSocial] scheduler.start: 加载 adaptive_state 失败: {e}"
            )

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

        # v0.2.8 持久化每群自适应阈值状态（mult + since_eval，重启恢复）
        try:
            state = {
                gid: g["adaptive"].state()
                for gid, g in self._groups.items()
                if "adaptive" in g
            }
            await self._kv_set("adaptive_state", state)
        except Exception as e:
            self._log("warning", f"[ProSocial] stop: 持久化 adaptive_state 失败: {e}")

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
            # v0.3.5 F2：emoji 过滤——配置启用时在入缓冲前移除 emoji 字符
            filter_emoji = bool(cfg.get("emoji_filter_enabled", True))
            entry_text = text
            if filter_emoji:
                entry_text = strip_emoji(text)
                if not entry_text.strip():
                    # 纯 emoji 消息：不入缓冲（但仍已记录上下文窗口）
                    return
            g["buffer"].append(
                user_id,
                nickname,
                entry_text,
                ts,
                group_id,
                is_wake=is_wake,
                filter_emoji=False,  # 已在此处过滤，append 内不再重复过滤
            )

            # 9. 调度批次任务（若无活跃任务则创建）
            t = g.get("batch_task")
            if t is None or t.done():
                g["batch_task"] = asyncio.create_task(self._schedule_batch(group_id))
        except Exception as e:
            self._log("error", f"[ProSocial] on_message 异常 group={group_id}: {e}")

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
        """v0.3.7：时间窗口内 bot 消息占比（0~1）。

        取最近 ``_COOLDOWN_TIME_WINDOW``（300 秒）内的消息，计算 bot 占比。
        旧逻辑取最后 N 条消息（不考虑时间），冷群中会跨越数小时导致误判。
        若时间窗口内消息数 < cooldown_messages，补充取最后 N 条兜底
        （避免冷群窗口内无消息时返回 0.0 丢失信号）。
        """
        window: deque = g["cooldown_window"]
        n = int(cfg.get("cooldown_messages", 4))
        if n <= 0 or not window:
            return 0.0
        now = time.time()
        # 时间窗口过滤：仅统计最近 _COOLDOWN_TIME_WINDOW 秒内的消息
        time_filtered = [
            (ts, is_bot) for ts, is_bot in window
            if (now - ts) <= _COOLDOWN_TIME_WINDOW
        ]
        if time_filtered:
            bot_count = sum(1 for _ts, is_bot in time_filtered if is_bot)
            return bot_count / len(time_filtered)
        # 时间窗口内无消息（群很冷）：退化为取最后 N 条兜底
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
