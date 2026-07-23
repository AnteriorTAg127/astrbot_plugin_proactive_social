"""test_v0_2_8.py —— v0.2.8 测试用例（Module F）。

测试对象：
- core/adaptive.py → AdaptiveThreshold / SendQuota（纯计算，无 I/O 依赖）
- core/models.py → BatchDecision.adaptive_mult 默认值
- core/metrics.py → _deserialize_decision 反序列化 adaptive_mult
- core/config_store.py → 4 个新配置键的默认值与 VALIDATORS
- core/interest.py → _compute_persona_hash 纳入 example_count/keyword_count
- core/scheduler.py → _dispatch_proactive / eff_threshold / 配额检查 /
  adaptive_mult 写入决策 / collect_tune_stats

覆盖 PRD §6 验收 #2/#3/#5 与分工.md Module F 全部 8 个测试范围。
不依赖 AstrBot 运行时，全部离线 pytest。异步测试统一用 asyncio.run() 包装。

注：post_autotune handler 测试已由 agent-e 在 test_web.py 中实现
（test_web_post_autotune_*，5 项 + handler 计数 11→12），本模块不重复。
"""

from __future__ import annotations

import asyncio
import time

from core.adaptive import AdaptiveThreshold, SendQuota
from core.config_store import ConfigStore
from core.interest import _compute_persona_hash
from core.metrics import _deserialize_decision
from core.models import BatchDecision, ScoreFactors

# ======================================================================
# 辅助：构造 SocialScheduler（注入自定义 inject_fn，绕过 conftest 的工厂）
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
):
    """inline 构造 SocialScheduler，支持传入 inject_fn（conftest 工厂未暴露此参数）。

    与 conftest.scheduler_factory 行为一致：模拟 start() 的预加载
    （group_enable_cache={}）但不真正 start()。
    """
    from core.interest import InterestManager
    from core.ratelimit import TokenBucketRateLimiter
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
# 1. AdaptiveThreshold 测试（9 项）
# ======================================================================


def test_adaptive_initial_mult():
    """初始 mult=1.0。"""
    a = AdaptiveThreshold()
    assert a.multiplier() == 1.0


def test_adaptive_high_trigger_rate_increases_mult():
    """高触发率（>30%，20 条 triggered=True）→ mult 上升 ×1.1。"""
    a = AdaptiveThreshold()
    for _ in range(20):
        a.record(0.8, True)
    assert a.multiplier() == 1.1  # 1.0 * 1.1


def test_adaptive_low_trigger_rate_decreases_mult():
    """低触发率（<5%，20 条 triggered=False）→ mult 下降 ×0.9。"""
    a = AdaptiveThreshold()
    for _ in range(20):
        a.record(0.3, False)
    assert a.multiplier() == 0.9  # 1.0 * 0.9


def test_adaptive_mid_trigger_rate_no_change():
    """区间内（5%-30%）不动。20 条中 2 条触发 = 10%，在 5%-30% 区间。"""
    a = AdaptiveThreshold()
    for i in range(20):
        a.record(0.6, i < 2)
    assert a.multiplier() == 1.0


def test_adaptive_clamp_max():
    """clamp [0.5, 2.0]：多轮高触发率，mult 应被钳制在 2.0。"""
    a = AdaptiveThreshold()
    # 每轮 EVAL_EVERY=20 条 triggered=True → +1.1；从 1.0 起 8 轮达 2.0 钳制
    # 200 条 = 10 轮评估，足够触达上限
    for _ in range(200):
        a.record(0.9, True)
    assert a.multiplier() == 2.0


def test_adaptive_clamp_min():
    """clamp [0.5, 2.0]：多轮低触发率，mult 应被钳制在 0.5。"""
    a = AdaptiveThreshold()
    # 每轮 EVAL_EVERY=20 条 triggered=False → ×0.9；从 1.0 起 7 轮达 0.5 钳制
    for _ in range(200):
        a.record(0.2, False)
    assert a.multiplier() == 0.5


def test_adaptive_window_truncation():
    """窗口截断 100：内部 deque maxlen=100，喂 150 条不报错且长度=100。"""
    a = AdaptiveThreshold()
    for _ in range(150):
        a.record(0.5, True)
    assert len(a._scores) == 100
    assert len(a._triggered) == 100


