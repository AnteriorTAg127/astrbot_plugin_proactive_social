"""test_v0_2_9.py —— v0.2.9 测试用例（Module G）。

测试对象：
- core/tune_controller.py → TuneRateLimiter（纯标准库，无 I/O 依赖）
- core/adaptive.py → AdaptiveThreshold 扩展（record 返回 bool / window_rate / window_size）
- core/config_store.py → v0.2.9 新增 7 项配置的默认值与 VALIDATORS
- core/scheduler.py → _maybe_autotune / collect_tune_stats 扩展 / autotune_trigger_fn 注入
- main.py → TUNE_DENYLIST / _writable_keys / llm_autotune 全视野 + 速率限制 +
  apply 分流（标量 / persona / keywords_patch / persona_revision / DENYLIST 过滤）+
  _build_tune_prompt 全视野 / _tune_current_config / _rate_limit_status /
  _apply_keywords_patch / _autotune_trigger 自动应用

覆盖 spec.md F1-F4 全部验收点：
  F1 LLM 全视野 / F2 denylist 可写键 + apply 分流 /
  F3 触发率越界自动调参 / F4 调参速率限制

测试策略：
- core/ 纯模块直接实例化断言。
- main.py 离线不可 import（强依赖 astrbot 运行时），采用两种方式：
  ① 复制 main.py 关键方法到 _MockPlugin 类（与 test_v0_2_1.py 一致），
     验证 llm_autotune / apply 分流 / prompt 构造逻辑；
  ② 直接通过 sys.modules 注入 mock astrbot 模块后 import main，
     验证 TUNE_DENYLIST 类属性与 _writable_keys 静态方法。
- scheduler 集成测试：inline 构造 scheduler，注入 autotune_trigger_fn mock，
  复用 conftest 的 mock_llm/mock_embed/mock_send/mock_kv/mock_log/tmp_data_dir。
- 异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio。

不重复 test_web.py 已覆盖的 post_autotune handler 12 项测试。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ======================================================================
# 辅助：注入 mock astrbot 模块（main.py 强依赖 astrbot 运行时）
# ======================================================================


_PLUGIN_PKG_NAME = "astrbot_plugin_proactive_social"


def _install_astrbot_mocks() -> None:
    """向 sys.modules 注入最小化 astrbot mock，使 main.py 可被 import。

    main.py 顶部导入：
        from astrbot.api import AstrBotConfig, logger
        from astrbot.api.event import AstrMessageEvent, MessageChain, filter
        from astrbot.api.provider import ProviderRequest
        from astrbot.api.star import Context, Star, register
        from astrbot.api.web import json_response, request
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    所有名字都 mock 成可调用对象或简单占位类。多次调用幂等。

    同时把插件根目录作为 ``astrbot_plugin_proactive_social`` 包注册到 sys.modules，
    使 main.py 的相对导入（``from .core.config_store import ...``）能解析——
    直接 ``import main`` 会触发 ``ImportError: attempted relative import with
    no known parent package``。
    """

    def _ensure_module(name: str) -> types.ModuleType:
        if name in sys.modules and sys.modules[name] is not None:
            return sys.modules[name]
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # 把插件根目录注册为 ``astrbot_plugin_proactive_social`` 包，使相对导入可解析。
    # main.py 用 ``from .core.config_store import ...``，必须以包内子模块方式加载。
    plugin_root = Path(__file__).resolve().parent.parent
    pkg = _ensure_module(_PLUGIN_PKG_NAME)
    pkg.__path__ = [str(plugin_root)]
    pkg.__package__ = _PLUGIN_PKG_NAME

    # astrbot.api
    api = _ensure_module("astrbot.api")
    if not hasattr(api, "AstrBotConfig"):
        api.AstrBotConfig = dict  # type: ignore[attr-defined]
    if not hasattr(api, "logger"):
        log = MagicMock()
        for lv in ("info", "warning", "error", "debug"):
            setattr(log, lv, MagicMock())
        api.logger = log  # type: ignore[attr-defined]

    # astrbot.api.event
    ev = _ensure_module("astrbot.api.event")

    class _FakeEventMessageType:
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _FakePermissionType:
        ADMIN = "admin"

    class _FakeFilter:
        EventMessageType = _FakeEventMessageType
        PermissionType = _FakePermissionType

        def command_group(self, name):
            """模拟 AstrBot command_group：返回的函数附加 ``command`` 方法。

            AstrBot ``@filter.command_group("foo")`` 装饰 ``def foo(self)``
            后，``foo`` 上挂载 ``.command(name)`` 方法供子指令注册用。
            本 mock 让 ``foo`` 变成带 ``command`` 属性的可调用对象，
            ``command(name)`` 返回装饰器（原函数透传），保证类体可执行。
            """

            def command(cmd_name):
                def deco(fn):
                    return fn

                return deco

            def make_group():
                def group_fn(*args, **kwargs):
                    pass

                group_fn.command = command  # type: ignore[attr-defined]
                return group_fn

            def deco(fn):
                return make_group()

            return deco

        def permission_type(self, p):
            def deco(fn):
                return fn

            return deco

        def event_message_type(self, t):
            def deco(fn):
                return fn

            return deco

        def on_astrbot_loaded(self):
            def deco(fn):
                return fn

            return deco

        def on_llm_request(self):
            def deco(fn):
                return fn

            return deco

        def after_message_sent(self):
            def deco(fn):
                return fn

            return deco

        def command(self, name):
            def deco(fn):
                return fn

            return deco

    if not hasattr(ev, "filter"):
        ev.filter = _FakeFilter()  # type: ignore[attr-defined]
    if not hasattr(ev, "AstrMessageEvent"):
        ev.AstrMessageEvent = object  # type: ignore[attr-defined]
    if not hasattr(ev, "MessageChain"):
        class _FakeMessageChain:
            def message(self, text):
                return self

        ev.MessageChain = _FakeMessageChain  # type: ignore[attr-defined]

    # astrbot.api.provider
    prov = _ensure_module("astrbot.api.provider")
    if not hasattr(prov, "ProviderRequest"):
        prov.ProviderRequest = object  # type: ignore[attr-defined]

    # astrbot.api.star
    star = _ensure_module("astrbot.api.star")

    class _FakeStar:
        pass

    class _FakeContext:
        pass

    def _fake_register(name, *_args, **_kw):
        def deco(cls):
            return cls

        return deco

    if not hasattr(star, "Star"):
        star.Star = _FakeStar  # type: ignore[attr-defined]
    if not hasattr(star, "Context"):
        star.Context = _FakeContext  # type: ignore[attr-defined]
    if not hasattr(star, "register"):
        star.register = _fake_register  # type: ignore[attr-defined]

    # astrbot.api.web
    web = _ensure_module("astrbot.api.web")
    if not hasattr(web, "json_response"):

        def _json_response(body, status_code=200):
            return body

        web.json_response = _json_response  # type: ignore[attr-defined]
    if not hasattr(web, "request"):
        web.request = MagicMock()  # type: ignore[attr-defined]

    # astrbot.core.utils.astrbot_path
    ap = _ensure_module("astrbot.core.utils.astrbot_path")
    if not hasattr(ap, "get_astrbot_data_path"):

        def _gadp():
            return str(Path(__file__).resolve().parent.parent / "data")

        ap.get_astrbot_data_path = _gadp  # type: ignore[attr-defined]

    # astrbot.core.platform.* —— _make_inject_fn 内部延迟 import，不强制注入；
    # 若测试触发可由测试侧自行 patch，这里仅占位
    for sub in (
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.astrbot_message",
        "astrbot.core.platform.message_type",
        "astrbot.core.utils",
        "astrbot.core.agent",
        "astrbot.core.agent.message",
    ):
        _ensure_module(sub)


# 首次 import 时安装 mock（幂等）
_install_astrbot_mocks()


# ======================================================================
# 辅助：构造 SocialScheduler（注入 autotune_trigger_fn，绕过 conftest 工厂）
# ======================================================================


def _make_scheduler(
    *,
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    inject_fn=None,
    autotune_trigger_fn=None,
):
    """inline 构造 SocialScheduler，支持传入 autotune_trigger_fn。

    与 conftest.scheduler_factory 行为一致：模拟 start() 的预加载
    （group_enable_cache={}）但不真正 start()。
    """
    from core.decision.interest import InterestManager
    from core.storage.ratelimit import TokenBucketRateLimiter
    from core.scheduler import SocialScheduler

    interest_mgr = InterestManager(tmp_data_dir, mock_log)
    rate_limiter = TokenBucketRateLimiter(
        int(mock_config.get("embedding_rate_limit_per_min", 30))
    )
    sched = SocialScheduler(
        config_getter=lambda: mock_config,
        interest_mgr=interest_mgr,
        send_fn=mock_send,
        llm_fn=mock_llm,
        embed_fn=mock_embed,
        rate_limiter=rate_limiter,
        kv_get_fn=mock_kv.get,
        kv_set_fn=mock_kv.set,
        log_fn=mock_log,
        data_dir=tmp_data_dir,
        inject_fn=inject_fn,
        autotune_trigger_fn=autotune_trigger_fn,
    )
    sched._group_enable_cache = {}
    return sched


async def _seed_message(
    sched,
    group_id,
    text,
    ts=None,
    user_id="u1",
    nickname="Alice",
    umo="aiocqhttp:g1",
    is_wake=False,
):
    """调用 on_message 喂入消息，然后取消自动调度的批次任务。"""
    if ts is None:
        ts = time.time()
    await sched.on_message(
        group_id=group_id,
        umo=umo,
        user_id=user_id,
        nickname=nickname,
        text=text,
        ts=ts,
        is_wake=is_wake,
    )
    g = sched._get_group(group_id)
    t = g.get("batch_task")
    if t is not None and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        g["batch_task"] = None


def _set_interest(sched, make_interest_data, centroids=None, high_kw=None, hate_kw=None):
    """直接设置 interest_mgr._data，避免走 LLM/embed 流程。"""
    sched._interest_mgr._data = make_interest_data(
        centroids=centroids or {},
        high_kw=high_kw or [],
        hate_kw=hate_kw or [],
    )


# ======================================================================
# 辅助：构造 _MockPlugin（镜像 main.py ProSocialPlugin 的核心逻辑）
# ======================================================================


def _make_mock_plugin(
    *,
    mock_config,
    mock_llm,
    mock_embed,
    tmp_data_dir,
    mock_log,
):
    """构造一个最小化的 _MockPlugin，复用 main.py ProSocialPlugin 的方法。

    与 test_v0_2_1.py 的 _MockPlugin 模式一致——避免完整 initialize / AstrBot 运行时，
    但能直接调用 llm_autotune / _build_tune_prompt / _apply_keywords_patch /
    _rate_limit_status / _autotune_trigger 等方法。
    """
    # 以包内子模块方式加载 main.py，使其相对导入（from .core.xxx）能解析
    ProSocialPlugin = _load_main_prosocial_plugin()

    class _MockInterestMgr:
        """镜像 InterestManager 的最小 mock，仅暴露 llm_autotune 用到的方法。"""

        def __init__(self):
            self._data = None
            self._rejected = {"examples": [], "keywords": []}
            self.regenerate = AsyncMock(return_value=None)
            self.add_item = AsyncMock(return_value=(True, ""))
            self.remove_item = AsyncMock(return_value=(True, ""))
            self.apply_rejected = AsyncMock(return_value=(True, ""))
            self.get_rejected = MagicMock(return_value={"examples": [], "keywords": []})
            # v0.3.5 F5：batch_update 替代逐次 add/remove，单次重算质心
            self.batch_update = AsyncMock(return_value=(0, ""))

        def export_view(self) -> dict:
            if self._data is None:
                return {
                    "generated": False,
                    "persona_hash": "",
                    "items": [],
                    "hate_keywords": [],
                    "high_interest_keywords": [],
                    "rejected": {"examples": [], "keywords": []},
                }
            return {
                "generated": True,
                "persona_hash": "mock_hash",
                "items": [],
                "hate_keywords": [],
                "high_interest_keywords": [],
                "rejected": {"examples": [], "keywords": []},
            }

    class _MockConfigStore:
        """镜像 ConfigStore 的最小 mock，仅暴露 llm_autotune 用到的方法。"""

        def __init__(self, cfg: dict):
            self._cfg = cfg

        def get(self) -> dict:
            return self._cfg

        def snapshot(self) -> dict:
            return dict(self._cfg)

        async def set_many(self, patch: dict) -> tuple[bool, str]:
            # 简化校验：仅做 isinstance + 范围粗校验，DENYLIST 已在 main.py 过滤
            from core.storage.config_store import ConfigStore

            for k, v in patch.items():
                if k not in ConfigStore.DEFAULT_CONFIG:
                    return False, f"未知键: {k}"
                vtype, lo, hi = ConfigStore.VALIDATORS.get(k, (None, None, None))
                if vtype is bool:
                    if not isinstance(v, bool):
                        return False, f"{k} 必须是布尔值"
                elif vtype is int:
                    if isinstance(v, bool) or not isinstance(v, int):
                        return False, f"{k} 必须是整数"
                    if lo is not None and v < lo:
                        return False, f"{k} 超出范围"
                    if hi is not None and v > hi:
                        return False, f"{k} 超出范围"
                elif vtype is float:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        return False, f"{k} 必须是数字"
                    if lo is not None and v < lo:
                        return False, f"{k} 超出范围"
                    if hi is not None and v > hi:
                        return False, f"{k} 超出范围"
            self._cfg.update(patch)
            return True, ""

        async def get_kv(self, key, default=None):
            return default

        async def set_kv(self, key, value):
            return None

        async def close(self):
            return None

    # 构造 ProSocialPlugin 实例（绕过 __init__，直接绑定属性）
    plugin = ProSocialPlugin.__new__(ProSocialPlugin)
    plugin.config = dict(mock_config)
    plugin._SPECIAL_KEYS = frozenset({"chat_provider_id", "embedding_provider_id"})
    plugin._config_store = _MockConfigStore(mock_config)
    plugin.interest_mgr = _MockInterestMgr()
    plugin._llm_fn = mock_llm
    plugin._embed_fn = mock_embed
    plugin._log = mock_log
    plugin.data_dir = tmp_data_dir
    plugin._last_tune_suggestion = None
    # v0.2.9 F4：TuneRateLimiter 单例
    from core.storage.tune_controller import TuneRateLimiter

    plugin._tune_limiter = TuneRateLimiter()
    # scheduler mock —— 仅 collect_tune_stats 返回空统计
    sched_mock = MagicMock()
    sched_mock.collect_tune_stats = MagicMock(
        return_value={
            "total": 0,
            "triggered_count": 0,
            "triggered_rate": 0.0,
            "suppressed_hist": {},
            "score_mean": 0.0,
            "score_median": 0.0,
            "score_min": 0.0,
            "score_max": 0.0,
            "threshold_mean": 0.0,
            "hit_level_hist": {},
            "factors_mean": {
                "s_int": 0.0,
                "s_topic": 0.0,
                "s_resp": 0.0,
                "c_cooldown": 0.0,
                "p_silence": 0.0,
            },
            "fatigue_value_mean": 0.0,
            "config": dict(mock_config),
            "adaptive_summary": [],
        }
    )
    plugin.scheduler = sched_mock
    # context mock —— _build_tune_prompt 用到 self.context.get_provider_by_id
    ctx = MagicMock()
    ctx.get_provider_by_id = MagicMock(return_value=None)
    plugin.context = ctx
    return plugin


# ======================================================================
# 1. TuneRateLimiter 测试（8 项）
# ======================================================================


def test_tune_rate_limiter_allow_first_call():
    """首次调用 allow 返回 (True, "")（无 cooldown / 无 daily_cap）。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    ok, reason = limiter.allow(1000.0, 3.0, 4)
    assert ok is True
    assert reason == ""


