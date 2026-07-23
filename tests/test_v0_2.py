"""test_v0_2.py —— v0.2 双通道融合 / 疲劳 / 惯性 / 等待窗口单元测试。

测试对象：core/fusion.py / core/rule_engine.py / core/fatigue.py / core/inertia.py
        + core/scheduler.py 的 v0.2 集成路径（run_batch 融合判定 / on_bot_sent 疲劳消耗+防重+惯性
        / on_message 等待窗口路由 / get_status fatigue+inertia 字段）。

覆盖 PRD §7 验收点 1-9：
  #1-3  通道开关与融合判定（双开 / 仅 rule / 仅 vector / 动态权重）
  #4    规则引擎屏蔽短语
  #5    全局疲劳（consume / should_suppress / threshold_modifier / 防重 / 持久化）
  #6-7  惯性（on_reply 阈值倍率 / 主动话题生命周期）与等待窗口（收满 / @ / 超时关闭）
  #8    get_status 含 fatigue + 每群 inertia
  #9    v0.1 持久化 decision_log 向后兼容加载

不依赖 AstrBot 运行时，全部离线 pytest。异步测试统一用 asyncio.run() 包装。
辅助函数 _seed_message / _set_interest 与 test_scheduler.py 保持一致语义（本文件内独立定义）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from core.models import GroupState

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
# 通道开关与融合判定（验收 #1-3）
# ======================================================================


def test_fusion_both_channels_default_alpha(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """双通道默认开（α=0.4），无规则信号、score_b 高 → final=0.6*score_b；拉高阈值使 final<threshold 不触发。

    覆盖验收 #2：双开时 final = α·score_a + (1−α)·score_b，channel=="fusion"。
    v0.2.6 注意：w_int 默认值从 1.0 调至 1.2，score_b = s_int * w_int = 1.5 * 1.2 = 1.8。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        # 拉高 base_threshold 使 final(1.08) < threshold(1.4) → 不触发
        mock_config["base_threshold"] = 2.0
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        # batch_emb 与 core 质心重合 → s_int=1.5, score_b=1.5*1.2=1.8（w_int=1.2）
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄配队")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["channel"] == "fusion"
        assert d["alpha"] == 0.4  # 默认 fusion_weight_rule
        assert d["score_a"] == 0.0  # 无规则信号（无唤醒词/疑问/@）
        assert d["score_b"] == pytest.approx(1.8)
        # final = 0.4*0 + 0.6*1.8 = 1.08
        assert d["score"] == pytest.approx(1.08)
        assert d["triggered"] is False

    asyncio.run(_run())


