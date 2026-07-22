"""对话惯性 + 等待窗口 — v0.2 模块 I（agent-inertia）。

职责：
- InertiaManager：回复后切换阈值倍率（降低阈值 = 提高回复概率），主动话题临时提升与生命周期管理。
- WaitWindow：回复后收集同触发用户的连续后续消息，合并为一条交 LLM 生成连贯回复。

时间单位：InertiaManager 使用**秒**，WaitWindow 使用**毫秒**（契约注明，勿混）。
硬性约束：core/ 模块，禁止 import astrbot；配置通过 config_getter() 实时获取。
"""

from __future__ import annotations

import time
from collections.abc import Callable

# 概率提升 -> 阈值倍率映射系数（PRD F12：multiplier = ∏(1 − boost × SCALE)）
INERTIA_PROB_SCALE = 0.5


class InertiaManager:
    """每群一个实例（scheduler 持有 dict[group_id, InertiaManager]）。时间单位：秒。

    回复后窗口（after_reply_until）：降低阈值，使后续消息更容易触发回复。
    主动话题窗口（proactive_until）：额外降低阈值，同时追踪是否有人回应。
    """

    def __init__(
        self,
        config_getter: Callable[[], dict],
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        """初始化惯性管理器。

        Args:
            config_getter: 每次调用返回实时配置 dict，用于获取 probability_duration、
                           proactive_boost_duration 等参数。
            now_fn: 时间源（默认 time.time），测试时注入可控时钟。
        """
        self._config_getter = config_getter
        self._now_fn = now_fn if now_fn is not None else time.time

        # 回复后窗口结束时间（epoch 秒），初始 0 表示不活跃
        self.after_reply_until: float = 0.0
        # 主动话题窗口结束时间（epoch 秒），初始 0 表示不活跃
        self.proactive_until: float = 0.0
        # 是否正在等待主动话题的回应
        self.proactive_awaiting: bool = False
        # 主动话题间接成功次数（有人回应）
        self.indirect_success: int = 0
        # 主动话题失败次数（超时无回应）
        self.proactive_failure: int = 0

    # ── 公共接口 ──────────────────────────────────────────────

    def on_reply(self, now: float, is_proactive: bool = False) -> None:
        """Bot 成功回复后调用，开启惯性窗口。

        总是开启 after_reply 窗口（probability_duration 秒）。
        若 is_proactive=True（轮询主动发起话题），额外开启 proactive 窗口并进入等待状态。

        Args:
            now: 当前时间（epoch 秒）。
            is_proactive: 是否为主动发起话题（非被动接话）。
        """
        cfg = self._config_getter()
        self.after_reply_until = now + float(cfg.get("probability_duration", 30))
        if is_proactive:
            self.proactive_until = now + float(cfg.get("proactive_boost_duration", 60))
            self.proactive_awaiting = True

    def on_user_message(self, now: float) -> bool:
        """收到用户消息时调用，检测是否有人回应主动话题。

        若 proactive_awaiting 且 proactive 窗口未过期（now < proactive_until），
        视为有人回应主动话题——取消等待、记一次间接成功。

        Args:
            now: 当前时间（epoch 秒）。

        Returns:
            True 表示有人回应了主动话题（间接成功），False 表示无影响。
        """
        if self.proactive_awaiting and now < self.proactive_until:
            self.proactive_awaiting = False
            self.indirect_success += 1
            return True
        return False

    def check_proactive_timeout(self, now: float) -> bool:
        """轮询/批处理前调用，检测主动话题是否超时无回应。

        若 proactive_awaiting 且 proactive 窗口已过期（now >= proactive_until），
        视为超时——记一次失败、取消等待。

        Args:
            now: 当前时间（epoch 秒）。

        Returns:
            True 表示主动话题超时（失败），False 表示无变化。
        """
        if self.proactive_awaiting and now >= self.proactive_until:
            self.proactive_awaiting = False
            self.proactive_failure += 1
            return True
        return False

    def threshold_multiplier(self, now: float) -> float:
        """计算惯性阈值倍率（≤1.0，越低越容易触发回复）。

        公式：mult = 1.0，若在 after_reply 窗口内则
        mult *= (1 − after_reply_probability × INERTIA_PROB_SCALE)，
        若在 proactive 窗口内则额外
        mult *= (1 − proactive_temp_boost × INERTIA_PROB_SCALE)。
        默认配置下最低约 0.7 × 0.75 = 0.525。

        Args:
            now: 当前时间（epoch 秒）。

        Returns:
            阈值倍率 [0.0, 1.0]，max(mult, 0.0) 保证非负。
        """
        cfg = self._config_getter()
        mult = 1.0
        if now < self.after_reply_until:
            mult *= (
                1.0
                - float(cfg.get("after_reply_probability", 0.6)) * INERTIA_PROB_SCALE
            )
        if now < self.proactive_until:
            mult *= (
                1.0 - float(cfg.get("proactive_temp_boost", 0.5)) * INERTIA_PROB_SCALE
            )
        return max(mult, 0.0)

    def snapshot(self, now: float | None = None) -> dict:
        """返回当前惯性状态快照（供 Dashboard / 调试）。

        Args:
            now: 当前时间（epoch 秒），默认 now_fn()。

        Returns:
            dict 含 after_reply_active、proactive_active、proactive_awaiting、
            indirect_success、proactive_failure。
        """
        if now is None:
            now = self._now_fn()
        return {
            "after_reply_active": now < self.after_reply_until,
            "proactive_active": now < self.proactive_until,
            "proactive_awaiting": self.proactive_awaiting,
            "indirect_success": self.indirect_success,
            "proactive_failure": self.proactive_failure,
        }


class WaitWindow:
    """每群一个实例。收集**触发用户**的连续后续消息合并为一条。时间单位：毫秒。

    生命周期：open() → add() 循环 → should_close() → 取 merged_text() → close()。
    关闭条件：超时（deadline） / 收满 max_extra 条 / 收到 @ 消息强制关闭。
    """

    def __init__(self, *, duration_ms: int, max_extra: int) -> None:
        """初始化等待窗口（不自动开窗，需调用 open()）。

        Args:
            duration_ms: 窗口时长（毫秒），<=0 表示禁用（open() 无效果）。
            max_extra: 最大收集条数（不含触发消息）。
        """
        self.duration_ms = duration_ms
        self.max_extra = max_extra
        self._active: bool = False
        self.trigger_user: str = ""
        self.deadline: float = 0.0
        self.texts: list[str] = []
        self.full: bool = False
        self.force_close: bool = False

    @property
    def active(self) -> bool:
        """窗口是否处于活跃收集中。"""
        return self._active

    def open(self, now_ms: float, trigger_user_id: str) -> None:
        """开窗，开始收集 trigger_user 的后续消息。

        duration_ms <= 0 时不开窗（_active 保持 False），即禁用等待窗口。

        Args:
            now_ms: 当前时间（毫秒）。
            trigger_user_id: 触发用户 ID，仅收集该用户的消息。
        """
        if self.duration_ms <= 0:
            return
        self._active = True
        self.trigger_user = trigger_user_id
        self.deadline = now_ms + self.duration_ms
        self.texts = []
        self.full = False
        self.force_close = False

    def add(self, now_ms: float, user_id: str, text: str, is_at: bool) -> None:
        """向窗口添加一条消息。

        仅当 _active 时处理：
        - is_at 为 True 时置 force_close（无论谁发的）。
        - 仅收集 trigger_user 的消息文本；其他用户消息忽略不进窗口。
        - 收满 max_extra 条时置 full。

        Args:
            now_ms: 当前时间（毫秒）。
            user_id: 消息发送者 ID。
            text: 消息文本。
            is_at: 是否包含 @Bot。
        """
        if not self._active:
            return
        # @ 消息强制关闭窗口（无论谁发的）
        if is_at:
            self.force_close = True
        # 仅收集触发用户的消息
        if user_id == self.trigger_user:
            if text:
                self.texts.append(text)
            if len(self.texts) >= self.max_extra:
                self.full = True

    def should_close(self, now_ms: float) -> bool:
        """判断窗口是否应该关闭。

        关闭条件（任一满足）：超时（now_ms >= deadline）、收满（full）、
        强制关闭（force_close）。不活跃时返回 False。

        Args:
            now_ms: 当前时间（毫秒）。

        Returns:
            True 表示应该关闭窗口。
        """
        if not self._active:
            return False
        return now_ms >= self.deadline or self.full or self.force_close

    def merged_text(self) -> str:
        """获取收集到的所有消息文本，用换行符拼接。

        Returns:
            拼接后的文本，无消息时返回空字符串。
        """
        return "\n".join(self.texts)

    def close(self) -> None:
        """关闭窗口，重置所有状态。"""
        self._active = False
        self.texts = []
        self.full = False
        self.force_close = False