def test_tune_rate_limiter_cooldown_rejects():
    """冷却期内调用 → (False, "cooldown")。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 第一次调用 + record
    limiter.record(1000.0)
    # 1 小时后仍未到 3 小时冷却 → 拒绝
    ok, reason = limiter.allow(1000.0 + 3600.0, 3.0, 4)
    assert ok is False
    assert reason == "cooldown"


def test_tune_rate_limiter_daily_cap_rejects():
    """达到日上限 → (False, "daily_cap")。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 填满 4 次日配额（无 cooldown 限制：cooldown=0）
    for i in range(4):
        limiter.record(1000.0 + i)
    ok, reason = limiter.allow(1000.0 + 5, 0.0, 4)
    assert ok is False
    assert reason == "daily_cap"


def test_tune_rate_limiter_cooldown_zero_means_unlimited():
    """cooldown=0 表示不限冷却——record 后立即可再调。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    limiter.record(1000.0)
    # cooldown=0 → 跳过冷却检查
    ok, reason = limiter.allow(1000.0 + 1.0, 0.0, 4)
    assert ok is True
    assert reason == ""


def test_tune_rate_limiter_max_per_day_zero_means_unlimited():
    """max_per_day=0 表示不限日数——历史再多也通过。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 填 100 条记录（24h 内）
    for i in range(100):
        limiter.record(1000.0 + i)
    # max_per_day=0 → 跳过日上限检查；cooldown=0 → 跳过冷却
    ok, reason = limiter.allow(1000.0 + 99, 0.0, 0)
    assert ok is True
    assert reason == ""