def test_fusion_rule_only_channel(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """关 vector（enable_vector_channel=False）→ α=1.0 → final=score_a，命中强唤醒词触发。

    覆盖验收 #1：仅 rule 通道可独立完成唤醒决策，channel=="rule"。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # 无兴趣质心 → 向量通道即便算出 score_b 也不影响 final（α=1.0）
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["channel"] == "rule"
        assert d["alpha"] == 1.0
        # mentions_bot=True(+70) + matched_word(+30) + 短句(+18) = 118 → score_a=1.0
        assert d["score_a"] == 1.0
        assert d["triggered"] is True

    asyncio.run(_run())


def test_fusion_vector_only_channel(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """关 rule（enable_rule_channel=False）→ α=0.0 → final=score_b，等价 v0.1。

    覆盖验收 #1：仅 vector 通道可独立完成唤醒决策，channel=="vector"，score_a 仍计算但不影响 final。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_rule_channel"] = False
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄配队")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["channel"] == "vector"
        assert d["alpha"] == 0.0
        assert d["score_b"] == pytest.approx(1.8)  # s_int=1.5 * w_int=1.2
        # final = 0*score_a + 1.0*score_b = score_b
        assert d["score"] == pytest.approx(d["score_b"])
        assert d["triggered"] is True

    asyncio.run(_run())


def test_fusion_direct_wakeup_word_high_alpha(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """双通道开 + 动态融合开 + batch_text 含强唤醒词 → mentions_bot=True → α=dynamic_alpha_wake(0.8)。

    覆盖验收 #3：强唤醒→α=0.8，final 主要由 score_a 决定。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["dynamic_fusion_enabled"] = True
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # 无兴趣质心 → score_b≈0，final 主要由 score_a 决定
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        assert d["channel"] == "fusion"
        assert d["alpha"] == 0.8  # dynamic_alpha_wake
        assert d["score_a"] == 1.0  # mentions_bot+matched_word+短句 → 118/100 clamp 1.0
        # final = 0.8*1.0 + 0.2*score_b；score_b≈0 → final≈0.8
        assert d["score"] == pytest.approx(0.8 * d["score_a"] + 0.2 * d["score_b"])
        assert d["triggered"] is True

    asyncio.run(_run())


def test_fusion_short_text_expecting_low_alpha(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """双通道开 + 动态开 + expecting=True + 短文本(≤8字) → α=dynamic_alpha_short_expect(0.2)。

    覆盖验收 #3：短消息+期待→α=0.2，final 主要由 score_b 决定。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["dynamic_fusion_enabled"] = True
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("天气真好啊", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "天气真好啊")  # 5 字 ≤ 8
        # 设置 EXPECTING_REPLY 使 expecting=True
        g = sched._get_group("g1")
        g["state"] = GroupState.EXPECTING_REPLY
        g["state_until"] = time.time() + 100
        await sched.run_batch("g1")
        d = sched._decision_log.recent(1)[0]
        assert d["channel"] == "fusion"
        assert d["alpha"] == 0.2  # dynamic_alpha_short_expect
        # 无唤醒词/疑问/@ → score_a=0；final 主要由 score_b 决定
        assert d["score_a"] == 0.0
        assert d["score_b"] == pytest.approx(1.8)  # core s_int=1.5 * w_int=1.2
        assert d["score"] == pytest.approx(0.2 * d["score_a"] + 0.8 * d["score_b"])
        assert d["triggered"] is True

    asyncio.run(_run())


# ======================================================================
# 屏蔽与抑制（验收 #4）
# ======================================================================


def test_block_phrase_suppresses_trigger(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """batch_text 含屏蔽短语（"别回"）→ rule_signal.suppress_reason=="block_phrase" → 不触发。

    覆盖验收 #4：屏蔽短语 score_a=0、triggered=False、suppressed_reason=="block_phrase"。
    即便配置了强唤醒词且向量分足够触发，屏蔽短语优先级最高。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        # core 质心与 batch_emb 重合 → score_b 高，若无屏蔽本会触发
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("符玄别回了", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄别回了")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "block_phrase"
        assert d["triggered"] is False
        assert d["score_a"] == 0.0

    asyncio.run(_run())


# ======================================================================
# 疲劳（验收 #5）
# ======================================================================


def test_fatigue_consume_on_active_reply(
    scheduler_factory, mock_config, mock_embed, make_interest_data
):
    """on_bot_sent(reply_type="active") 消耗 fatigue_cost_active(1.2)，连续多次后 level 升 high。

    覆盖验收 #5：连续 consume 后 value 升高、级别升 high。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_embed.set("bot reply", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "hi", user_id="u1")
        t0 = time.time()
        # 5 次主动回复，每次间隔 3s（>2s 防重窗口，衰减可忽略）
        for i in range(5):
            await sched.on_bot_sent(
                group_id="g1",
                text="bot reply",
                ts=t0 + i * 3.0,
                reply_type="active",
                is_proactive=True,
            )
        # 在最后一次 consume 时刻快照（dt=0 无衰减），5*1.2=6.0 cap 到 limit=5.0 → ratio=1.0 → high
        snap = sched._fatigue.snapshot(now=t0 + 12.0)
        assert snap["value"] > 1.2  # 远高于单次消耗
        assert snap["level"] == "high"

    asyncio.run(_run())


def test_fatigue_should_suppress_high_non_forced(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """疲劳拉到 high，should_suppress(is_forced=False) → run_batch 不触发，reason=="fatigue"。

    覆盖验收 #5：高疲劳且非强制唤醒被抑制。general 命中（非 core/非 @/非 direct）→ is_forced=False。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(
            sched,
            make_interest_data,
            centroids={"general": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("闲聊", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 疲劳拉到 high：直接设 value>limit（restore/consume 会 cap 到 limit，
        # 而 high 要求 ratio>=1.0，任何衰减都会跌到 medium，故直接设内部值高于 limit 保持稳健）
        sched._fatigue._value = 100.0
        sched._fatigue._last_ts = time.time()
        assert sched._fatigue.should_suppress(is_forced=False) is True
        await _seed_message(sched, "g1", "闲聊")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "fatigue"
        assert d["triggered"] is False

    asyncio.run(_run())


def test_fatigue_not_suppress_when_forced(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """高疲劳但 is_forced=True（core 命中）→ should_suppress 返回 False → 仍可触发。

    覆盖验收 #5：强制唤醒不受疲劳抑制。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 疲劳拉到 high（value>limit，见上用例说明）
        sched._fatigue._value = 100.0
        sched._fatigue._last_ts = time.time()
        assert sched._fatigue.should_suppress(is_forced=True) is False
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        assert d["hit_level"] == "core"

    asyncio.run(_run())


def test_fatigue_threshold_modifier(scheduler_factory, mock_config):
    """fatigue medium 时 threshold_modifier≈1.1，high 时≈1.2，none 时=1.0。

    覆盖验收 #5：A_modifier 随疲劳级别变化。
    """
    sched = scheduler_factory()
    fm = sched._fatigue
    now = 1000.0
    # none：value=0
    fm.restore(0.0, now)
    assert fm.threshold_modifier(now) == 1.0
    # medium：value=3.0, limit=5.0 → ratio=0.6 ≥ 0.55
    fm.restore(3.0, now)
    assert fm.threshold_modifier(now) == 1.1
    # high：value=5.0 → ratio=1.0
    fm.restore(5.0, now)
    assert fm.threshold_modifier(now) == 1.2


def test_on_bot_sent_dedup_skips_consume(
    scheduler_factory, mock_config, mock_embed, make_interest_data
):
    """同 text <2s 连续两次 on_bot_sent → 第二次跳过 consume（value 不增加），但嵌入/状态仍执行。

    覆盖验收 #5：防重窗口避免主动发送后框架 after_message_sent 再触发时重复消耗疲劳。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_embed.set("bot reply", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "hi", user_id="u1")
        t0 = time.time()
        # 第一次：消耗 1.2
        await sched.on_bot_sent(
            group_id="g1",
            text="bot reply",
            ts=t0,
            reply_type="active",
        )
        value_after_first = sched._fatigue.state()[0]
        embed_after_first = mock_embed.call_count
        assert value_after_first == 1.2  # fatigue_cost_active
        # 第二次：同 text，ts 距上次 <2s → 防重，跳过 consume/inertia
        await sched.on_bot_sent(
            group_id="g1",
            text="bot reply",
            ts=t0 + 1.0,
            reply_type="active",
        )
        value_after_second = sched._fatigue.state()[0]
        # 疲劳值未增加（未 consume）
        assert value_after_second == value_after_first
        # 嵌入仍执行（记录己方发言嵌入）
        assert mock_embed.call_count > embed_after_first

    asyncio.run(_run())


def test_fatigue_persist_restore(
    scheduler_factory, mock_config, mock_kv, make_interest_data
):
    """stop() 持久化 fatigue KV，restore() 重新加载后值一致。

    覆盖验收 #5：疲劳状态可持久化往返。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        # 消耗一定疲劳
        sched._fatigue.consume("active", now=1000.0)
        sched._fatigue.consume("active", now=1003.0)
        value_before = sched._fatigue.state()[0]
        assert value_before > 0.0
        # stop() 持久化 fatigue 到 KV
        await sched.stop()
        assert "fatigue" in mock_kv
        fv = mock_kv["fatigue"]
        assert fv["value"] == value_before
        # 新 scheduler 通过 restore 加载（用 state() 比较 raw 值，避免 snapshot 四舍五入差异）
        sched2 = scheduler_factory()
        sched2._fatigue.restore(fv["value"], fv["last_ts"])
        assert sched2._fatigue.state()[0] == pytest.approx(value_before)

    asyncio.run(_run())


# ======================================================================
# 惯性与等待窗口（验收 #6-7）
# ======================================================================


def test_inertia_on_reply_lowers_threshold_multiplier(scheduler_factory, mock_config):
    """on_reply(is_proactive=True) 后 threshold_multiplier<1.0（after_reply 窗口内）。

    覆盖验收 #6：回复后窗口内阈值倍率降低（更易触发）。
    """
    sched = scheduler_factory()
    inertia = sched._get_group("g1")["inertia"]
    now = 1000.0
    # 回复前：倍率 1.0
    assert inertia.threshold_multiplier(now) == 1.0
    inertia.on_reply(now=now, is_proactive=True)
    # after_reply 窗口内：×(1-0.7×0.5)=0.65；proactive 窗口内：×(1-0.5×0.5)=0.75 → 0.4875
    mult = inertia.threshold_multiplier(now + 10)
    assert mult < 1.0
    assert mult == pytest.approx(0.4875)  # 0.65 * 0.75


def test_inertia_proactive_topic_user_responds(scheduler_factory, mock_config):
    """on_reply(is_proactive=True) 开主动话题 → on_user_message 返回 True（indirect_success+1）
    → 随后 check_proactive_timeout 返回 False。

    覆盖验收 #6：proactive 有人回应记 indirect_success。
    """
    sched = scheduler_factory()
    inertia = sched._get_group("g1")["inertia"]
    now = 1000.0
    inertia.on_reply(now=now, is_proactive=True)
    assert inertia.proactive_awaiting is True
    # 用户在 proactive 窗口内回应
    responded = inertia.on_user_message(now + 30)
    assert responded is True
    assert inertia.indirect_success == 1
    assert inertia.proactive_awaiting is False
    # 已回应 → 不再计超时失败
    timeout = inertia.check_proactive_timeout(now + 100)
    assert timeout is False
    assert inertia.proactive_failure == 0


def test_inertia_proactive_topic_timeout(scheduler_factory, mock_config):
    """on_reply(is_proactive=True) 后无 on_user_message → 推进时间超 proactive_boost_duration
    → check_proactive_timeout 返回 True（proactive_failure+1）。

    覆盖验收 #6：proactive 超时无回应记 failure。
    """
    sched = scheduler_factory()
    inertia = sched._get_group("g1")["inertia"]
    now = 1000.0
    inertia.on_reply(now=now, is_proactive=True)
    assert inertia.proactive_awaiting is True
    # 无用户消息；推进超过 proactive_boost_duration(60s)
    timeout = inertia.check_proactive_timeout(now + 70)
    assert timeout is True
    assert inertia.proactive_failure == 1
    assert inertia.proactive_awaiting is False


def test_wait_window_collect_and_close_on_max_extra():
    """WaitWindow.open → 连续 add 同 trigger_user 消息满 max_extra 条 → should_close=True，merged_text 拼接正确。

    覆盖验收 #7：窗口收满 max_extra 条后关闭。
    """
    from core.inertia import WaitWindow

    ww = WaitWindow(duration_ms=3000, max_extra=3)
    ww.open(now_ms=1000.0, trigger_user_id="u1")
    assert ww.active is True
    # 未收满 → 不关闭
    ww.add(now_ms=1100.0, user_id="u1", text="m1", is_at=False)
    ww.add(now_ms=1200.0, user_id="u1", text="m2", is_at=False)
    assert ww.should_close(now_ms=1200.0) is False
    # 第 3 条 → len(texts)>=max_extra → full → should_close
    ww.add(now_ms=1300.0, user_id="u1", text="m3", is_at=False)
    assert ww.should_close(now_ms=1300.0) is True
    assert ww.merged_text() == "m1\nm2\nm3"


def test_wait_window_close_on_at():
    """WaitWindow 开窗后，其他用户 add(is_at=True) → should_close=True（@ 强制关闭）。

    覆盖验收 #7：@ 消息强制关闭等待窗口。
    """
    from core.inertia import WaitWindow

    ww = WaitWindow(duration_ms=3000, max_extra=3)
    ww.open(now_ms=1000.0, trigger_user_id="u1")
    # 其他用户发 @ 消息 → force_close
    ww.add(now_ms=1100.0, user_id="u2", text="@bot hi", is_at=True)
    assert ww.should_close(now_ms=1100.0) is True


def test_wait_window_close_on_timeout():
    """WaitWindow 开窗后推进时间超 duration_ms → should_close=True。

    覆盖验收 #7：窗口超时关闭。
    """
    from core.inertia import WaitWindow

    ww = WaitWindow(duration_ms=3000, max_extra=3)
    ww.open(now_ms=1000.0, trigger_user_id="u1")
    # 窗口内 → 不关闭
    assert ww.should_close(now_ms=2000.0) is False
    # 超过 deadline → 关闭
    assert ww.should_close(now_ms=4001.0) is True


# ======================================================================
# get_status 与向后兼容（验收 #8-9）
# ======================================================================


def test_get_status_includes_fatigue_and_inertia(scheduler_factory, mock_config):
    """scheduler.get_status() 返回 dict 含 'fatigue' 键（value/limit/ratio/level），
    每群 groups[*] 含 'inertia' 键。

    覆盖验收 #8：状态面板含全局疲劳 + 每群惯性。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        await _seed_message(sched, "g1", "hello", user_id="u1")
        status = sched.get_status()
        assert "fatigue" in status
        for k in ("value", "limit", "ratio", "level"):
            assert k in status["fatigue"]
        assert len(status["groups"]) == 1
        g = status["groups"][0]
        assert "inertia" in g
        for k in (
            "after_reply_active",
            "proactive_active",
            "proactive_awaiting",
            "indirect_success",
            "proactive_failure",
        ):
            assert k in g["inertia"]

    asyncio.run(_run())


def test_decision_log_backward_compat_load_v0_1(scheduler_factory, mock_config):
    """构造 v0.1 格式 BatchDecision dict（无 score_a/score_b/alpha/fatigue_level/fatigue_value/channel），
    通过 scheduler._decision_log.load([...]) 加载，不报错且 v0.2 字段有默认值。

    覆盖验收 #9：加载 v0.1 持久化 decision_log 向后兼容。
    """
    sched = scheduler_factory()
    v01_decision = {
        "ts": 1000.0,
        "group_id": "g1",
        "batch_summary": "hi",
        "factors": {
            "s_int": 1.0,
            "s_topic": 0.5,
            "s_resp": 0.0,
            "c_cooldown": 0.0,
            "p_silence": 0.1,
        },
        "score": 0.8,
        "threshold": 0.65,
        "hit_level": "core",
        "triggered": True,
        "suppressed_reason": "",
        "dry_run": False,
        "message_count": 1,
        # 无 v0.2 增量字段
    }
    # 不抛异常
    sched._decision_log.load([v01_decision])
    assert len(sched._decision_log) == 1
    d = sched._decision_log.recent(1)[0]
    # v0.2 字段使用默认值
    assert d["score_a"] == 0.0
    assert d["score_b"] == 0.0
    assert d["alpha"] == 0.0
    assert d["fatigue_level"] == "none"
    assert d["fatigue_value"] == 0.0
    assert d["channel"] == "vector"
    # v0.1 字段保留
    assert d["score"] == 0.8
    assert d["triggered"] is True
    assert d["hit_level"] == "core"