def test_adaptive_state_restore():
    """state/restore 往返：恢复后 mult 与 _since_eval 一致。"""
    a = AdaptiveThreshold()
    for _ in range(20):
        a.record(0.8, True)
    s = a.state()
    assert s == {"mult": 1.1, "since_eval": 0}

    b = AdaptiveThreshold()
    b.restore(s)
    assert b.multiplier() == a.multiplier()
    assert b._since_eval == a._since_eval


def test_adaptive_eval_every_pacing():
    """EVAL_EVERY 步进节奏：19 条不评估，20 条评估。"""
    a = AdaptiveThreshold()
    for _ in range(19):
        a.record(0.8, True)
    assert a.multiplier() == 1.0  # 未满 20 不评估
    a.record(0.8, True)  # 第 20 条触发评估
    assert a.multiplier() == 1.1


def test_adaptive_restore_invalid():
    """restore 容错：非 dict / 越界值不应用，不抛错。"""
    a = AdaptiveThreshold()
    a.restore(None)  # 不抛错
    a.restore({"mult": 5.0, "since_eval": -1})  # 越界不应用
    assert a.multiplier() == 1.0
    assert a._since_eval == 0


# ======================================================================
# 2. SendQuota 测试（5 项）
# ======================================================================


def test_quota_per_hour_exceeded():
    """per_hour 超限返回 False。"""
    q = SendQuota()
    now = 1000.0
    for _ in range(5):
        q.record(now)
    # 第 6 条被 per_hour=5 拒绝
    assert q.check(now, 5, 0) is False


def test_quota_per_day_exceeded():
    """per_day 超限返回 False。"""
    q = SendQuota()
    now = 1000.0
    for _ in range(20):
        q.record(now)
    assert q.check(now, 0, 20) is False


def test_quota_zero_means_unlimited():
    """0=不限：per_hour=0 且 per_day=0 时永远 True。"""
    q = SendQuota()
    now = 1000.0
    for _ in range(100):
        q.record(now)
    assert q.check(now, 0, 0) is True


def test_quota_hour_window_expiry():
    """record 后滑动窗口过期恢复：1 小时后旧记录过期，check 通过。"""
    q = SendQuota()
    now = 1000.0
    for _ in range(5):
        q.record(now)
    assert q.check(now, 5, 0) is False
    # 1 小时后旧记录过期（cutoff_hour = now+3601-3600 = now+1 > 旧记录 ts=now）
    assert q.check(now + 3601, 5, 0) is True


def test_quota_check_does_not_record():
    """check 不清记录、record 才记。"""
    q = SendQuota()
    now = 1000.0
    assert q.check(now, 5, 0) is True
    assert len(q._ts) == 0  # check 不记录
    q.record(now)
    assert len(q._ts) == 1


# ======================================================================
# 3. models/metrics 测试（2 项）
# ======================================================================


def test_batch_decision_adaptive_mult_default():
    """BatchDecision.adaptive_mult 默认 1.0。"""
    d = BatchDecision(
        ts=1000.0,
        group_id="g1",
        batch_summary="hi",
        factors=ScoreFactors(0.0, 0.0, 0.0, 0.0, 0.0),
        score=0.7,
        threshold=0.55,
        hit_level="none",
        triggered=False,
        suppressed_reason="",
        dry_run=False,
        message_count=1,
    )
    assert d.adaptive_mult == 1.0


def test_deserialize_decision_with_adaptive_mult():
    """_deserialize_decision 往返含 adaptive_mult：1.3 正确还原。"""
    raw = {
        "ts": 1000.0,
        "group_id": "g1",
        "batch_summary": "hello",
        "factors": {
            "s_int": 0.5,
            "s_topic": 0.4,
            "s_resp": 0.3,
            "c_cooldown": 0.2,
            "p_silence": 0.1,
        },
        "score": 0.7,
        "threshold": 0.55,
        "hit_level": "core",
        "triggered": True,
        "suppressed_reason": "",
        "dry_run": False,
        "message_count": 3,
        "score_a": 0.6,
        "score_b": 0.75,
        "alpha": 0.4,
        "fatigue_level": "low",
        "fatigue_value": 1.2,
        "channel": "fusion",
        "keyword_match_score": 0.2,
        "keyword_added_score": 0.05,
        "embedding_degraded": False,
        "adaptive_mult": 1.3,
    }
    d = _deserialize_decision(raw)
    assert d is not None
    assert d.adaptive_mult == 1.3
    # 兼容性：缺失 adaptive_mult 字段时回退默认 1.0
    raw2 = dict(raw)
    raw2.pop("adaptive_mult")
    d2 = _deserialize_decision(raw2)
    assert d2 is not None
    assert d2.adaptive_mult == 1.0