def test_tune_rate_limiter_record_increments_history():
    """record 后 history 长度增加，last_call 更新。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    assert limiter._last_call is None
    limiter.record(1000.0)
    assert len(limiter._history) == 1
    assert limiter._last_call == 1000.0
    limiter.record(2000.0)
    assert len(limiter._history) == 2
    assert limiter._last_call == 2000.0


def test_tune_rate_limiter_state_restore_roundtrip():
    """state/restore 往返一致。"""
    from core.storage.tune_controller import TuneRateLimiter

    a = TuneRateLimiter()
    a.record(1000.0)
    a.record(2000.0)
    s = a.state()
    assert s == {
        "history": [1000.0, 2000.0],
        "last_call": 2000.0,
        "force_history": [],
    }

    b = TuneRateLimiter()
    b.restore(s)
    assert list(b._history) == [1000.0, 2000.0]
    assert b._last_call == 2000.0
    # restore 后 allow 行为与 a 一致（cooldown 拒绝）
    ok_a, _ = a.allow(2500.0, 3.0, 4)
    ok_b, _ = b.allow(2500.0, 3.0, 4)
    assert ok_a == ok_b


def test_tune_rate_limiter_last_call_none_skips_cooldown():
    """_last_call=None（从未 record）时跳过冷却检查，allow 通过。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 从未 record → _last_call=None → 跳过 cooldown
    ok, reason = limiter.allow(1000.0, 3.0, 4)
    assert ok is True
    assert reason == ""
    # 即便 history 为空，daily_cap 检查也通过（0 < 4）
    assert limiter._last_call is None


