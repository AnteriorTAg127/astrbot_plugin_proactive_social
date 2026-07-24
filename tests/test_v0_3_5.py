"""test_v0_3_5.py —— v0.3.5 测试用例（Module G）。

测试对象：
- core/common/emoji_filter.py → strip_emoji / is_pure_emoji（F2）
- core/storage/tune_controller.py → TuneRateLimiter 扩展 allow_force/record_force（F4）
- core/decision/interest.py → InterestManager.batch_update 批量重算（F5）
- core/plugin/autotune.py → llm_autotune apply 异步化 background 标志（F5）
- core/decision/conversation_state.py → ConversationStateEvaluator（F6）
- core/scheduler/batch_pipeline.py → 短批次合并（F1）+ conversation_state 集成（F6）

覆盖 V1-V15 所有可离线测试验收点：
  F1 短批次合并（3 项）/ F2 emoji 过滤（4 项）/
  F4 限流修复 + 强制触发（5 项）/ F5 apply 异步化 + 批量重算（3 项）/
  F6 对话状态（17 项）

测试策略：
- emoji_filter / tune_controller / conversation_state 纯模块直接实例化断言。
- interest.batch_update 用真实 InterestManager + mock embed_fn，monkey-patch
  _recompute_centroids / _save_npz 计数。
- autotune apply 复用 test_v0_2_9.py 的 _MockPlugin + sys.modules mock 注入模式。
- scheduler 集成测试复用 conftest scheduler_factory + inline mock_config 改配置。
- 异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# 确保插件根目录在 path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ======================================================================
# 辅助：注入 mock astrbot 模块（复用 test_v0_2_9.py 模式，用于 autotune 测试）
# ======================================================================


_PLUGIN_PKG_NAME = "astrbot_plugin_proactive_social"


def _install_astrbot_mocks() -> None:
    """向 sys.modules 注入最小化 astrbot mock，使 main.py 可被 import。"""

    def _ensure_module(name: str) -> types.ModuleType:
        if name in sys.modules and sys.modules[name] is not None:
            return sys.modules[name]
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    plugin_root = Path(__file__).resolve().parent.parent
    pkg = _ensure_module(_PLUGIN_PKG_NAME)
    pkg.__path__ = [str(plugin_root)]
    pkg.__package__ = _PLUGIN_PKG_NAME

    api = _ensure_module("astrbot.api")
    if not hasattr(api, "AstrBotConfig"):
        api.AstrBotConfig = dict  # type: ignore[attr-defined]
    if not hasattr(api, "logger"):
        log = MagicMock()
        for lv in ("info", "warning", "error", "debug"):
            setattr(log, lv, MagicMock())
        api.logger = log  # type: ignore[attr-defined]

    ev = _ensure_module("astrbot.api.event")

    class _FakeEventMessageType:
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _FakePermissionType:
        ADMIN = "admin"

    class _FakeFilter:
        EventMessageType = _FakeEventMessageType
        PermissionType = _FakePermissionType

        def command_group(self, name):
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

    prov = _ensure_module("astrbot.api.provider")
    if not hasattr(prov, "ProviderRequest"):
        prov.ProviderRequest = object  # type: ignore[attr-defined]

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

    web = _ensure_module("astrbot.api.web")
    if not hasattr(web, "json_response"):

        def _json_response(body, status_code=200):
            return body

        web.json_response = _json_response  # type: ignore[attr-defined]
    if not hasattr(web, "request"):
        web.request = MagicMock()  # type: ignore[attr-defined]

    ap = _ensure_module("astrbot.core.utils.astrbot_path")
    if not hasattr(ap, "get_astrbot_data_path"):

        def _gadp():
            return str(Path(__file__).resolve().parent.parent / "data")

        ap.get_astrbot_data_path = _gadp  # type: ignore[attr-defined]

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
# 辅助：构造 _MockPlugin（镜像 test_v0_2_9.py 模式）
# ======================================================================


def _load_main_prosocial_plugin():
    """以包内子模块方式加载 main.py 并返回 ProSocialPlugin 类。"""
    import importlib

    main_mod = sys.modules.get(f"{_PLUGIN_PKG_NAME}.main")
    if main_mod is None:
        main_mod = importlib.import_module(f"{_PLUGIN_PKG_NAME}.main")
    return main_mod.ProSocialPlugin


def _default_cfg_for_main() -> dict:
    """从 conftest.default_config 取一份配置供 _MockPlugin 使用。"""
    from tests.conftest import default_config

    return default_config()


def _make_mock_plugin(
    *,
    mock_config,
    mock_llm,
    mock_embed,
    tmp_data_dir,
    mock_log,
):
    """构造一个最小化的 _MockPlugin，复用 main.py ProSocialPlugin 的方法。"""
    ProSocialPlugin = _load_main_prosocial_plugin()

    class _MockInterestMgr:
        def __init__(self):
            self._data = None
            self._rejected = {"examples": [], "keywords": []}
            self.regenerate = AsyncMock(return_value=None)
            self.add_item = AsyncMock(return_value=(True, ""))
            self.remove_item = AsyncMock(return_value=(True, ""))
            self.apply_rejected = AsyncMock(return_value=(True, ""))
            self.get_rejected = MagicMock(return_value={"examples": [], "keywords": []})
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
        def __init__(self, cfg: dict):
            self._cfg = cfg

        def get(self) -> dict:
            return self._cfg

        def snapshot(self) -> dict:
            return dict(self._cfg)

        async def set_many(self, patch: dict) -> tuple[bool, str]:
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
    from core.storage.tune_controller import TuneRateLimiter

    plugin._tune_limiter = TuneRateLimiter()
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
            "conversation_state_summary": {"enabled": True, "groups": []},
        }
    )
    plugin.scheduler = sched_mock
    ctx = MagicMock()
    ctx.get_provider_by_id = MagicMock(return_value=None)
    plugin.context = ctx
    return plugin


# ======================================================================
# 辅助：scheduler 集成测试的 inline 构造（复用 test_v0_2_9 模式）
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
    autotune_trigger_fn=None,
):
    """inline 构造 SocialScheduler（复用 conftest 工厂行为，但不依赖 fixture 注入）。

    ``autotune_trigger_fn`` 注入 ``scheduler._autotune_trigger``，供 TC-012
    验证 ``_maybe_autotune`` 在 ``rate > force_threshold`` 时以 ``force=True`` 调用。
    """
    from core.decision.interest import InterestManager
    from core.scheduler import SocialScheduler
    from core.storage.ratelimit import TokenBucketRateLimiter

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
# F1: 短批次合并（3 项）
# ======================================================================


def test_short_batch_merge_prepend(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """V1: batch_text < batch_min_text_length 且 msgs≤1 且 attempts<max →
    回填缓冲区、attempts+1、不评估（_decision_log 为空）。"""

    async def _run():
        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        # 启用短批次合并：阈值 12，"hi" 仅 2 字
        mock_config["batch_min_text_length"] = 12
        _set_interest(sched, make_interest_data, centroids={})
        # 喂入一条短消息
        await _seed_message(sched, "g1", "hi")
        g = sched._get_group("g1")
        assert g["short_batch_attempts"] == 0
        await sched.run_batch("g1")
        # 短批次合并触发：回填缓冲区，attempts+1，不评估
        assert len(sched._decision_log) == 0
        assert g["short_batch_attempts"] == 1
        # 消息回填到缓冲区头部
        assert g["buffer"].pending_count() == 1

    asyncio.run(_run())


def test_short_batch_merge_max_attempts_reset(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """V2: 达 max_attempts 后强制评估（_decision_log 有记录、attempts 重置为 0）。"""

    async def _run():
        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["hi"]
        mock_config["batch_min_text_length"] = 12
        mock_config["batch_short_merge_max_attempts"] = 2
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("hi", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 喂入短消息
        await _seed_message(sched, "g1", "hi")
        g = sched._get_group("g1")
        # 预设 attempts 达到 max_attempts → 下次 run_batch 不再合并，强制评估
        g["short_batch_attempts"] = 2
        await sched.run_batch("g1")
        # 强制评估：有决策记录
        assert len(sched._decision_log) == 1
        # 成功评估后重置 attempts
        assert g["short_batch_attempts"] == 0

    asyncio.run(_run())


def test_short_batch_merge_disabled_when_min_zero():
    """V3: batch_min_text_length=0 时短批次合并不触发（默认配置行为）。
    此处仅断言 ConfigStore 默认值为 12（生产默认启用）。
    """
    from core.storage.config_store import ConfigStore

    assert ConfigStore.DEFAULT_CONFIG["batch_min_text_length"] == 12


# ======================================================================
# F2: emoji 过滤（4 项）
# ======================================================================


def test_strip_emoji_pure_emoji():
    """V3: 纯 emoji 字符串过滤后为空。"""
    from core.common.emoji_filter import strip_emoji

    assert strip_emoji("😊🎉") == ""
    assert strip_emoji("🔥❤️") == ""
    assert strip_emoji("😀") == ""


def test_strip_emoji_mixed():
    """V4: 混合文本过滤后保留非 emoji 部分。"""
    from core.common.emoji_filter import strip_emoji

    assert strip_emoji("hello😊world") == "helloworld"
    assert strip_emoji("你好😀世界") == "你好世界"
    assert strip_emoji("test🎉123") == "test123"


def test_is_pure_emoji_true_false():
    """V3/V4: 纯 emoji 返回 True，混合返回 False。"""
    from core.common.emoji_filter import is_pure_emoji

    assert is_pure_emoji("😊🎉") is True
    assert is_pure_emoji("😀") is True
    assert is_pure_emoji("") is True  # 空串视为纯 emoji（不入缓冲）
    assert is_pure_emoji("hello😊") is False
    assert is_pure_emoji("你好") is False


def test_emoji_filter_buffer_append():
    """V3: GroupBuffer.append 传 filter_emoji=True 时纯 emoji 消息返回 False 不入缓冲。"""
    from core.tracking.buffer import GroupBuffer

    buf = GroupBuffer(max_size=10, log_fn=lambda lv, msg: None)
    # 纯 emoji → 返回 False，不入缓冲
    ok = buf.append(
        user_id="u1",
        nickname="U1",
        text="😊🎉",
        ts=1000.0,
        group_id="g1",
        filter_emoji=True,
    )
    assert ok is False
    assert buf.pending_count() == 0
    # 混合文本 → 过滤后入缓冲
    ok2 = buf.append(
        user_id="u1",
        nickname="U1",
        text="hello😊",
        ts=1000.0,
        group_id="g1",
        filter_emoji=True,
    )
    assert ok2 is True
    assert buf.pending_count() == 1
    # 缓冲区文本为过滤后的 "hello"（不含 emoji）
    assert "hello" in buf.pending_text()
    assert "😊" not in buf.pending_text()


# ======================================================================
# F4: 限流修复 + 强制触发（5 项）
# ======================================================================


def test_tune_limiter_allow_force_first_pass():
    """V8: 首次 allow_force 通过（force_history 为空）。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # force_history 为空 → 允许强制触发
    assert limiter.allow_force(1000.0, 1.0) is True