# ======================================================================
# 4. config_store 测试（4 项）
# ======================================================================


def test_config_store_v028_defaults():
    """4 新键默认值。"""
    assert ConfigStore.DEFAULT_CONFIG["reply_via_pipeline"] is True
    assert ConfigStore.DEFAULT_CONFIG["adaptive_threshold_enabled"] is True
    assert ConfigStore.DEFAULT_CONFIG["max_proactive_per_hour"] == 5
    assert ConfigStore.DEFAULT_CONFIG["max_proactive_per_day"] == 20


def test_config_store_v028_validators():
    """VALIDATORS 范围。"""
    v = ConfigStore.VALIDATORS
    assert v["reply_via_pipeline"] == (bool, None, None)
    assert v["adaptive_threshold_enabled"] == (bool, None, None)
    assert v["max_proactive_per_hour"] == (int, 0, 200)
    assert v["max_proactive_per_day"] == (int, 0, 500)


def test_config_store_v028_validator_rejects_out_of_range(tmp_data_dir):
    """越界值校验失败：max_proactive_per_hour=300 超出 [0,200] 被拒。"""

    async def _run():
        store = ConfigStore(tmp_data_dir / "config.db")
        ok, msg = await store.set_many({"max_proactive_per_hour": 300})
        assert ok is False
        assert "超出范围" in msg or "max_proactive_per_hour" in msg
        # 缓存未被修改
        assert store.get()["max_proactive_per_hour"] == 5
        # max_proactive_per_day=501 超出 [0,500]
        ok2, msg2 = await store.set_many({"max_proactive_per_day": 501})
        assert ok2 is False
        assert "超出范围" in msg2 or "max_proactive_per_day" in msg2
        await store.close()

    asyncio.run(_run())


def test_config_store_v028_validator_rejects_wrong_type(tmp_data_dir):
    """类型错误校验失败：max_proactive_per_hour="abc" 被拒。"""

    async def _run():
        store = ConfigStore(tmp_data_dir / "config.db")
        ok, msg = await store.set_many({"max_proactive_per_hour": "abc"})
        assert ok is False
        assert "整数" in msg or "max_proactive_per_hour" in msg
        # reply_via_pipeline 必须是 bool，传字符串被拒
        ok2, msg2 = await store.set_many({"reply_via_pipeline": "yes"})
        assert ok2 is False
        assert "布尔" in msg2 or "reply_via_pipeline" in msg2
        # adaptive_threshold_enabled 用 int（1）也应被拒（bool 排除 int）
        ok3, msg3 = await store.set_many({"adaptive_threshold_enabled": 1})
        assert ok3 is False
        assert "布尔" in msg3 or "adaptive_threshold_enabled" in msg3
        await store.close()

    asyncio.run(_run())


# ======================================================================
# 5. interest hash 测试（3 项）
# ======================================================================


def test_persona_hash_differs_by_example_count():
    """同文本不同 example_count → 不同 hash。"""
    h1 = _compute_persona_hash("text", "knowledge", example_count=3, keyword_count=12)
    h2 = _compute_persona_hash("text", "knowledge", example_count=5, keyword_count=12)
    assert h1 != h2


def test_persona_hash_differs_by_keyword_count():
    """同文本不同 keyword_count → 不同 hash。"""
    h1 = _compute_persona_hash("text", "knowledge", example_count=3, keyword_count=12)
    h2 = _compute_persona_hash("text", "knowledge", example_count=3, keyword_count=20)
    assert h1 != h2


def test_persona_hash_stable_and_default_compatible():
    """同参数多次调用一致；默认参数 == 显式传 (3, 12)。"""
    h1 = _compute_persona_hash("text", "knowledge")
    h2 = _compute_persona_hash("text", "knowledge")
    assert h1 == h2
    # 默认参数与显式 (3, 12) 等价
    h3 = _compute_persona_hash("text", "knowledge", 3, 12)
    assert h1 == h3
    # 不同人设文本 → 不同 hash
    h4 = _compute_persona_hash("other", "knowledge")
    assert h1 != h4