# ======================================================================
# 2. AdaptiveThreshold 扩展测试（4 项）
# ======================================================================


def test_adaptive_record_returns_true_on_eval():
    """record 返回 True：满 EVAL_EVERY=20 时触发评估。"""
    from core.decision.adaptive import AdaptiveThreshold

    a = AdaptiveThreshold()
    # 前 19 条不评估
    for _ in range(19):
        assert a.record(0.8, True) is False
    # 第 20 条触发评估
    assert a.record(0.8, True) is True


def test_adaptive_record_returns_false_before_eval():
    """record 返回 False：未满 EVAL_EVERY 不评估。"""
    from core.decision.adaptive import AdaptiveThreshold

    a = AdaptiveThreshold()
    for i in range(19):
        assert a.record(0.5, True) is False
    # 21 条后又开始计数，第 21 条不评估
    a.record(0.5, True)  # 第 20 条评估
    assert a.record(0.5, True) is False  # 第 21 条（新周期第 1 条）


def test_adaptive_window_rate_empty_returns_zero():
    """window_rate 空窗口返回 0.0。"""
    from core.decision.adaptive import AdaptiveThreshold

    a = AdaptiveThreshold()
    assert a.window_rate() == 0.0
    assert a.window_size() == 0


def test_adaptive_window_rate_with_samples():
    """window_rate 有样本时返回正确触发率。"""
    from core.decision.adaptive import AdaptiveThreshold

    a = AdaptiveThreshold()
    # 4 条 triggered=True + 6 条 triggered=False → rate = 0.4
    for i in range(10):
        a.record(0.7, i < 4)
    assert a.window_size() == 10
    assert abs(a.window_rate() - 0.4) < 1e-9


# ======================================================================
# 3. TUNE_DENYLIST 测试（3 项）
# ======================================================================


def _load_main_prosocial_plugin():
    """以包内子模块方式加载 main.py 并返回 ProSocialPlugin 类。

    直接 ``from main import ProSocialPlugin`` 会触发 ``ImportError: attempted
    relative import with no known parent package``（main.py 用 ``from .core`` 相对导入）。
    必须以 ``astrbot_plugin_proactive_social.main`` 子模块方式加载。
    """
    import importlib

    main_mod = sys.modules.get(f"{_PLUGIN_PKG_NAME}.main")
    if main_mod is None:
        main_mod = importlib.import_module(f"{_PLUGIN_PKG_NAME}.main")
    return main_mod.ProSocialPlugin


def test_tune_denylist_contains_six_keys():
    """TUNE_DENYLIST 含 6 个安全敏感键。"""
    ProSocialPlugin = _load_main_prosocial_plugin()

    expected = {
        "enable",
        "dry_run",
        "group_whitelist",
        "group_mode",
        "chat_provider_id",
        "embedding_provider_id",
    }
    assert ProSocialPlugin.TUNE_DENYLIST == frozenset(expected)
    assert len(ProSocialPlugin.TUNE_DENYLIST) == 6