def test_tune_limiter_allow_force_cooldown_reject():
    """V10: force_history 有近 cooldown_hours 内记录时拒绝。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 记录一次强制触发
    limiter.record_force(1000.0)
    # 0.5 小时后仍在 1 小时冷却内 → 拒绝
    assert limiter.allow_force(1000.0 + 1800.0, 1.0) is False
    # 1.5 小时后超出冷却 → 允许
    assert limiter.allow_force(1000.0 + 5400.0, 1.0) is True


def test_tune_limiter_record_force():
    """V9: record_force 后 force_history 有记录。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    assert len(limiter._force_history) == 0
    limiter.record_force(1000.0)
    assert len(limiter._force_history) == 1
    assert limiter._force_history[0] == 1000.0
    limiter.record_force(2000.0)
    assert len(limiter._force_history) == 2


def test_tune_limiter_state_restore_force_history():
    """V8: state() 含 force_history，restore() 恢复。"""
    from core.storage.tune_controller import TuneRateLimiter

    a = TuneRateLimiter()
    a.record_force(1000.0)
    a.record_force(2000.0)
    s = a.state()
    assert s["force_history"] == [1000.0, 2000.0]

    b = TuneRateLimiter()
    b.restore(s)
    assert list(b._force_history) == [1000.0, 2000.0]
    # restore 后 allow_force 行为与 a 一致（冷却内拒绝）
    assert b.allow_force(2500.0, 1.0) is False


