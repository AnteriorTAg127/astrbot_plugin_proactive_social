"""test_v0_3_7.py —— v0.3.7 回归测试。

测试对象：
- core/plugin/autotune.py → _apply_keywords_patch 结构校验 + 去重（Bug A）
- core/storage/tune_history.py → mark_applied / get_stats applied 统计（Bug B）
- core/scheduler/batch_pipeline.py → run_batch proactive_min_interval 冷却
- core/scheduler/scheduler.py → _cooldown_ratio 时间窗口

覆盖 12 项：
  Bug A _apply_keywords_patch（4 项）/ Bug B mark_applied（3 项）/
  get_stats applied 统计（1 项）/ proactive_min_interval 冷却（2 项）/
  _cooldown_ratio 时间窗口（2 项）

测试策略：
- _apply_keywords_patch：构造最小化 mock TuneMixin 对象（__new__ 跳过 __init__），
  注入 interest_mgr / _embed_fn / _log。
- TuneHistoryStore：tempfile 临时 SQLite 文件，每用例独立。
- proactive_min_interval：conftest scheduler_factory + mock_config/mock_embed/mock_send。
- _cooldown_ratio：SocialScheduler.__new__ 构造裸实例，直接调方法。
- 异步测试统一用 asyncio.run() 包装，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from core.common.models import InterestData, InterestItem, InterestLevel
from core.decision.interest import InterestManager
from core.plugin.autotune import TuneMixin
from core.scheduler.scheduler import SocialScheduler
from core.storage.tune_history import TuneHistoryStore

# 确保插件根目录在 path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------- #
# 辅助函数
# ---------------------------------------------------------------------- #


def _silent_log(level, msg):
    pass


def _make_interest_mgr_with_data():
    """构造一个已加载兴趣数据的 InterestManager（复用 test_v0_3_6 模式）。"""
    tmpdir = Path(tempfile.mkdtemp())
    mgr = InterestManager(tmpdir, _silent_log)
    items = [
        InterestItem(
            level=InterestLevel.CORE,
            topic="核心",
            examples=["你好", "在吗"],
            weight=1.5,
        ),
        InterestItem(
            level=InterestLevel.GENERAL,
            topic="日常",
            examples=["天气", "吃饭"],
            weight=1.0,
        ),
    ]
    mgr._data = InterestData(
        centroids={},
        weights={"core": 1.5, "general": 1.0, "marginal": 0.6, "hate": 1.0},
        high_interest_keywords=["闲聊", "游戏"],
        hate_keywords=["骂人"],
        items=items,
        persona_hash="test",
        dim=0,
    )
    return mgr


def _make_tune_store():
    """构造临时 SQLite TuneHistoryStore（调用方在 asyncio.run 内 close）。"""
    tmpdir = Path(tempfile.mkdtemp())
    return TuneHistoryStore(tmpdir / "test_tune_history.db")


def _make_tune_mixin():
    """构造最小化 mock TuneMixin 对象（__new__ 跳过 __init__）。

    注入 interest_mgr（带数据）/ _embed_fn / _log（静默）。
    """
    mgr = _make_interest_mgr_with_data()

    async def _mock_embed(texts):
        return [[0.1] * 8 for _ in texts]

    obj = TuneMixin.__new__(TuneMixin)
    obj.interest_mgr = mgr
    obj._embed_fn = _mock_embed
    obj._log = _silent_log
    return obj


# scheduler 测试辅助（复用 test_v0_2_8 模式）


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


def _set_interest(
    sched, make_interest_data, centroids=None, high_kw=None, hate_kw=None
):
    """直接设置 interest_mgr._data，避免走 LLM/embed 流程。"""
    sched._interest_mgr._data = make_interest_data(
        centroids=centroids or {},
        high_kw=high_kw or [],
        hate_kw=hate_kw or [],
    )


# ======================================================================
# 1. Bug A: _apply_keywords_patch 结构校验 + 去重（4 项）
# ======================================================================


def test_apply_keywords_patch_dict_text_coerced():
    """Bug A-1: add 项 text 字段是 dict 时，强制 str 转换后加入列表。"""
    mock_obj = _make_tune_mixin()

    async def _run():
        patch = {
            "add": [{"kind": "high_keyword", "text": {"nested": "x"}}],
        }
        await mock_obj._apply_keywords_patch(patch)

    asyncio.run(_run())
    mgr = mock_obj.interest_mgr
    # dict 被 str 转换为 "{'nested': 'x'}" 并加入列表
    coerced = str({"nested": "x"})
    assert coerced in mgr._data.high_interest_keywords
    # 列表中不存在 dict 对象（全部为 str）
    assert all(isinstance(kw, str) for kw in mgr._data.high_interest_keywords)


def test_apply_keywords_patch_cross_dedup():
    """Bug A-2: 同一 (kind, text) 同时在 add 和 remove 中 → 优先 remove，不 add。"""
    mock_obj = _make_tune_mixin()
    mgr = mock_obj.interest_mgr
    assert "闲聊" in mgr._data.high_interest_keywords

    async def _run():
        patch = {
            "add": [{"kind": "high_keyword", "text": "闲聊"}],
            "remove": [{"kind": "high_keyword", "text": "闲聊"}],
        }
        await mock_obj._apply_keywords_patch(patch)

    asyncio.run(_run())
    # 优先 remove：闲聊不在 active 中
    assert "闲聊" not in mgr._data.high_interest_keywords
    # 闲聊加入 _rejected（remove 路径）
    assert any(
        k.get("text") == "闲聊" and k.get("kind") == "high_keyword"
        for k in mgr._rejected["keywords"]
    )


def test_apply_keywords_patch_internal_dedup():
    """Bug A-3: add 列表内部重复同一 (kind, text) → 只加入一次。"""
    mock_obj = _make_tune_mixin()

    async def _run():
        patch = {
            "add": [
                {"kind": "high_keyword", "text": "新词"},
                {"kind": "high_keyword", "text": "新词"},
            ],
        }
        await mock_obj._apply_keywords_patch(patch)

    asyncio.run(_run())
    mgr = mock_obj.interest_mgr
    assert "新词" in mgr._data.high_interest_keywords
    assert mgr._data.high_interest_keywords.count("新词") == 1


def test_apply_keywords_patch_non_dict_item_skipped():
    """Bug A-4: add 列表含非 dict 项（纯字符串）→ 静默跳过，不报错。"""
    mock_obj = _make_tune_mixin()

    async def _run():
        patch = {
            "add": ["hello", {"kind": "high_keyword", "text": "有效词"}],
        }
        await mock_obj._apply_keywords_patch(patch)

    asyncio.run(_run())
    mgr = mock_obj.interest_mgr
    # "hello" 未加入（非 dict 被跳过）
    assert "hello" not in mgr._data.high_interest_keywords
    # "有效词" 正常加入
    assert "有效词" in mgr._data.high_interest_keywords


# ======================================================================
# 2. Bug B: tune_history mark_applied（3 项）
# ======================================================================


def test_mark_applied_finds_analyze():
    """Bug B-1: 先 record analyze applied=False，再 mark_applied → True，记录 applied=True。"""
    store = _make_tune_store()

    async def _run():
        await store.record(
            action="analyze",
            source="manual",
            patch={"base_threshold": 0.6},
            keywords_patch={"add": []},
            persona_revision=None,
            analysis="test",
            expected_effect="effect",
            applied=False,
        )
        marked = await store.mark_applied("manual")
        assert marked is True
        records = await store.list()
        assert len(records) == 1
        assert records[0]["applied"] is True
        await store.close()

    asyncio.run(_run())


def test_mark_applied_no_match():
    """Bug B-2: mark_applied("auto") 在只有 source="manual" 时 → False。"""
    store = _make_tune_store()

    async def _run():
        await store.record(
            action="analyze",
            source="manual",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="",
            expected_effect="",
            applied=False,
        )
        marked = await store.mark_applied("auto")
        assert marked is False
        records = await store.list()
        assert records[0]["applied"] is False
        await store.close()

    asyncio.run(_run())


def test_mark_applied_only_unapplied():
    """Bug B-3: mark_applied 只更新 applied=False 的记录，不动 applied=True 的。"""
    store = _make_tune_store()

    async def _run():
        # 第一条：未应用
        await store.record(
            action="analyze",
            source="manual",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="first",
            expected_effect="",
            applied=False,
        )
        # 第二条：已应用
        await store.record(
            action="analyze",
            source="manual",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="second",
            expected_effect="",
            applied=True,
        )
        # mark_applied 应找到第一条（applied=False）并更新
        marked = await store.mark_applied("manual")
        assert marked is True
        # 再次调用：已无 applied=False 的 analyze 记录 → False
        marked2 = await store.mark_applied("manual")
        assert marked2 is False
        # 两条记录现在都 applied=True
        records = await store.list()
        assert all(r["applied"] is True for r in records)
        await store.close()

    asyncio.run(_run())


# ======================================================================
# 3. tune_history get_stats 统计 applied（1 项）
# ======================================================================


def test_get_stats_counts_applied():
    """get_stats 的 apply_count 统计 applied=1 的记录数（不再依赖 action="apply"）。"""
    store = _make_tune_store()

    async def _run():
        # 2 条 analyze applied=False
        await store.record(
            action="analyze",
            source="manual",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="",
            expected_effect="",
            applied=False,
        )
        await store.record(
            action="analyze",
            source="auto",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="",
            expected_effect="",
            applied=False,
        )
        # 1 条 analyze applied=True
        await store.record(
            action="analyze",
            source="manual",
            patch={},
            keywords_patch=None,
            persona_revision=None,
            analysis="",
            expected_effect="",
            applied=True,
        )
        stats = await store.get_stats()
        assert stats["total"] == 3
        assert stats["analyze_count"] == 3
        assert stats["apply_count"] == 1
        await store.close()

    asyncio.run(_run())


# ======================================================================
# 4. proactive_min_interval 冷却（2 项）
# ======================================================================


def test_proactive_min_interval_suppresses(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """proactive_min_interval=300，last_proactive_ts=now-100 → suppressed_reason="min_interval"。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["proactive_min_interval"] = 300
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        # 设置 100 秒前刚发过主动消息（未过冷却）
        g = sched._get_group("g1")
        g["last_proactive_ts"] = time.time() - 100
        mock_send.calls.clear()
        await sched.run_batch("g1")
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "min_interval"
        assert d["triggered"] is False
        # 未发送
        assert mock_send.call_count == 0

    asyncio.run(_run())