def test_writable_keys_excludes_denylist():
    """_writable_keys = DEFAULT_CONFIG - DENYLIST，不含任何 DENYLIST 键。"""
    ProSocialPlugin = _load_main_prosocial_plugin()
    from core.storage.config_store import ConfigStore

    writable = ProSocialPlugin._writable_keys()
    # 长度 = DEFAULT_CONFIG 长度 - 5（group_mode/group_whitelist/enable/dry_run 在 DEFAULT_CONFIG；
    # chat_provider_id/embedding_provider_id 不在 DEFAULT_CONFIG，属 SPECIAL_KEYS）
    # DENYLIST 中 chat_provider_id/embedding_provider_id 不在 DEFAULT_CONFIG，故不影响差集
    expected_len = len(ConfigStore.DEFAULT_CONFIG) - 4  # enable/dry_run/group_whitelist/group_mode
    assert len(writable) == expected_len
    # DENYLIST 中在 DEFAULT_CONFIG 的键不在 writable
    for k in ("enable", "dry_run", "group_whitelist", "group_mode"):
        assert k not in writable
    # persona_text/persona_knowledge 等敏感业务键应在 writable（LLM 可改）
    assert "persona_text" in writable
    assert "persona_knowledge" in writable
    assert "base_threshold" in writable


def test_llm_suggested_denylist_keys_are_dropped_and_noted():
    """LLM suggested_patch 含 DENYLIST 键时被丢弃且 analysis 末尾注明。"""
    plugin = _make_mock_plugin(
        mock_config={
            **_default_cfg_for_main(),
            "autotune_cooldown_hours": 0.0,
            "autotune_max_per_day": 0,
        },
        mock_llm=_MockLLMJson(
            {
                "analysis": "测试分析",
                "suggested_patch": {
                    "enable": False,  # DENYLIST
                    "base_threshold": 0.7,  # 可写
                    "dry_run": True,  # DENYLIST
                    "w_int": 1.5,  # 可写
                },
                "expected_effect": "效果",
            }
        ),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )

    result = asyncio.run(plugin.llm_autotune("analyze", force=True))
    assert result["ok"] is True
    suggested = result["suggested_patch"]
    # DENYLIST 键被丢弃
    assert "enable" not in suggested
    assert "dry_run" not in suggested
    # 可写键保留
    assert suggested["base_threshold"] == 0.7
    assert suggested["w_int"] == 1.5
    # analysis 末尾注明过滤
    assert "已过滤安全敏感键" in result["analysis"]
    assert "enable" in result["analysis"]
    assert "dry_run" in result["analysis"]


# ======================================================================
# 4. llm_autotune apply 分流测试（6 项）
# ======================================================================


class _MockLLMJson:
    """返回固定 JSON 字符串的 LLM mock。"""

    def __init__(self, response_dict: dict):
        self._response = json.dumps(response_dict, ensure_ascii=False)
        self.call_count = 0
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.call_count += 1
        self.calls.append(prompt)
        return self._response


def _default_cfg_for_main() -> dict:
    """从 conftest.default_config 取一份配置供 _MockPlugin 使用。"""
    from tests.conftest import default_config

    return default_config()


def test_llm_autotune_apply_scalar_only():
    """apply 纯标量 patch：走 ConfigStore.set_many，无 regenerate / 无 keywords_updated。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 直接给缓存建议（绕过 analyze）
    plugin._last_tune_suggestion = {
        "suggested_patch": {"base_threshold": 0.7, "w_int": 1.5},
        "suggested_keywords_patch": None,
        "persona_revision": None,
    }

    result = asyncio.run(plugin.llm_autotune("apply"))
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["updated"] == 2
    assert result["regenerate"] is False
    assert result["keywords_updated"] == 0
    # 配置确实写入
    assert plugin._config_store.get()["base_threshold"] == 0.7
    assert plugin._config_store.get()["w_int"] == 1.5


def test_llm_autotune_apply_persona_triggers_regenerate():
    """apply patch 含 persona_text → 触发后台 regenerate（regenerate=True）。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    plugin._last_tune_suggestion = {
        "suggested_patch": {"persona_text": "新人设"},
        "suggested_keywords_patch": None,
        "persona_revision": None,
    }

    # asyncio.create_task 在事件循环中调度；run 内部 yield 一次让 task 启动
    async def _run():
        result = await plugin.llm_autotune("apply")
        # 等待 create_task 调度的 _bg_regenerate_persona 完成
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["regenerate"] is True
    # interest_mgr.regenerate 被调用（后台 task）
    assert plugin.interest_mgr.regenerate.called


def test_llm_autotune_apply_keywords_patch_add():
    """apply keywords_patch.add → 后台调 interest_mgr.batch_update + apply_rejected。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    kp = {
        "add": [
            {"kind": "high_keyword", "label": "core", "text": "Python"},
            {"kind": "example", "label": "general", "text": "今天聊啥"},
        ],
        "remove": [],
    }
    # v0.3.5 F5：batch_update 返回 (2, "")
    plugin.interest_mgr.batch_update.return_value = (2, "")

    # v0.3.5 F5：apply 异步化——keywords_patch 走后台 task，需 await sleep 让 create_task 完成
    async def _run():
        result = await plugin.llm_autotune("apply", keywords_patch=kp)
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["applied"] is True
    # v0.3.5 F5：apply 立即返回，keywords_updated=0，background=true
    assert result["keywords_updated"] == 0
    assert result["background"] is True
    # batch_update 被调用 1 次（add 2 项 + remove 0 项合并一次）
    assert plugin.interest_mgr.batch_update.call_count == 1
    # apply_rejected 被调用（兜底重算质心）
    assert plugin.interest_mgr.apply_rejected.called


def test_llm_autotune_apply_keywords_patch_remove():
    """apply keywords_patch.remove → 后台调 interest_mgr.batch_update。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    kp = {
        "add": [],
        "remove": [
            {"kind": "hate_keyword", "label": "hate", "text": "广告"},
        ],
    }
    # v0.3.5 F5：batch_update 返回 (1, "")
    plugin.interest_mgr.batch_update.return_value = (1, "")

    async def _run():
        result = await plugin.llm_autotune("apply", keywords_patch=kp)
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["keywords_updated"] == 0
    assert result["background"] is True
    assert plugin.interest_mgr.batch_update.call_count == 1