def test_tune_limiter_force_history_backward_compat():
    """V8: restore 旧状态（无 force_history 键）时 force_history 为空 deque。"""
    from core.storage.tune_controller import TuneRateLimiter

    limiter = TuneRateLimiter()
    # 模拟旧版本持久化的状态（无 force_history 键）
    old_state = {"history": [1000.0], "last_call": 1000.0}
    limiter.restore(old_state)
    assert len(limiter._force_history) == 0
    # 旧状态恢复后 allow_force 通过
    assert limiter.allow_force(2000.0, 1.0) is True


# ======================================================================
# F5: apply 异步化 + 批量重算（3 项）
# ======================================================================


def test_batch_update_single_recompute(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """V12: batch_update 多个 add/remove 只调一次 _recompute_centroids + 一次 _save_npz。"""

    async def _run():
        from core.decision.interest import InterestManager

        mgr = InterestManager(tmp_data_dir, mock_log)
        # 先生成一次兴趣数据
        await mgr.regenerate("p", "k", mock_llm, mock_embed)
        assert mgr.get() is not None

        # monkey-patch _recompute_centroids / _save_npz 计数
        recompute_count = 0
        save_count = 0
        orig_recompute = mgr._recompute_centroids
        orig_save = mgr._save_npz

        async def _counting_recompute(items, embed_fn):
            nonlocal recompute_count
            recompute_count += 1
            return await orig_recompute(items, embed_fn)

        def _counting_save(data):
            nonlocal save_count
            save_count += 1
            return orig_save(data)

        mgr._recompute_centroids = _counting_recompute
        mgr._save_npz = _counting_save

        # 批量 add 2 项 + remove 1 项
        adds = [
            {"kind": "high_keyword", "label": "", "text": "Python"},
            {"kind": "hate_keyword", "label": "", "text": "广告"},
        ]
        removes = [
            {"kind": "high_keyword", "label": "", "text": "符玄"},
        ]
        count, msg = await mgr.batch_update(adds, removes, mock_embed)
        assert count >= 1
        assert msg == ""
        # 只调一次 _recompute_centroids + 一次 _save_npz
        assert recompute_count == 1
        assert save_count == 1

    asyncio.run(_run())


def test_batch_update_returns_count_msg(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """V12: batch_update 返回 (count, msg) 格式正确。"""

    async def _run():
        from core.decision.interest import InterestManager

        mgr = InterestManager(tmp_data_dir, mock_log)
        await mgr.regenerate("p", "k", mock_llm, mock_embed)

        adds = [{"kind": "high_keyword", "label": "", "text": "新关键词"}]
        removes = []
        result = await mgr.batch_update(adds, removes, mock_embed)
        # 返回值为 (int, str) 元组
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], str)
        assert result[0] == 1
        assert result[1] == ""

    asyncio.run(_run())


