"""test_v0_2_5.py —— v0.2.5 基于回复分词的连续对话匹配测试。

测试对象：
- core/reply_keyword.py → ReplyKeywordCache / ReplyKeywordManager（纯计算模块）
- core/scheduler.py → v0.2.5 三集成点（on_bot_sent 提取 / run_batch 加分+跟踪增强+清除）

覆盖 PRD §8.1 全 11 项验收：
- 单元测试 ReplyKeywordManager：available / extract 基本+空文本+短文本+噪声过滤+top_n 限制
  / match_score 基本+空+归一化 / is_valid_for 过期+目标+空关键词 / jieba 不可用降级
- 集成测试 scheduler（mock ReplyKeywordCache 注入绕过 jieba 不确定性）：
  TC1 基本匹配加分 / TC2 无匹配不加分 / TC3 有效期过期 / TC4 目标限定 /
  TC5 回复后清除 / TC6 连续低分清除 / TC7 干运行日志 / TC8 jieba 不可用 /
  TC9 个人跟踪增强 / TC10 疲劳档位 track / TC11 降级路径加分

测试策略：
- 单元测试用真实 jieba（已装），选用 jieba 分词一致的文本，避免 token 不确定性。
- 集成测试直接注入 ReplyKeywordCache 对象到 g["reply_keyword_cache"]，绕过 jieba
  提取的不确定性，专注验证 scheduler 的集成逻辑（加分/清除/跟踪增强/疲劳档位等）。
- 异步测试统一用 asyncio.run() 包装，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import time

import core.reply_keyword as rk_mod
import pytest
from core.models import TrackerEntry
from core.reply_keyword import ReplyKeywordCache, ReplyKeywordManager

# ReplyKeywordManager.extract 单元测试默认配置
DEFAULT_CFG = {
    "reply_keyword_top_n": 5,
    "reply_keyword_ttl_seconds": 60,
}


# ======================================================================
# 辅助：构造 ReplyKeywordCache（集成测试用，绕过 jieba 提取）
# ======================================================================


def make_rk_cache(
    target_user_id: str,
    keywords_dict: dict[str, float],
    expire_at_offset: float = 60.0,
) -> ReplyKeywordCache:
    """构造 ReplyKeywordCache；expire_at = time.time() + expire_at_offset。

    expire_at_offset 为负数表示已过期（TC3 用）。
    """
    return ReplyKeywordCache(
        target_user_id=target_user_id,
        keywords=dict(keywords_dict),
        expire_at=time.time() + expire_at_offset,
        low_score_streak=0,
    )


# ======================================================================
# 辅助：喂入消息并取消自动调度的批次任务（避免与手动 run_batch 竞争）
# ======================================================================


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
# 单元测试：ReplyKeywordManager（独立模块，真实 jieba）
# ======================================================================


def test_reply_keyword_available():
    """available() 返回 True（requirements.txt 已声明 jieba 且运行时已安装）。"""
    assert ReplyKeywordManager.available() is True


def test_reply_keyword_extract_basic():
    """extract 返回非 None cache；keywords 非空 dict；expire_at = now + ttl；
    target_user_id 与 low_score_streak 正确。"""
    now = 1000.0
    cache = ReplyKeywordManager.extract(
        "量子队当前版本需要银狼来植入弱点",
        "u1",
        now,
        DEFAULT_CFG,
    )
    assert cache is not None
    assert isinstance(cache.keywords, dict)
    assert len(cache.keywords) > 0
    assert cache.target_user_id == "u1"
    assert cache.expire_at == 1060.0  # now + ttl_seconds(60)
    assert cache.low_score_streak == 0


def test_reply_keyword_extract_empty_text():
    """extract 对空文本或空 target_user_id 返回 None。"""
    assert ReplyKeywordManager.extract("", "u1", 0, {}) is None
    assert ReplyKeywordManager.extract("text", "", 0, {}) is None


def test_reply_keyword_extract_short_text_fallback():
    """短文本 extract 返回非空 keywords（含"你好"或"世界"）。"""
    cache = ReplyKeywordManager.extract("你好世界", "u1", 0, DEFAULT_CFG)
    assert cache is not None
    assert len(cache.keywords) > 0
    assert "你好" in cache.keywords or "世界" in cache.keywords


def test_reply_keyword_extract_filter_noise():
    """纯噪声（数字+标点）被过滤后 extract 返回 None。"""
    cache = ReplyKeywordManager.extract("123 !!! 。。。", "u1", 0, DEFAULT_CFG)
    assert cache is None


def test_reply_keyword_extract_top_n_limit():
    """extract 返回的 keywords 数量 <= top_n。"""
    long_text = (
        "量子队当前版本需要银狼来植入弱点，符玄的搭配也很重要，"
        "建议用量子战意作为主输出，配合银狼的能力进行弱点突破，"
        "看版本强势阵容推荐符玄量子队"
    )
    cache = ReplyKeywordManager.extract(
        long_text,
        "u1",
        0,
        {"reply_keyword_top_n": 3, "reply_keyword_ttl_seconds": 60},
    )
    assert cache is not None
    assert len(cache.keywords) <= 3


def test_reply_keyword_match_score_basic():
    """match_score：用户消息含"银狼"→ >0；无关消息 → 0.0。"""
    keywords = {"银狼": 1.0, "弱点": 0.5}
    # jieba.lcut("银狼怎么培养") = ["银狼", "怎么", "培养"]，含"银狼"
    assert ReplyKeywordManager.match_score("银狼怎么培养", keywords) > 0
    # jieba.lcut("今天天气不错") 不含"银狼"或"弱点"
    assert ReplyKeywordManager.match_score("今天天气不错", keywords) == 0.0


def test_reply_keyword_match_score_empty():
    """match_score 对空输入返回 0.0。"""
    assert ReplyKeywordManager.match_score("", {"a": 1.0}) == 0.0
    assert ReplyKeywordManager.match_score("text", {}) == 0.0


def test_reply_keyword_match_score_normalized():
    """match_score = 命中权重 / 总权重（归一化占比）。

    keywords = {"a": 1.0, "b": 0.5}（总 1.5）；
    user_text="b" → lcut=["b"]，命中"b"（权重 0.5）→ 0.5/1.5 ≈ 0.333。
    """
    keywords = {"a": 1.0, "b": 0.5}
    score = ReplyKeywordManager.match_score("b", keywords)
    assert score == pytest.approx(0.5 / 1.5, abs=0.001)


def test_reply_keyword_cache_is_valid_for():
    """is_valid_for 检查目标用户 + 未过期 + 关键词非空。"""
    now = 1000.0
    cache = ReplyKeywordCache(
        target_user_id="u1",
        keywords={"银狼": 1.0},
        expire_at=now + 60,
        low_score_streak=0,
    )
    # 有效：目标匹配 + 未过期 + 关键词非空
    assert cache.is_valid_for("u1", now) is True
    # 目标不匹配
    assert cache.is_valid_for("u2", now) is False
    # 已过期
    assert cache.is_valid_for("u1", now + 100) is False
    # 关键词为空 → 无效
    empty_cache = ReplyKeywordCache(
        target_user_id="u1",
        keywords={},
        expire_at=now + 60,
    )
    assert empty_cache.is_valid_for("u1", now) is False


def test_reply_keyword_jieba_unavailable(monkeypatch):
    """monkeypatch _JIEBA_AVAILABLE=False：available()=False、extract=None、match_score=0。"""
    monkeypatch.setattr(rk_mod, "_JIEBA_AVAILABLE", False)
    assert ReplyKeywordManager.available() is False
    assert ReplyKeywordManager.extract("你好世界", "u1", 0, DEFAULT_CFG) is None
    assert ReplyKeywordManager.match_score("你好世界", {"你好": 1.0}) == 0.0


# ======================================================================
# 集成测试：scheduler（mock ReplyKeywordCache 注入，绕过 jieba 不确定性）
# ======================================================================


def test_tc1_basic_match_boost(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """TC1：cache keywords 命中用户消息 → keyword_match_score > 0、added_score > 0。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0  # 高阈值 → 不触发发送，聚焦验证加分
        mock_config["reply_keyword_enabled"] = True
        mock_config["reply_keyword_boost_factor"] = 0.3
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_a")
        # 注入 cache：target=user_a，keywords 含"银狼"
        g = sched._get_group("g1")
        g["reply_keyword_cache"] = make_rk_cache("user_a", {"银狼": 1.0, "弱点": 0.5})
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        # match_score：lcut("银狼怎么培养")含"银狼" → 命中 1.0 / 总 1.5 ≈ 0.667
        assert d["keyword_match_score"] > 0
        assert d["keyword_added_score"] > 0
        # added = match_score * boost_factor(0.3)
        assert d["keyword_added_score"] == pytest.approx(
            d["keyword_match_score"] * 0.3, abs=0.001
        )
        # 未触发发送（高阈值）
        assert mock_send.call_count == 0

    asyncio.run(_run())