def test_llm_autotune_apply_persona_revision_merges_into_persona_text():
    """apply persona_revision → 合并入 persona_text 走同路径（regenerate=True）。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    rev = "你是一只爱聊编程的猫娘。"

    async def _run():
        result = await plugin.llm_autotune("apply", persona_revision=rev)
        await asyncio.sleep(0.05)  # 等 create_task 调度的 regenerate
        return result

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["regenerate"] is True
    # persona_text 被合并（写入了 ConfigStore 缓存）
    assert plugin._config_store.get()["persona_text"] == rev
    # interest_mgr.regenerate 被触发
    assert plugin.interest_mgr.regenerate.called


def test_llm_autotune_apply_drops_denylist_keys():
    """apply patch 含 DENYLIST 键时被丢弃（不写入）。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 直接传 patch（不经缓存）
    result = asyncio.run(
        plugin.llm_autotune(
            "apply",
            patch={
                "enable": False,  # DENYLIST
                "group_mode": "all",  # DENYLIST
                "base_threshold": 0.6,  # 可写
            },
        )
    )
    assert result["ok"] is True
    assert result["applied"] is True
    # updated 只数可写键（1 项）
    assert result["updated"] == 1
    # DENYLIST 键未写入
    assert plugin._config_store.get()["enable"] is True  # 保持默认 True
    assert plugin._config_store.get()["group_mode"] == "whitelist"  # 保持默认
    # 可写键已写入
    assert plugin._config_store.get()["base_threshold"] == 0.6
    # dropped 字段含被丢弃的键
    assert "enable" in result["dropped"]
    assert "group_mode" in result["dropped"]


# ======================================================================
# 5. llm_autotune 速率限制测试（5 项）
# ======================================================================


def test_llm_autotune_rate_limited_by_cooldown():
    """冷却期内调用 → {ok:False, error:rate_limited, reason:cooldown}。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 3.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 预先 record 一次（_last_call 设为当前时间）
    plugin._tune_limiter.record(time.time())
    # 立即再调 analyze → 被冷却拒绝
    result = asyncio.run(plugin.llm_autotune("analyze", force=False))
    assert result["ok"] is False
    assert result["error"] == "rate_limited"
    assert result["reason"] == "cooldown"
    # rate_limit 状态块附带
    assert "rate_limit" in result
    assert result["rate_limit"]["cooldown_hours"] == 3.0


def test_llm_autotune_rate_limited_by_daily_cap():
    """达到日上限 → {ok:False, error:rate_limited, reason:daily_cap}。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0  # 不限冷却
    cfg["autotune_max_per_day"] = 2
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 预先 record 2 次（填满日上限）
    now = time.time()
    plugin._tune_limiter.record(now)
    plugin._tune_limiter.record(now + 1)
    result = asyncio.run(plugin.llm_autotune("analyze", force=False))
    assert result["ok"] is False
    assert result["error"] == "rate_limited"
    assert result["reason"] == "daily_cap"
    assert result["limit"] == 2


def test_llm_autotune_force_skips_but_records():
    """force=True 跳过 allow 检查直接执行，成功后仍 record 计入配额。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 3.0
    cfg["autotune_max_per_day"] = 4
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=_MockLLMJson(
            {
                "analysis": "强制分析",
                "suggested_patch": {"base_threshold": 0.6},
                "expected_effect": "ok",
            }
        ),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 预先 record 一次（冷却期内）
    plugin._tune_limiter.record(time.time())
    # 非 force → 被拒
    result = asyncio.run(plugin.llm_autotune("analyze", force=False))
    assert result["ok"] is False
    # force → 跳过限制执行
    result2 = asyncio.run(plugin.llm_autotune("analyze", force=True))
    assert result2["ok"] is True
    # force 后仍 record（_last_call 更新到本次）
    assert plugin._tune_limiter._last_call is not None
    # 历史 +1（record 触发）
    used = len(
        [t for t in plugin._tune_limiter._history if t >= time.time() - 86400]
    )
    assert used == 2  # 原 1 次 + force 1 次


def test_llm_autotune_rate_limit_state_restores():
    """TuneRateLimiter.restore 后冷却/日上限计数连续。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 3.0
    cfg["autotune_max_per_day"] = 4
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 模拟从 KV 恢复：之前已 record 2 次（在 24h 内）
    now = time.time()
    plugin._tune_limiter.restore(
        {"history": [now - 100, now - 50], "last_call": now - 50}
    )
    # 调 analyze → 应被 cooldown 拒绝（last_call 距今 50s < 3h）
    result = asyncio.run(plugin.llm_autotune("analyze", force=False))
    assert result["ok"] is False
    assert result["reason"] == "cooldown"
    # 状态恢复后 used 计数正确
    assert result["rate_limit"]["used"] == 2