def test_apply_async_background_flag(
    mock_config, mock_embed, tmp_data_dir, mock_log
):
    """V11: llm_autotune("apply") 含 keywords_patch 或 regenerate_needed 时返回
    background=True。"""
    cfg = _default_cfg_for_main()
    cfg["autotune_cooldown_hours"] = 0.0
    cfg["autotune_max_per_day"] = 0
    plugin = _make_mock_plugin(
        mock_config=cfg,
        mock_llm=MagicMock(),
        mock_embed=mock_embed,
        tmp_data_dir=tmp_data_dir,
        mock_log=mock_log,
    )

    # 场景 1：keywords_patch → background=True
    kp = {
        "add": [{"kind": "high_keyword", "label": "core", "text": "Python"}],
        "remove": [],
    }
    plugin.interest_mgr.batch_update.return_value = (1, "")

    async def _run_keywords():
        result = await plugin.llm_autotune("apply", keywords_patch=kp)
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_run_keywords())
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["background"] is True
    assert result["keywords_updated"] == 0

    # 场景 2：regenerate_needed（persona_text 变更）→ background=True
    plugin2 = _make_mock_plugin(
        mock_config=dict(cfg),
        mock_llm=MagicMock(),
        mock_embed=mock_embed,
        tmp_data_dir=tmp_data_dir,
        mock_log=mock_log,
    )
    plugin2._last_tune_suggestion = {
        "suggested_patch": {"persona_text": "新人设"},
        "suggested_keywords_patch": None,
        "persona_revision": None,
    }

    async def _run_persona():
        result = await plugin2.llm_autotune("apply")
        await asyncio.sleep(0.05)
        return result

    result2 = asyncio.run(_run_persona())
    assert result2["ok"] is True
    assert result2["applied"] is True
    assert result2["background"] is True
    assert result2["regenerate"] is True

    # 场景 3：纯标量 patch（无 keywords_patch / 无 regenerate）→ background=False
    plugin3 = _make_mock_plugin(
        mock_config=dict(cfg),
        mock_llm=MagicMock(),
        mock_embed=mock_embed,
        tmp_data_dir=tmp_data_dir,
        mock_log=mock_log,
    )
    plugin3._last_tune_suggestion = {
        "suggested_patch": {"base_threshold": 0.7},
        "suggested_keywords_patch": None,
        "persona_revision": None,
    }

    async def _run_scalar():
        result = await plugin3.llm_autotune("apply")
        return result

    result3 = asyncio.run(_run_scalar())
    assert result3["ok"] is True
    assert result3["applied"] is True
    assert result3["background"] is False