def test_tc2_no_match_no_boost(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """TC2：用户消息与 keywords 无重叠 → match_score ≈ 0、added_score ≈ 0。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("今天天气不错", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "今天天气不错", user_id="user_a")
        g = sched._get_group("g1")
        g["reply_keyword_cache"] = make_rk_cache("user_a", {"银狼": 1.0})
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        assert d["keyword_match_score"] < 0.01
        assert d["keyword_added_score"] < 0.01

    asyncio.run(_run())


def test_tc3_expired_cache(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """TC3：cache 已过期（expire_at_offset=-10）→ is_valid_for False → 不加分。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_a")
        g = sched._get_group("g1")
        # expire_at_offset=-10 → 已过期
        g["reply_keyword_cache"] = make_rk_cache(
            "user_a", {"银狼": 1.0}, expire_at_offset=-10
        )
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        assert d["keyword_match_score"] == 0.0
        assert d["keyword_added_score"] == 0.0

    asyncio.run(_run())


def test_tc4_target_user_restriction(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """TC4：target_user_id=user_a，发送者 user_b → 不匹配，不加分。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        # 发送者是 user_b，cache target 是 user_a
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_b")
        g = sched._get_group("g1")
        g["reply_keyword_cache"] = make_rk_cache("user_a", {"银狼": 1.0})
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        assert d["keyword_match_score"] == 0.0
        assert d["keyword_added_score"] == 0.0

    asyncio.run(_run())


def test_tc5_cache_cleared_after_keyword_trigger(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    make_interest_data,
    monkeypatch,
):
    """TC5：keyword_triggered=True 触发回复后，g["reply_keyword_cache"] 被清除为 None。

    用集成点 2（个人跟踪增强）触发 keyword_triggered；
    mock ReplyKeywordManager.extract 返回 None，避免 on_bot_sent 重建缓存，
    可验证清除后真的为 None。
    """
    # mock extract → on_bot_sent 不重建缓存
    monkeypatch.setattr(
        ReplyKeywordManager, "extract", staticmethod(lambda *a, **kw: None)
    )

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0  # 高阈值 → 不走向量触发
        mock_config["personal_threshold"] = 0.9  # sim=0 < 0.9 → 不走 sim 触发
        mock_config["reply_keyword_min_score_to_trigger"] = 0.4
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_tracked")
        g = sched._get_group("g1")
        # 跟踪条目：bot_last_emb 与 batch_emb 正交 → sim=0 < personal_threshold
        g["tracker"].add(
            TrackerEntry(
                user_id="user_tracked",
                nickname="Tracked",
                bot_last_emb=[0.0, 1.0, 0, 0, 0, 0, 0, 0],
                last_own_text="hi",
                created_ts=time.time(),
            )
        )
        # cache：keywords 命中用户消息 → match_score >= min_score_to_trigger
        g["reply_keyword_cache"] = make_rk_cache("user_tracked", {"银狼": 1.0})
        await sched.run_batch("g1")
        # personal_triggered via keyword → triggered=True → 发送 → on_bot_sent
        assert mock_send.call_count == 1
        # keyword_triggered=True → 清除 cache；extract mocked None → 不重建
        assert g["reply_keyword_cache"] is None

    asyncio.run(_run())


def test_tc6_consecutive_low_score_clear(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """TC6：目标用户连续 2 条消息 match_score < 0.1 → low_score_streak 达 2 → 清除。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = (
            2.0  # 高阈值 → 不触发发送，cache 不被 on_bot_sent 重建
        )
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("无关消息 AAA", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "无关消息 AAA", user_id="user_a")
        g = sched._get_group("g1")
        cache = make_rk_cache("user_a", {"银狼": 1.0})
        g["reply_keyword_cache"] = cache
        # 第一次低分：match_score=0 → streak=1，未清除
        await sched.run_batch("g1")
        assert cache.low_score_streak == 1
        assert g["reply_keyword_cache"] is cache
        # 第二次低分：match_score=0 → streak=2 → 清除
        mock_embed.set("另一条无关 BBB", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "另一条无关 BBB", user_id="user_a")
        await sched.run_batch("g1")
        assert cache.low_score_streak == 2
        assert g["reply_keyword_cache"] is None

    asyncio.run(_run())


def test_tc7_dry_run_log(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    mock_log,
    make_interest_data,
):
    """TC7：dry_run=True → run_batch 含 "reply_keyword" debug 日志（含 keywords/match/added/final）。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        mock_config["dry_run"] = True
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_a")
        g = sched._get_group("g1")
        g["reply_keyword_cache"] = make_rk_cache("user_a", {"银狼": 1.0})
        await sched.run_batch("g1")
        # dry_run 不阻止集成点 1 加分日志（加分在 triggered 判定之前执行）
        assert mock_log.has("debug", "reply_keyword")

    asyncio.run(_run())


def test_tc8_jieba_unavailable(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    mock_log,
    make_interest_data,
    monkeypatch,
):
    """TC8：jieba 不可用 → on_bot_sent 警告一次、cache 为 None、scheduler 不崩溃。"""
    monkeypatch.setattr(rk_mod, "_JIEBA_AVAILABLE", False)

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_embed.set("bot reply", [1.0, 0, 0, 0, 0, 0, 0, 0])
        # 先 seed 一条用户消息（on_bot_sent 提取需要 recent_speakers 非空）
        await _seed_message(sched, "g1", "hi", user_id="u1")
        mock_log.reset()
        # 第一次 on_bot_sent：available()=False → 警告一次，不提取
        await sched.on_bot_sent(
            group_id="g1",
            text="bot reply",
            ts=time.time(),
            reply_type="passive",
        )
        g = sched._get_group("g1")
        assert g["reply_keyword_cache"] is None
        assert mock_log.has("warning", "jieba 未安装")
        warning_count = sum(
            1 for lv, msg in mock_log.calls if lv == "warning" and "jieba 未安装" in msg
        )
        assert warning_count == 1
        # 第二次 on_bot_sent：_rk_unavailable_warned=True → 不再警告
        mock_log.reset()
        await sched.on_bot_sent(
            group_id="g1",
            text="bot reply 2",
            ts=time.time() + 10,
            reply_type="passive",
        )
        assert not mock_log.has("warning", "jieba 未安装")

    asyncio.run(_run())


def test_tc9_personal_tracker_keyword_trigger(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    make_interest_data,
    monkeypatch,
):
    """TC9：sim < personal_threshold 但 keyword match >= min → personal_triggered=True。

    集成点 2：向量相似度不足时转用关键词匹配作为强信号直接触发。
    mock extract 返回 None 以验证触发后 cache 被清除（不重建）。
    """
    monkeypatch.setattr(
        ReplyKeywordManager, "extract", staticmethod(lambda *a, **kw: None)
    )

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0  # 高阈值 → 不走向量触发
        mock_config["personal_threshold"] = 0.9  # sim=0 < 0.9 → 不走 sim 触发
        mock_config["reply_keyword_min_score_to_trigger"] = 0.4
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_tracked")
        g = sched._get_group("g1")
        # 跟踪条目：bot_last_emb 与 batch_emb 正交 → sim=0 < personal_threshold
        g["tracker"].add(
            TrackerEntry(
                user_id="user_tracked",
                nickname="Tracked",
                bot_last_emb=[0.0, 1.0, 0, 0, 0, 0, 0, 0],
                last_own_text="hi",
                created_ts=time.time(),
            )
        )
        # cache：keywords 命中用户消息 → match_score(1.0) >= 0.4
        g["reply_keyword_cache"] = make_rk_cache("user_tracked", {"银狼": 1.0})
        await sched.run_batch("g1")
        # personal_triggered via keyword → triggered=True → 发送
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        # keyword_triggered=True → 清除 cache；extract mocked None → 不重建
        assert g["reply_keyword_cache"] is None

    asyncio.run(_run())


def test_tc10_fatigue_track_cost(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    make_interest_data,
    monkeypatch,
):
    """TC10：keyword 触发的回复使用 track 档位（fatigue_cost_track=0.6）消耗疲劳。"""
    monkeypatch.setattr(
        ReplyKeywordManager, "extract", staticmethod(lambda *a, **kw: None)
    )

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        mock_config["personal_threshold"] = 0.9
        mock_config["reply_keyword_min_score_to_trigger"] = 0.4
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("银狼怎么培养", [1.0, 0, 0, 0, 0, 0, 0, 0])
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_tracked")
        g = sched._get_group("g1")
        g["tracker"].add(
            TrackerEntry(
                user_id="user_tracked",
                nickname="Tracked",
                bot_last_emb=[0.0, 1.0, 0, 0, 0, 0, 0, 0],
                last_own_text="hi",
                created_ts=time.time(),
            )
        )
        g["reply_keyword_cache"] = make_rk_cache("user_tracked", {"银狼": 1.0})
        fatigue_before = sched._fatigue.snapshot()["value"]
        await sched.run_batch("g1")
        # triggered=True → send → on_bot_sent(reply_type="track") → consume 0.6
        assert mock_send.call_count == 1
        fatigue_after = sched._fatigue.snapshot()["value"]
        # fatigue_cost_track 默认 0.6（衰减在毫秒级可忽略）
        assert fatigue_after - fatigue_before == pytest.approx(0.6, abs=0.01)

    asyncio.run(_run())


def test_tc11_degraded_path_boost(
    scheduler_factory,
    mock_config,
    mock_embed,
    mock_send,
    make_interest_data,
):
    """TC11：batch_emb=None（降级路径）时集成点 1 仍执行加分。

    降级路径 fusion.final_score 初始为 score_b=0，加 added 后 > 0。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 2.0
        _set_interest(sched, make_interest_data, centroids={})
        # 嵌入失败 → batch_emb=None → 走降级路径
        mock_embed.set_fail_mode(True)
        await _seed_message(sched, "g1", "银狼怎么培养", user_id="user_a")
        g = sched._get_group("g1")
        g["reply_keyword_cache"] = make_rk_cache("user_a", {"银狼": 1.0})
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        # 降级路径仍执行加分
        assert d["keyword_match_score"] > 0
        assert d["keyword_added_score"] > 0
        # final_score = score_b(0) + added > 0
        assert d["score"] > 0

    asyncio.run(_run())