def test_llm_autotune_allowed_when_passes_and_records():
    """allow 通过 → 执行 analyze + record（计入配额）。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 3.0
    cfg["autotune_max_per_day"] = 4
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=_MockLLMJson(
            {
                "analysis": "通过",
                "suggested_patch": {"base_threshold": 0.5},
                "expected_effect": "效果",
            }
        ),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 从未调用 → allow 通过
    assert plugin._tune_limiter._last_call is None
    result = asyncio.run(plugin.llm_autotune("analyze", force=False))
    assert result["ok"] is True
    # record 被调用
    assert plugin._tune_limiter._last_call is not None
    assert len(plugin._tune_limiter._history) == 1
    # rate_limit 状态块 used=1
    assert result["rate_limit"]["used"] == 1


# ======================================================================
# 6. scheduler 自动触发集成测试（4 项）
# ======================================================================


def test_scheduler_autotune_triggers_on_high_rate(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """rate > autotune_safe_rate_hi 且样本达标 → autotune_trigger_fn 被调用。"""

    async def _run():
        trigger_calls: list[dict] = []

        async def trigger_fn(force: bool = False):
            trigger_calls.append({"called": True, "force": force})
            return {"ok": True}

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            autotune_trigger_fn=trigger_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # 关闭等待窗口：触发回复后不收集同用户后续消息，避免后续 _seed_message
        # 消息被 wait_window 路由吞掉不入 buffer 导致只产生少量决策（test_v0_2_8 TC-032）
        mock_config["wait_window_duration_ms"] = 0
        # 关闭冷却与配额限制：确保 20 条消息全部触发（rate=1.0 > 0.30），
        # 否则 cooldown_messages=4 会让前 4 条 suppressed + max_proactive_per_hour=5
        # 会让后续 suppressed="quota"，实际触发率被压到 < 0.30，无法触发自动调参
        mock_config["cooldown_messages"] = 0
        mock_config["max_proactive_per_hour"] = 0
        mock_config["max_proactive_per_day"] = 0
        # 自动触发开启 + min_decisions 设小（便于 20 条样本就触发）
        mock_config["autotune_auto_trigger_enabled"] = True
        mock_config["autotune_min_decisions"] = 10
        mock_config["autotune_safe_rate_hi"] = 0.30
        mock_config["autotune_safe_rate_lo"] = 0.05
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 喂 20 条触发消息（满一个 EVAL_EVERY 周期，触发率 100% > 0.30）
        for _ in range(20):
            await _seed_message(sched, "g1", "符玄")
            await sched.run_batch("g1")
        # 等待 create_task 调度的 trigger_fn 完成
        await asyncio.sleep(0.1)
        # 第 20 条 record 返回 True → 触发 _maybe_autotune → 后台 task 调 trigger_fn
        assert len(trigger_calls) >= 1

    asyncio.run(_run())


def test_scheduler_autotune_triggers_on_low_rate(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """rate < autotune_safe_rate_lo 且样本达标 → autotune_trigger_fn 被调用。"""

    async def _run():
        trigger_calls: list[dict] = []

        async def trigger_fn(force: bool = False):
            trigger_calls.append({"called": True, "force": force})
            return {"ok": True}

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            autotune_trigger_fn=trigger_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        # 不设 rule_direct_wakeup_words，所有消息都不会触发（triggered=False）
        mock_config["wait_window_duration_ms"] = 0  # 防止 wait_window 吞消息
        mock_config["autotune_auto_trigger_enabled"] = True
        mock_config["autotune_min_decisions"] = 10
        mock_config["autotune_safe_rate_hi"] = 0.30
        mock_config["autotune_safe_rate_lo"] = 0.05
        _set_interest(sched, make_interest_data, centroids={})
        # 喂 20 条普通消息（triggered=False → rate=0 < 0.05）
        for _ in range(20):
            await _seed_message(sched, "g1", "随便说点什么")
            await sched.run_batch("g1")
        await asyncio.sleep(0.1)
        assert len(trigger_calls) >= 1

    asyncio.run(_run())


def test_scheduler_autotune_skipped_when_samples_insufficient(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """样本数 < autotune_min_decisions → 不触发 autotune_trigger_fn。"""

    async def _run():
        trigger_calls: list[dict] = []

        async def trigger_fn(force: bool = False):
            trigger_calls.append({"called": True, "force": force})
            return {"ok": True}

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            autotune_trigger_fn=trigger_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["wait_window_duration_ms"] = 0  # 防止 wait_window 吞消息
        # min_decisions=100，远超 WINDOW=100 但 window_size 最多 100，故此测试用
        # 一个 20 条的场景：window_size=20 < 100 → 不触发
        mock_config["autotune_auto_trigger_enabled"] = True
        mock_config["autotune_min_decisions"] = 100
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        for _ in range(20):
            await _seed_message(sched, "g1", "符玄")
            await sched.run_batch("g1")
        await asyncio.sleep(0.1)
        # 样本不足（20 < 100）→ 不触发
        assert len(trigger_calls) == 0

    asyncio.run(_run())


def test_scheduler_autotune_not_triggered_when_fn_none(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """autotune_trigger_fn=None → 即便条件满足也不触发（既有行为不变）。"""

    async def _run():
        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            autotune_trigger_fn=None,  # 默认 None
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["wait_window_duration_ms"] = 0  # 防止 wait_window 吞消息
        mock_config["autotune_auto_trigger_enabled"] = True
        mock_config["autotune_min_decisions"] = 10
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 不抛异常即通过（既有行为不变）
        for _ in range(20):
            await _seed_message(sched, "g1", "符玄")
            await sched.run_batch("g1")
        # 仍能正常产生决策（wait_window 关闭后 20 条全入 buffer）
        assert len(sched._decision_log) == 20

    asyncio.run(_run())


# ======================================================================
# 7. prompt 全视野测试（3 项）
# ======================================================================


def test_build_tune_prompt_contains_persona_text():
    """_build_tune_prompt 注入 persona_text（人设文本）。"""
    cfg = _default_cfg_for_main()
    cfg["persona_text"] = "你是一个爱聊星穹铁道的群聊机器人。"
    cfg["persona_knowledge"] = "你对量子队配队很了解。"
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    stats = plugin.scheduler.collect_tune_stats()
    prompt = plugin._build_tune_prompt(stats, style="balanced", guidance="")
    # persona_text 注入
    assert "爱聊星穹铁道" in prompt
    # persona_knowledge 注入
    assert "量子队配队" in prompt


def test_build_tune_prompt_contains_interest_items():
    """_build_tune_prompt 注入 export_view items。"""
    cfg = _default_cfg_for_main()
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    # 模拟 interest_mgr 有数据
    plugin.interest_mgr._data = True  # 触发 generated=True 分支
    plugin.interest_mgr.export_view = lambda: {
        "generated": True,
        "persona_hash": "abc123",
        "items": [
            {"label": "core", "topic": "星穹铁道", "examples": ["符玄"], "weight": 1.5},
        ],
        "hate_keywords": ["刷屏"],
        "high_interest_keywords": ["符玄"],
        "rejected": {"examples": [], "keywords": []},
    }
    stats = plugin.scheduler.collect_tune_stats()
    prompt = plugin._build_tune_prompt(stats)
    # export_view items 注入（json 序列化）
    assert "星穹铁道" in prompt
    assert "符玄" in prompt
    assert "刷屏" in prompt


def test_build_tune_prompt_contains_schedule():
    """_build_tune_prompt 注入 schedule。"""
    cfg = _default_cfg_for_main()
    cfg["schedule"] = [
        {"start": "09:00", "end": "12:00"},
        {"start": "20:00", "end": "23:00"},
    ]
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=MagicMock(),
        tmp_data_dir=Path("."),
        mock_log=MagicMock(),
    )
    stats = plugin.scheduler.collect_tune_stats()
    prompt = plugin._build_tune_prompt(stats)
    # schedule 注入
    assert "09:00" in prompt
    assert "23:00" in prompt
    # 也应含 group_mode / group_whitelist
    assert "group_mode" in prompt or "whitelist" in prompt


# ======================================================================
# 8. collect_tune_stats 扩展测试（2 项）
# ======================================================================


def test_collect_tune_stats_contains_adaptive_summary_with_window_rate():
    """collect_tune_stats 返回 adaptive_summary 含 mult/window_rate/samples。"""

    async def _run():
        sched = _make_scheduler(
            mock_config=_default_cfg_for_main(),
            mock_llm=MagicMock(),
            mock_embed=MagicMock(),
            mock_send=MagicMock(),
            mock_kv=MagicMock(),
            mock_log=MagicMock(),
            tmp_data_dir=Path("."),
        )
        # 喂几条决策触发 adaptive 记录
        g = sched._get_group("g1")
        for i in range(10):
            g["adaptive"].record(0.7, i < 4)  # 4/10 triggered
        stats = sched.collect_tune_stats()
        # adaptive_summary 字段存在
        assert "adaptive_summary" in stats
        assert isinstance(stats["adaptive_summary"], list)
        # 至少有一项（g1）
        assert len(stats["adaptive_summary"]) >= 1
        entry = next(
            e for e in stats["adaptive_summary"] if e["group_id"] == "g1"
        )
        assert entry["mult"] == 1.0  # 未满 EVAL_EVERY=20，未评估
        assert abs(entry["window_rate"] - 0.4) < 1e-9
        assert entry["samples"] == 10

    asyncio.run(_run())


def test_collect_tune_stats_config_is_full_snapshot():
    """collect_tune_stats.config 是全量配置快照（v0.2.9 改为 _tune_config_subset 全量）。"""
    from tests.conftest import default_config

    cfg = default_config()
    cfg["base_threshold"] = 0.99  # 非默认值

    async def _run():
        sched = _make_scheduler(
            mock_config=cfg,
            mock_llm=MagicMock(),
            mock_embed=MagicMock(),
            mock_send=MagicMock(),
            mock_kv=MagicMock(),
            mock_log=MagicMock(),
            tmp_data_dir=Path("."),
        )
        stats = sched.collect_tune_stats()
        # config 字段含全量普通键
        assert "base_threshold" in stats["config"]
        assert stats["config"]["base_threshold"] == 0.99
        # v0.2.9 新增配置键也在
        assert "autotune_safe_rate_hi" in stats["config"]
        assert "autotune_cooldown_hours" in stats["config"]
        assert "autotune_max_per_day" in stats["config"]
        # schedule 也在（全量快照，非子集）
        assert "schedule" in stats["config"]
        assert "persona_text" in stats["config"]

    asyncio.run(_run())


# ======================================================================
# 辅助：mock_kv/mock_log 兼容（_make_scheduler 用 MagicMock 时需异步接口）
# ======================================================================


@pytest.fixture
def _async_kv():
    """提供异步 get/set 接口的 mock KV（_make_scheduler 用 MagicMock 时备用）。"""

    class _KV:
        def __init__(self):
            self._d: dict = {}

        async def get(self, key, default=None):
            return self._d.get(key, default)

        async def set(self, key, value):
            self._d[key] = value

    return _KV()