# ======================================================================
# 6. scheduler 集成测试（8 项）
# ======================================================================


def test_scheduler_run_batch_has_adaptive_mult_field(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """inject_fn=None 时 run_batch 行为不变，决策含 adaptive_mult 字段（默认 1.0）。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        # v0.2.8 新增字段 adaptive_mult 存在且为默认 1.0
        assert "adaptive_mult" in d
        assert d["adaptive_mult"] == 1.0

    asyncio.run(_run())


def test_scheduler_inject_path_skips_llm_and_send(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """注入 mock（inject_fn 返回 True）→ 不调 llm_fn/send_fn、计数 proactive_sends、quota.record 生效。"""

    async def _run():
        inject_calls: list[tuple] = []

        async def inject_fn(umo, text, hint, group_id, sender_id=""):
            inject_calls.append((umo, text, hint, group_id, sender_id))
            return True

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            inject_fn=inject_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        mock_llm.calls.clear()
        mock_send.calls.clear()
        await sched.run_batch("g1")
        # 触发了主动回复
        assert len(inject_calls) == 1
        # 注入路径不调 llm_fn / send_fn
        assert mock_llm.call_count == 0
        assert mock_send.call_count == 0
        # 计数 proactive_sends（metrics snapshot）
        snap = sched._metrics.snapshot()
        assert snap["proactive_sends"] == 1
        assert snap["proactive_triggered"] == 1
        # quota.record 生效（_ts 非空）
        g = sched._get_group("g1")
        assert len(g["quota"]._ts) == 1

    asyncio.run(_run())


def test_scheduler_inject_failure_falls_back(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """注入失败 → 降级走旧路径（调 llm_fn + send_fn）。"""

    async def _run():
        inject_calls: list[tuple] = []

        async def inject_fn(umo, text, hint, group_id, sender_id=""):
            inject_calls.append((umo, text, hint, group_id, sender_id))
            return False  # 注入失败

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            inject_fn=inject_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        mock_llm.calls.clear()
        mock_send.calls.clear()
        # LLM 给个非空回复
        mock_llm.set_return_value("hello reply")
        await sched.run_batch("g1")
        # inject_fn 被调用一次但失败
        assert len(inject_calls) == 1
        # 降级走旧路径：llm_fn + send_fn 被调用
        assert mock_llm.call_count >= 1
        assert mock_send.call_count == 1
        # send_fn 收到 LLM 输出
        assert mock_send.calls[-1][1] == "hello reply"

    asyncio.run(_run())


def test_scheduler_quota_exceeded_suppresses(
    mock_config,
    mock_llm,
    mock_embed,
    mock_send,
    mock_kv,
    mock_log,
    tmp_data_dir,
    make_interest_data,
):
    """配额超限 → suppressed_reason="quota" 且不调 inject。"""

    async def _run():
        inject_calls: list[tuple] = []

        async def inject_fn(umo, text, hint, group_id, sender_id=""):
            inject_calls.append((umo, text, hint, group_id, sender_id))
            return True

        sched = _make_scheduler(
            mock_config=mock_config,
            mock_llm=mock_llm,
            mock_embed=mock_embed,
            mock_send=mock_send,
            mock_kv=mock_kv,
            mock_log=mock_log,
            tmp_data_dir=tmp_data_dir,
            inject_fn=inject_fn,
        )
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # per_hour=1：配额极易超限
        mock_config["max_proactive_per_hour"] = 1
        mock_config["max_proactive_per_day"] = 0
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        # 预先记录 1 条发送 → 配额已满（per_hour=1）
        g = sched._get_group("g1")
        g["quota"].record(time.time())
        mock_llm.calls.clear()
        mock_send.calls.clear()
        await sched.run_batch("g1")
        # 决策记录存在且 suppressed_reason="quota"
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "quota"
        assert d["triggered"] is False
        # 不调 inject_fn / llm_fn / send_fn
        assert len(inject_calls) == 0
        assert mock_llm.call_count == 0
        assert mock_send.call_count == 0

    asyncio.run(_run())


def test_scheduler_adaptive_disabled(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """自适应关闭 → eff_threshold 等于 fusion.threshold（adaptive_mult 仍记录）。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["adaptive_threshold_enabled"] = False
        # 人为抬高 adaptive.mult 看是否生效（关闭时应不影响判定）
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        # 把 adaptive 多次高触发率拉到 2.0
        g = sched._get_group("g1")
        for _ in range(40):
            g["adaptive"].record(0.9, True)
        # 此时 multiplier 应已 > 1.0（多次评估后上升）
        assert g["adaptive"].multiplier() > 1.0
        await sched.run_batch("g1")
        # 触发了回复（说明 eff_threshold 没被 adaptive 放大）
        assert mock_send.call_count == 1
        # 决策 adaptive_mult 字段仍记录当前 multiplier（即使关闭也记录）
        d = sched._decision_log.recent(1)[0]
        assert d["adaptive_mult"] == g["adaptive"].multiplier()

    asyncio.run(_run())


def test_scheduler_adaptive_mult_recorded(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """adaptive_mult 写入决策：拉高 multiplier 后决策记录值与之一致。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        # 手动设置 adaptive 状态（用 restore 注入特定 mult）
        g = sched._get_group("g1")
        g["adaptive"].restore({"mult": 1.5, "since_eval": 0})
        assert g["adaptive"].multiplier() == 1.5
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        # adaptive_mult 字段 = 决策时的 multiplier（1.5）
        assert d["adaptive_mult"] == 1.5

    asyncio.run(_run())


def test_scheduler_collect_tune_stats_empty(scheduler_factory):
    """collect_tune_stats：空日志返回 total=0 且所有字段有默认值。"""
    sched = scheduler_factory()
    stats = sched.collect_tune_stats()
    assert stats["total"] == 0
    assert stats["triggered_count"] == 0
    assert stats["triggered_rate"] == 0.0
    assert stats["suppressed_hist"] == {}
    assert stats["score_mean"] == 0.0
    assert stats["threshold_mean"] == 0.0
    assert stats["hit_level_hist"] == {}
    assert "factors_mean" in stats
    assert stats["fatigue_value_mean"] == 0.0
    # config 子集仍返回（即使无决策）
    assert "config" in stats
    assert "base_threshold" in stats["config"]


def test_scheduler_collect_tune_stats_with_data(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """collect_tune_stats：喂决策后统计字段正确。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # 关闭等待窗口：触发回复后不收集同用户后续消息，避免后续 _seed_message
        # 消息被 wait_window 吞掉不入 buffer 导致只产生 1 条决策
        mock_config["wait_window_duration_ms"] = 0
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 喂 3 次决策
        for _ in range(3):
            await _seed_message(sched, "g1", "符玄")
            # 清空 buffer 防累积（on_message 后会调度批次任务，取消之）
            await sched.run_batch("g1")
        # 喂 1 次低分决策（不触发）
        mock_embed.set("无关文本xyz", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
        await _seed_message(sched, "g1", "无关文本xyz")
        await sched.run_batch("g1")

        stats = sched.collect_tune_stats()
        assert stats["total"] == 4
        # 至少 3 条触发（符玄命中规则通道 direct）
        assert stats["triggered_count"] >= 3
        assert 0.0 < stats["triggered_rate"] <= 1.0
        # suppressed_hist 至少包含 below_threshold 或空串
        assert isinstance(stats["suppressed_hist"], dict)
        # score 统计字段有值
        assert isinstance(stats["score_mean"], float)
        assert isinstance(stats["score_min"], float)
        assert isinstance(stats["score_max"], float)
        assert stats["score_min"] <= stats["score_mean"] <= stats["score_max"]
        # hit_level_hist 字典
        assert isinstance(stats["hit_level_hist"], dict)
        # factors_mean 含五键
        assert set(stats["factors_mean"].keys()) == {
            "s_int",
            "s_topic",
            "s_resp",
            "c_cooldown",
            "p_silence",
        }
        # config 子集含 v0.2.8 新键
        assert "max_proactive_per_hour" in stats["config"]
        assert "max_proactive_per_day" in stats["config"]
        assert "adaptive_threshold_enabled" in stats["config"]

    asyncio.run(_run())


# ======================================================================
# 7. web 测试
# ======================================================================
# 注：post_autotune handler 测试（5 项 + handler 计数 11→12）已由 agent-e
# 在 tests/test_web.py 中实现（test_web_post_autotune_*、
# test_web_build_handlers_returns_twelve）。本模块不重复。
# 见 test_web.py L376-458。