def test_proactive_min_interval_allows_after_expiry(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """proactive_min_interval=300，last_proactive_ts=now-400 → 间隔已过，正常触发。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["proactive_min_interval"] = 300
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        # 设置 400 秒前发过（间隔已过 300 秒冷却）
        g = sched._get_group("g1")
        g["last_proactive_ts"] = time.time() - 400
        mock_send.calls.clear()
        await sched.run_batch("g1")
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        assert d["suppressed_reason"] != "min_interval"
        # 已发送
        assert mock_send.call_count == 1

    asyncio.run(_run())


# ======================================================================
# 5. _cooldown_ratio 时间窗口（2 项）
# ======================================================================


def test_cooldown_ratio_time_window():
    """时间窗口 300 秒内只有 3 条非 bot 消息 → ratio=0.0（旧 bot 消息不算）。"""
    sched = SocialScheduler.__new__(SocialScheduler)
    now = time.time()
    g = {
        "cooldown_window": deque([
            (now - 100, False),  # 3 条 100 秒前，非 bot（窗口内）
            (now - 100, False),
            (now - 100, False),
            (now - 400, True),  # 2 条 400 秒前，bot（窗口外）
            (now - 400, True),
        ])
    }
    cfg = {"cooldown_messages": 4}
    ratio = sched._cooldown_ratio(g, cfg)
    assert ratio == 0.0


def test_cooldown_ratio_fallback_cold_group():
    """冷群：窗口内无消息 → 退化为取最后 N 条兜底 → ratio=0.5。"""
    sched = SocialScheduler.__new__(SocialScheduler)
    now = time.time()
    g = {
        "cooldown_window": deque([
            (now - 600, True),  # 1 bot，600 秒前（窗口外）
            (now - 600, False),  # 1 非 bot，600 秒前（窗口外）
        ])
    }
    cfg = {"cooldown_messages": 4}
    ratio = sched._cooldown_ratio(g, cfg)
    assert ratio == 0.5