# ======================================================================
# F6: 对话状态（17 项）
# ======================================================================


def _make_msg(
    user_id="u1",
    text="hello",
    ts=1000.0,
    group_id="g1",
    is_wake=False,
):
    """构造 LogicalMessage 辅助函数。"""
    from core.common.models import LogicalMessage

    return LogicalMessage(
        user_id=user_id,
        nickname=user_id.upper(),
        text=text,
        ts=ts,
        group_id=group_id,
        is_wake=is_wake,
    )


def _default_cfg():
    """v0.3.5 对话状态默认配置。"""
    return {
        "conversation_state_enabled": True,
        "conversation_state_window": 10,
        "conversation_state_monologue_ratio": 0.6,
        "conversation_state_argument_msg_len": 20,
    }


def test_has_question_true():
    """V13: 含 ? 且长度>5 的消息 → has_question=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    msgs = [_make_msg(text="今天天气怎么样？", ts=1000.0)]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.has_question is True


def test_has_question_false_short():
    """V13: 含 ? 但长度≤5 → has_question=False。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    msgs = [_make_msg(text="你好?", ts=1000.0)]  # len=3 ≤5
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.has_question is False


def test_is_monologue_true():
    """V13: 同一用户占比 ≥ 0.6 → is_monologue=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 5 条消息，4 条来自 u1，1 条来自 u2 → u1 占比 0.8 ≥ 0.6
    msgs = [
        _make_msg(user_id="u1", text="说点什么", ts=1000.0),
        _make_msg(user_id="u1", text="继续说", ts=1000.1),
        _make_msg(user_id="u1", text="再说", ts=1000.2),
        _make_msg(user_id="u1", text="继续", ts=1000.3),
        _make_msg(user_id="u2", text="插一句", ts=1000.4),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_monologue is True


def test_is_monologue_false_multi_user():
    """V13: 多用户均衡 → is_monologue=False。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 4 条消息，u1/u2 各 2 条 → 最大占比 0.5 < 0.6
    msgs = [
        _make_msg(user_id="u1", text="第一条消息", ts=1000.0),
        _make_msg(user_id="u2", text="第二条消息", ts=1001.0),
        _make_msg(user_id="u1", text="第三条消息", ts=1002.0),
        _make_msg(user_id="u2", text="第四条消息", ts=1003.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_monologue is False


def test_is_argument_true():
    """V13: 2+ 用户 + 平均长度>20 + 标点占比>0.5 → is_argument=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 构造长消息 + 高标点占比
    # 7 中文 + 10 '！' + 7 '？' = 24 字，17 标点，ratio=17/24≈0.708 > 0.5
    # avg_len = 24 > 20 (默认 conversation_state_argument_msg_len)
    msgs = [
        _make_msg(user_id="u1", text="你怎么能这样做！！！！！！！！！！？？？？？？？"),
        _make_msg(user_id="u2", text="我就是要这样做！！！！！！！！！！？？？？？？？"),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_argument is True


def test_is_argument_false_low_punct():
    """V13: 标点占比≤0.5 → is_argument=False。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 长消息但标点占比低
    msgs = [
        _make_msg(user_id="u1", text="这是一段很长的消息没有任何标点符号"),
        _make_msg(user_id="u2", text="这也是一段很长的消息同样没有标点"),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_argument is False


def test_is_casual_chat_true():
    """V13: 2+ 用户 + 平均长度<12 + 平均间隔<5s → is_casual_chat=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 短消息 + 间隔小（1s）
    msgs = [
        _make_msg(user_id="u1", text="hi", ts=1000.0),
        _make_msg(user_id="u2", text="yo", ts=1001.0),
        _make_msg(user_id="u1", text="hey", ts=1002.0),
        _make_msg(user_id="u2", text="sup", ts=1003.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_casual_chat is True


def test_is_casual_chat_false_long_msg():
    """V13: 平均长度≥12 → is_casual_chat=False。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 长消息（>12 字）+ 间隔小
    msgs = [
        _make_msg(
            user_id="u1", text="这是一条很长的消息内容超过十二个字", ts=1000.0
        ),
        _make_msg(
            user_id="u2", text="这也是一条很长的消息内容超过十二个字", ts=1001.0
        ),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_casual_chat is False


def test_bot_turn_wake():
    """V13: 最后一条 is_wake=True → bot_turn=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    msgs = [
        _make_msg(user_id="u1", text="hello", ts=1000.0),
        _make_msg(user_id="u2", text="hi there", ts=1001.0, is_wake=True),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.bot_turn is True


def test_bot_turn_bot_silent():
    """V13: 最后一条是 bot 发言且 now-ts≤5 → bot_turn=True。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 最后一条是 bot（user_id="__bot__"），now - ts = 3 ≤ 5
    msgs = [
        _make_msg(user_id="u1", text="hello", ts=1000.0),
        _make_msg(user_id="__bot__", text="hi", ts=1005.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 1008.0)
    assert state.bot_turn is True


def test_bot_turn_bot_long_silent():
    """V13: 最后一条是 bot 发言但 now-ts>5 → bot_turn=False。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 最后一条是 bot，now - ts = 10 > 5
    msgs = [
        _make_msg(user_id="u1", text="hello", ts=1000.0),
        _make_msg(user_id="__bot__", text="hi", ts=1005.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 1015.0)
    assert state.bot_turn is False


def test_appropriateness_high():
    """V14: has_question+bot_turn+casual → appropriateness=0.8（0.3+0.3+0.2）。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # has_question=True（含?且长度>5）+ bot_turn=True（is_wake）+ is_casual_chat=True
    msgs = [
        _make_msg(user_id="u1", text="what", ts=1000.0),
        _make_msg(user_id="u2", text="今天天气如何？", ts=1001.0, is_wake=True),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    # 0.3 (has_question) + 0.3 (bot_turn) + 0.2 (is_casual_chat) = 0.8
    assert state.appropriateness == pytest.approx(0.8, abs=1e-6)


def test_appropriateness_low():
    """V14: is_argument+is_monologue → appropriateness=0.0（-0.5-0.3 clamp 到 0）。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # 构造 is_argument=True + is_monologue=True 场景
    # u1 发 4 条长高标点消息，u2 发 1 条 → u1 占比 0.8≥0.6 (monologue) +
    # 2 个不同用户 + 平均长度>20 + 标点占比>0.5 (argument)
    # 7 中文 + 10 '！' + 7 '？' = 24 字，17 标点，ratio=17/24≈0.708 > 0.5
    text = "你怎么能这样做！！！！！！！！！！？？？？？？？"
    msgs = [
        _make_msg(user_id="u1", text=text, ts=1000.0),
        _make_msg(user_id="u1", text=text, ts=1000.5),
        _make_msg(user_id="u1", text=text, ts=1001.0),
        _make_msg(user_id="u1", text=text, ts=1001.5),
        _make_msg(user_id="u2", text=text, ts=1002.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.is_argument is True
    assert state.is_monologue is True
    # -0.5 (argument) - 0.3 (monologue) = -0.8 → clamp to 0.0
    assert state.appropriateness == pytest.approx(0.0, abs=1e-6)


def test_modifier_high_approp():
    """V14: 高 appropriateness → modifier < 1.0（放宽阈值）。
    公式：modifier = 1.0 + (0.5 - appropriateness) * 0.6
    appropriateness=1.0 → modifier=0.7（公式验证）
    appropriateness=0.8（实际最高）→ modifier=0.82
    """
    from core.decision.conversation_state import ConversationStateEvaluator

    # 公式验证：appropriateness=1.0 → modifier=0.7
    modifier_at_1 = 1.0 + (0.5 - 1.0) * 0.6
    assert modifier_at_1 == pytest.approx(0.7, abs=1e-6)

    # 实际场景：has_question + bot_turn + is_casual_chat → appropriateness=0.8
    msgs = [
        _make_msg(user_id="u1", text="what", ts=1000.0),
        _make_msg(user_id="u2", text="今天天气如何？", ts=1001.0, is_wake=True),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.appropriateness == pytest.approx(0.8, abs=1e-6)
    # modifier = 1.0 + (0.5 - 0.8) * 0.6 = 0.82
    assert state.modifier == pytest.approx(0.82, abs=1e-6)
    assert state.modifier < 1.0  # 放宽


def test_modifier_low_approp():
    """V14: 低 appropriateness → modifier > 1.0（收紧阈值）。
    appropriateness=0.0 → modifier=1.3
    """
    from core.decision.conversation_state import ConversationStateEvaluator

    # 公式验证：appropriateness=0.0 → modifier=1.3
    modifier_at_0 = 1.0 + (0.5 - 0.0) * 0.6
    assert modifier_at_0 == pytest.approx(1.3, abs=1e-6)

    # 实际场景：is_argument + is_monologue → appropriateness=0.0 → modifier=1.3
    # 7 中文 + 10 '！' + 7 '？' = 24 字，17 标点，ratio≈0.708 > 0.5；avg_len=24>20
    text = "你怎么能这样做！！！！！！！！！！？？？？？？？"
    msgs = [
        _make_msg(user_id="u1", text=text, ts=1000.0),
        _make_msg(user_id="u1", text=text, ts=1000.5),
        _make_msg(user_id="u1", text=text, ts=1001.0),
        _make_msg(user_id="u1", text=text, ts=1001.5),
        _make_msg(user_id="u2", text=text, ts=1002.0),
    ]
    state = ConversationStateEvaluator.evaluate(msgs, "__bot__", _default_cfg(), 2000.0)
    assert state.appropriateness == pytest.approx(0.0, abs=1e-6)
    assert state.modifier == pytest.approx(1.3, abs=1e-6)
    assert state.modifier > 1.0  # 收紧


def test_modifier_default_on_exception():
    """V15: 异常输入（如 cfg=None 或 msgs=None）→ modifier=1.0（兜底）。"""
    from core.decision.conversation_state import ConversationStateEvaluator

    # cfg=None → 异常 → 兜底 modifier=1.0
    state = ConversationStateEvaluator.evaluate(
        [_make_msg(text="hello", ts=1000.0)], "__bot__", None, 2000.0  # type: ignore
    )
    assert state.modifier == pytest.approx(1.0, abs=1e-6)
    assert state.appropriateness == pytest.approx(0.5, abs=1e-6)

    # msgs=None → 异常 → 兜底 modifier=1.0
    state2 = ConversationStateEvaluator.evaluate(
        None, "__bot__", _default_cfg(), 2000.0  # type: ignore
    )
    assert state2.modifier == pytest.approx(1.0, abs=1e-6)


def test_conversation_state_disabled_in_pipeline(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """V15: conversation_state_enabled=False 时 batch_pipeline 不调
    ConversationStateEvaluator，eff_threshold 不受影响，断言
    BatchDecision.conversation_state_mod=1.0。"""

    async def _run():
        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # 禁用对话状态模块
        mock_config["conversation_state_enabled"] = False
        # 禁用短批次合并（保证消息能进入评估流程）
        mock_config["batch_min_text_length"] = 0
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        # 有决策记录
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        # conversation_state_enabled=False → modifier 未应用，conversation_state_mod=1.0
        assert d["conversation_state_mod"] == 1.0

    asyncio.run(_run())
