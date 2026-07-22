"""test_scheduler.py —— E 调度器集成测试（最大模块）。

测试对象：core/scheduler.py → SocialScheduler
覆盖点（含 PRD §8 验收）：
- group_enabled AND 语义（§8.12）：whitelist/all 模式 + KV 快捷开关
- in_active_hours（§8.9）：时段内/外、空 schedule、跨午夜、非法格式
- run_batch 触发与抑制：
  - core 触发率高于 marginal（§8.1）
  - DRY_RUN 决策日志完整 + 零发送（§8.8）
  - 反感屏蔽（§8.5）
  - 冷却抑制非 core，core 可突破（§8.4）
  - 嵌入失败降级 rule_fallback（§8.6）
  - 个人跟踪快通道（§8.2）
  - 实时配置 live 读取（§8.13）
- on_message：基本路径、唤醒消息跳过、未启用群跳过
- on_bot_sent：状态转 EXPECTING_REPLY
- glance_once：最多插话一群（§8.3）
- replay：产生决策日志 + 零发送（§8.11）
- _pick_poll_candidate：选群、排除冷却/沉默/未启用
- get_status / set_group_enabled

异步测试统一用 asyncio.run() 包装，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from core.models import GroupState, TrackerEntry

# ======================================================================
# 辅助：喂入消息并取消自动调度的批次任务（避免与手动 run_batch 竞争）
# ======================================================================

async def _seed_message(
    sched, group_id, text, ts=None, user_id="u1", nickname="Alice",
    umo="aiocqhttp:g1", is_wake=False,
):
    """调用 on_message 喂入消息，然后取消自动调度的批次任务。"""
    if ts is None:
        ts = time.time()
    await sched.on_message(
        group_id=group_id, umo=umo, user_id=user_id, nickname=nickname,
        text=text, ts=ts, is_wake=is_wake,
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


def _make_replay_file(tmp_data_dir, name, messages):
    """创建回放 JSONL 文件。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    path = replay_dir / f"{name}.jsonl"
    lines = [json.dumps(m, ensure_ascii=False) for m in messages]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ======================================================================
# group_enabled AND 语义（§8.12）
# ======================================================================

def test_scheduler_group_enabled_all_mode(scheduler_factory, mock_config):
    """mode=all → 所有群启用。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    assert sched.group_enabled("any_group") is True


def test_scheduler_group_enabled_whitelist_in_list(scheduler_factory, mock_config):
    """mode=whitelist + 群在白名单 → 启用。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "whitelist"
    mock_config["group_whitelist"] = ["g1", "g2"]
    assert sched.group_enabled("g1") is True
    assert sched.group_enabled("g2") is True


def test_scheduler_group_enabled_whitelist_not_in_list(scheduler_factory, mock_config):
    """mode=whitelist + 群不在白名单 → 未启用。对应 §8.12。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "whitelist"
    mock_config["group_whitelist"] = ["g1"]
    assert sched.group_enabled("g_other") is False


def test_scheduler_group_enabled_kv_disabled(scheduler_factory, mock_config):
    """KV 显式停用 → 未启用（AND 语义）。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    sched._group_enable_cache = {"g1": False}
    assert sched.group_enabled("g1") is False


def test_scheduler_group_enabled_kv_enabled(scheduler_factory, mock_config):
    """KV 显式启用 → 启用。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    sched._group_enable_cache = {"g1": True}
    assert sched.group_enabled("g1") is True


def test_scheduler_group_enabled_cache_none_falls_back_to_mode(scheduler_factory, mock_config):
    """缓存未就绪时只判定 mode_ok。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    sched._group_enable_cache = None
    assert sched.group_enabled("g1") is True  # mode=all → True


def test_scheduler_group_enabled_whitelist_kv_disabled_and(scheduler_factory, mock_config):
    """whitelist + KV 停用 → False（AND 语义：两者都满足才启用）。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "whitelist"
    mock_config["group_whitelist"] = ["g1"]
    sched._group_enable_cache = {"g1": False}
    assert sched.group_enabled("g1") is False


def test_scheduler_set_group_enabled_updates_cache_and_kv(
    scheduler_factory, mock_config, mock_kv
):
    """set_group_enabled 写缓存 + 写 KV。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    asyncio.run(sched.set_group_enabled("g1", False))
    assert sched._group_enable_cache["g1"] is False
    assert mock_kv["group_enable"]["g1"] is False
    assert sched.group_enabled("g1") is False
    # 重新启用
    asyncio.run(sched.set_group_enabled("g1", True))
    assert sched.group_enabled("g1") is True


# ======================================================================
# in_active_hours（§8.9）
# ======================================================================

def _ts_for(hour, minute=0):
    """构造本地时间某天 hour:minute 的 epoch 时间戳。"""
    return datetime(2026, 1, 1, hour, minute, 0).timestamp()


def test_scheduler_in_active_hours_inside_segment(scheduler_factory, mock_config):
    """10:00 在 09:00-12:00 段内 → True。对应 §8.9。"""
    sched = scheduler_factory()
    mock_config["schedule"] = [{"start": "09:00", "end": "12:00"}]
    assert sched.in_active_hours(_ts_for(10, 0)) is True


def test_scheduler_in_active_hours_outside_all_segments(scheduler_factory, mock_config):
    """13:00 不在任何段内 → False。对应 §8.9。"""
    sched = scheduler_factory()
    mock_config["schedule"] = [
        {"start": "09:00", "end": "12:00"},
        {"start": "14:00", "end": "18:00"},
    ]
    assert sched.in_active_hours(_ts_for(13, 0)) is False


def test_scheduler_in_active_hours_empty_schedule(scheduler_factory, mock_config):
    """空 schedule → False（全天不活跃）。"""
    sched = scheduler_factory()
    mock_config["schedule"] = []
    assert sched.in_active_hours(_ts_for(10, 0)) is False


def test_scheduler_in_active_hours_cross_midnight(scheduler_factory, mock_config):
    """跨午夜段 22:00-02:00：23:30 在段内 → True。"""
    sched = scheduler_factory()
    mock_config["schedule"] = [{"start": "22:00", "end": "02:00"}]
    assert sched.in_active_hours(_ts_for(23, 30)) is True
    assert sched.in_active_hours(_ts_for(1, 0)) is True  # 01:00 也在段内
    assert sched.in_active_hours(_ts_for(3, 0)) is False  # 03:00 在段外


def test_scheduler_in_active_hours_invalid_format_skipped(scheduler_factory, mock_config):
    """非法时段格式被跳过，不抛异常。"""
    sched = scheduler_factory()
    mock_config["schedule"] = [
        {"start": "25:00", "end": "12:00"},  # 非法小时
        {"start": "09:00", "end": "12:00"},  # 合法
    ]
    # 10:00 命中合法段
    assert sched.in_active_hours(_ts_for(10, 0)) is True


def test_scheduler_in_active_hours_boundary_start(scheduler_factory, mock_config):
    """段边界：start 时刻在内，end 时刻不在内（半开区间）。"""
    sched = scheduler_factory()
    mock_config["schedule"] = [{"start": "09:00", "end": "12:00"}]
    assert sched.in_active_hours(_ts_for(9, 0)) is True  # start 在内
    assert sched.in_active_hours(_ts_for(12, 0)) is False  # end 不在内


# ======================================================================
# run_batch：核心决策管线
# ======================================================================

def test_scheduler_run_batch_no_trigger_low_score(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """batch_emb 与兴趣质心正交 → s_int=0 → score 低 → 不触发。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        # batch_emb 与 core 正交 → s_int=0
        mock_embed.set("天气真好", [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "天气真好")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is False
    asyncio.run(_run())


def test_scheduler_run_batch_core_triggers(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """core 命中 + score >= threshold → 触发发送。对应 §8.1。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄配队")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        assert d["hit_level"] == "core"
    asyncio.run(_run())


def test_scheduler_run_batch_marginal_higher_threshold_than_core(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """core 触发但 marginal 不触发（阈值倍率生效）。对应 §8.1。

    用 base_threshold=0.9 拉开差距：
    - core: s_int=1.5, s_topic=1.0(自相似), score=1.9, threshold=0.9*0.7=0.63 → 触发
    - marginal: s_int=0.6, s_topic=1.0, score=1.0, threshold=0.9*1.3=1.17 → 不触发
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["base_threshold"] = 0.9
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                                 "marginal": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        # core 命中 → 触发
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        assert sched._decision_log.recent(1)[0]["hit_level"] == "core"

        # marginal 命中 → 不触发（阈值倍率 1.3 提高 threshold）
        mock_embed.set("新闻", [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g2", "新闻")
        await sched.run_batch("g2")
        assert mock_send.call_count == 1  # 仍为 1，marginal 未触发
        d2 = sched._decision_log.recent(1)[0]
        assert d2["hit_level"] == "marginal"
        assert d2["triggered"] is False
    asyncio.run(_run())


def test_scheduler_run_batch_dry_run_no_send(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """DRY_RUN 开启 → 决策日志完整 + send_fn 零调用。对应 §8.8。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["dry_run"] = True
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is False
        assert d["suppressed_reason"] == "dry_run"
        assert d["dry_run"] is True
    asyncio.run(_run())


def test_scheduler_run_batch_hate_suppress(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """命中 hate 质心 ≥ hate_threshold → 反感屏蔽。对应 §8.5。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        # hate 质心与 batch_emb 相同 → hate_score=1.0 >= 0.75
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                                 "hate": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("骂人话", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "骂人话")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "hate"
        assert d["triggered"] is False
    asyncio.run(_run())


def test_scheduler_run_batch_cooldown_suppress_non_core(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """COOLDOWN 状态 + general 命中（非 core）→ 冷却压制。对应 §8.4。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data,
                      centroids={"general": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("闲聊", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "闲聊")
        # 设置 COOLDOWN 状态
        g = sched._get_group("g1")
        g["state"] = GroupState.COOLDOWN
        g["state_until"] = time.time() + 100
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["suppressed_reason"] == "cooldown"
    asyncio.run(_run())


def test_scheduler_run_batch_cooldown_core_breakthrough(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """COOLDOWN 状态 + core 命中 → core 突破冷却。对应 §8.4。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        g = sched._get_group("g1")
        g["state"] = GroupState.COOLDOWN
        g["state_until"] = time.time() + 100
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        assert d["hit_level"] == "core"
    asyncio.run(_run())


def test_scheduler_run_batch_embed_fail_rule_fallback(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """嵌入失败 → 降级 rule_fallback（关键词+沉默），不抛异常。对应 §8.6。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                      high_kw=["符玄"])
        mock_embed.set_fail_mode(True)
        # 喂入旧消息（沉默 > 180s 默认阈值）
        old_ts = time.time() - 200
        await _seed_message(sched, "g1", "符玄怎么配队", ts=old_ts)
        await sched.run_batch("g1")
        # rule_fallback：命中"符玄" + 沉默 200>=180 → True → 触发
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
    asyncio.run(_run())


def test_scheduler_run_batch_embed_fail_no_keyword_no_trigger(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """嵌入失败 + 无关键词命中 → rule_fallback=False → 不触发。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(sched, make_interest_data, high_kw=["符玄"])
        mock_embed.set_fail_mode(True)
        await _seed_message(sched, "g1", "今天天气不错", ts=time.time() - 200)
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is False
    asyncio.run(_run())


def test_scheduler_run_batch_personal_tracker_fast_path(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """个人跟踪快通道：跟踪用户发言 s_resp >= personal_threshold → bypass 阈值触发。对应 §8.2。

    设置无兴趣质心 → s_int=0, hit_level="none"；
    s_topic=1.0(自相似) → score = 0 + 0.4 = 0.4 < threshold(0.65) → 正常不触发；
    但 personal_triggered=True（bot_last_emb 与 batch_emb 相同 → cosine=1.0 >= 0.55）→ 触发。
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        # 无兴趣质心 → s_int=0, hit_level="none", threshold=0.65
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("接话内容", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "接话内容", user_id="u_tracked")
        g = sched._get_group("g1")
        # 跟踪条目：bot_last_emb 与 batch_emb 相同 → cosine=1.0 >= personal_threshold(0.55)
        g["tracker"].add(TrackerEntry(
            user_id="u_tracked", nickname="Alice",
            bot_last_emb=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            last_own_text="hi", created_ts=time.time(),
        ))
        await sched.run_batch("g1")
        # personal_triggered=True → bypass 阈值触发发送
        assert mock_send.call_count == 1
        d = sched._decision_log.recent(1)[0]
        assert d["triggered"] is True
        # 注意：on_bot_sent 发送后会从 recent_speakers 重建跟踪条目，
        # 故不能断言 tracker 为空；此处仅验证发送发生（personal bypass 生效）。
    asyncio.run(_run())


def test_scheduler_run_batch_empty_buffer_returns_early(
    scheduler_factory, mock_config, mock_send
):
    """空缓冲 → run_batch 立即返回，无决策日志。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        # 直接调 run_batch（缓冲为空）
        await sched.run_batch("g1")
        assert len(sched._decision_log) == 0
        assert mock_send.call_count == 0
    asyncio.run(_run())


def test_scheduler_run_batch_topic_excludes_current_batch(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """BUG-2: run_batch 计算 topic_emb 时 _batches 不含当前批次（无历史 → s_topic=0）。

    对应 PRD F2/§8.4。修复前 push_batch 在 evaluate 之前，当前批次进入滑动窗口
    致自相似 → s_topic=1.0；修复后 push_batch 移到 evaluate 之后，无历史批次时
    topic_emb=None → s_topic=0。通过 decision_log.factors.s_topic 断言。
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        # marginal 质心与 batch_emb 正交 → s_int=0, hit_level="none"
        _set_interest(sched, make_interest_data,
                      centroids={"marginal": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        # batch_emb=[1,0,0,...]，与 marginal 正交
        mock_embed.set("新闻", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "新闻")
        await sched.run_batch("g1")
        # 无历史批次 → topic_emb=None → s_topic=0（非自相似 1.0）
        d = sched._decision_log.recent(1)[0]
        assert d["factors"]["s_topic"] == 0.0
    asyncio.run(_run())


# ======================================================================
# 实时配置 live 读取（§8.13）
# ======================================================================

def test_scheduler_live_config_threshold_change(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """修改 base_threshold 后下一次 run_batch 用新阈值。对应 §8.13。

    BUG-2 修复后 run_batch 在 evaluate 之后才 push_batch：
    - 第一次 run_batch：无历史批次 → topic_emb=None → s_topic=0；
      marginal score = s_int(0.6) + 0 = 0.6。
      用 base_threshold=0.9：threshold=0.9*1.3=1.17 > 0.6 → 不触发。
    - 第二次 run_batch：历史批次=[1,0,0,...]（同 text "新闻"），当前批次同向量；
      topic_emb=[1,0,0,...]，s_topic=cosine([1,0,...],[1,0,...])=1.0；
      score = 0.6 + 0.4*1.0 = 1.0。
      改 base_threshold=0.5：threshold=0.5*1.3=0.65 < 1.0 → 触发。
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        # v0.2 迁移：关闭规则通道（α=0 → final=score_b），恢复 v0.1 阈值语义，
        # 让本用例专注验证 base_threshold 实时变更对触发判定的影响。
        mock_config["enable_rule_channel"] = False
        mock_config["base_threshold"] = 0.9  # 高阈值 → 不触发
        _set_interest(sched, make_interest_data,
                      centroids={"marginal": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("新闻", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "新闻")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0  # 高阈值不触发

        # 改阈值：base_threshold=0.5 → threshold=0.5*1.3=0.65 < 1.0 → 触发
        mock_config["base_threshold"] = 0.5
        await _seed_message(sched, "g1", "新闻")
        await sched.run_batch("g1")
        assert mock_send.call_count == 1  # 新阈值触发
    asyncio.run(_run())


# ======================================================================
# on_message
# ======================================================================

def test_scheduler_on_message_records_context_and_buffer(
    scheduler_factory, mock_config
):
    """on_message 记录窗口、活跃度、入缓冲。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        await _seed_message(sched, "g1", "hello", user_id="u1", nickname="Alice")
        g = sched._get_group("g1")
        assert g["umo"] == "aiocqhttp:g1"
        assert g["last_active_ts"] > 0
        assert len(g["msg_timestamps"]) == 1
        assert g["buffer"].pending_count() == 1
        assert g["context"].last_message_ts() > 0
    asyncio.run(_run())


def test_scheduler_on_message_wake_skips_buffer(scheduler_factory, mock_config):
    """唤醒消息(@机器人) → 不入缓冲（框架处理被动回复）。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        await _seed_message(sched, "g1", "@bot hello", is_wake=True)
        g = sched._get_group("g1")
        # 窗口有消息（记录），但缓冲为空
        assert g["context"].last_message_ts() > 0
        assert g["buffer"].pending_count() == 0
    asyncio.run(_run())


def test_scheduler_on_message_disabled_group_skips_buffer(
    scheduler_factory, mock_config
):
    """未启用群 → 不入缓冲（仅记录窗口）。对应 §8.12。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "whitelist"
        mock_config["group_whitelist"] = []  # g1 不在白名单
        await _seed_message(sched, "g1", "hello")
        g = sched._get_group("g1")
        assert g["context"].last_message_ts() > 0  # 窗口记录
        assert g["buffer"].pending_count() == 0  # 缓冲为空
    asyncio.run(_run())


def test_scheduler_on_message_enable_off_skips_buffer(
    scheduler_factory, mock_config
):
    """总开关 enable=False → 不入缓冲。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["enable"] = False
        await _seed_message(sched, "g1", "hello")
        g = sched._get_group("g1")
        assert g["buffer"].pending_count() == 0
    asyncio.run(_run())


# ======================================================================
# on_bot_sent
# ======================================================================

def test_scheduler_on_bot_sent_sets_expecting_reply(
    scheduler_factory, mock_config, mock_embed, make_interest_data
):
    """on_bot_sent → 状态转 EXPECTING_REPLY + 记录嵌入。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_embed.set("bot reply", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # 先有用户消息（建跟踪候选需要 recent_speakers）
        await _seed_message(sched, "g1", "hi", user_id="u1")
        await sched.on_bot_sent(group_id="g1", text="bot reply", ts=time.time())
        g = sched._get_group("g1")
        assert g["state"] == GroupState.EXPECTING_REPLY
        assert g["last_bot_emb"] is not None
        assert g["last_bot_ts"] > 0
    asyncio.run(_run())


def test_scheduler_on_bot_sent_builds_tracker(
    scheduler_factory, mock_config, mock_embed, make_interest_data
):
    """on_bot_sent 建立跟踪候选（最近 2 个发言者）。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_embed.set("reply", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "hi1", user_id="u1", nickname="A")
        await _seed_message(sched, "g1", "hi2", user_id="u2", nickname="B")
        await sched.on_bot_sent(group_id="g1", text="reply", ts=time.time())
        g = sched._get_group("g1")
        tracker_entries = g["tracker"].all()
        assert len(tracker_entries) == 2
    asyncio.run(_run())


# ======================================================================
# glance_once（§8.3 瞥一眼最多一群）
# ======================================================================

def test_scheduler_glance_once_max_one_group(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """瞥眼候选多群命中 → 最多插话一个群。对应 §8.3。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = True
        # BUG-1: 关键词从 InterestData 取（修复后），不再从 config 读
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                      high_kw=["符玄"])
        mock_config["glance_min_score"] = 0.5  # 降低门槛便于触发
        # 两个群都有命中关键词的最近消息
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g2", "符玄配队", user_id="u2")
        await _seed_message(sched, "g3", "符玄配队", user_id="u3")
        # 从 g1 发起瞥眼
        await _seed_message(sched, "g1", "hi", user_id="u1")
        await sched.glance_once("g1")
        # 最多发送一次
        assert mock_send.call_count <= 1
    asyncio.run(_run())


def test_scheduler_glance_once_uses_interest_keywords(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """BUG-1: glance_once 关键词从 InterestData 取，候选群最后消息含关键词 → 进入嵌入判断。

    对应 PRD F5/§8.3。修复前从 cfg 取恒为 []，关键词路径永不命中；
    修复后 interest_mgr.get() 返回含 high_interest_keywords=["符玄"] 的 InterestData，
    候选群最后一条消息含"符玄" → 触发嵌入调用（mock_embed.call_count 增加）。
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = True
        mock_config["glance_min_score"] = 0.5
        # 关键词放在 InterestData（修复后路径），不放 config
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                      high_kw=["符玄"])
        # 候选群最后一条消息含关键词"符玄"
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g2", "符玄配队", user_id="u2")
        await _seed_message(sched, "g1", "hi", user_id="u1")
        embed_before = mock_embed.call_count
        await sched.glance_once("g1")
        # 关键词命中 → 进入嵌入判断路径 → mock_embed 至少多调用一次
        assert mock_embed.call_count > embed_before
    asyncio.run(_run())


def test_scheduler_glance_once_no_keywords_skips_embed(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """BUG-1 对比：InterestData 未加载或无关键词 → 不进入嵌入判断路径。

    interest_mgr.get() 返回 None（未加载）→ high_kws=[] → 关键词不命中 →
    不调用 embed；与上用例对照证明关键词确实来自 InterestData。
    """
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = True
        mock_config["glance_min_score"] = 0.5
        # 不调 _set_interest → interest_mgr.get() 返回 None
        mock_embed.set("符玄配队", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g2", "符玄配队", user_id="u2")
        await _seed_message(sched, "g1", "hi", user_id="u1")
        embed_before = mock_embed.call_count
        await sched.glance_once("g1")
        # 无关键词 → 不进入嵌入判断 → mock_embed 调用计数不变
        assert mock_embed.call_count == embed_before
        assert mock_send.call_count == 0
    asyncio.run(_run())


def test_scheduler_glance_once_disabled_returns(
    scheduler_factory, mock_config, mock_send
):
    """glance_enable=False → 立即返回不发送。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["glance_enable"] = False
        await sched.glance_once("g1")
        assert mock_send.call_count == 0
    asyncio.run(_run())


def test_scheduler_glance_once_replay_active_returns(
    scheduler_factory, mock_config, mock_send
):
    """回放期间不瞥眼。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["glance_enable"] = True
        sched._replay_active = True
        await sched.glance_once("g1")
        assert mock_send.call_count == 0
    asyncio.run(_run())


# ======================================================================
# replay（§8.11）
# ======================================================================

def test_scheduler_replay_run_batch_no_send_when_active(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """_replay_active=True 时 run_batch 视同 DRY_RUN：决策日志完整 + 零发送。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        sched._replay_active = True
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        await sched.run_batch("g1")
        assert mock_send.call_count == 0
        d = sched._decision_log.recent(1)[0]
        assert d["dry_run"] is True
        assert d["suppressed_reason"] == "dry_run"
    asyncio.run(_run())


def test_scheduler_replay_full_flow_produces_decisions_zero_send(
    scheduler_factory, mock_config, mock_embed, mock_send, mock_log, tmp_data_dir,
    make_interest_data
):
    """完整回放流程：样例 JSONL → 产生决策日志且零发送。对应 §8.11。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["batch_interval_min"] = 0.05
        mock_config["batch_interval_max"] = 0.05
        # 设置高阈值避免回放后批次任务触发发送（race 保护）
        mock_config["base_threshold"] = 999.0
        _set_interest(sched, make_interest_data,
                      centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
        # 创建回放文件：3 条消息，ts 间隔 1 秒
        msgs = [
            {"ts": 1000.0 + i, "group_id": "g1", "user_id": f"u{i}",
             "nickname": f"N{i}", "text": f"消息{i}"}
            for i in range(3)
        ]
        _make_replay_file(tmp_data_dir, "test", msgs)
        # 执行回放（speed=10 → 0.1s 间隔）
        await sched.replay("test", 10.0)
        # 等待批次任务完成
        await asyncio.sleep(0.5)
        # 决策日志应有条目
        assert len(sched._decision_log) >= 1
        # 零发送
        assert mock_send.call_count == 0
        assert sched._replay_active is False
    asyncio.run(_run())


def test_scheduler_stop_replay_sets_flag(scheduler_factory):
    """stop_replay 置 _replay_stop=True。"""
    sched = scheduler_factory()
    sched._replay_stop = False
    sched.stop_replay()
    assert sched._replay_stop is True


# ======================================================================
# _pick_poll_candidate
# ======================================================================

def test_scheduler_pick_poll_candidate_picks_active_group(
    scheduler_factory, mock_config
):
    """选最近活跃度最高的群。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    now = time.time()
    g1 = sched._get_group("g1")
    g1["last_active_ts"] = now - 10
    g1["state"] = GroupState.IDLE
    g2 = sched._get_group("g2")
    g2["last_active_ts"] = now - 1  # 更近
    g2["state"] = GroupState.IDLE
    candidate = sched._pick_poll_candidate(now, silent_minutes=10)
    assert candidate == "g2"


def test_scheduler_pick_poll_candidate_skips_cooldown(
    scheduler_factory, mock_config
):
    """COOLDOWN 状态群被跳过。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    now = time.time()
    g1 = sched._get_group("g1")
    g1["last_active_ts"] = now
    g1["state"] = GroupState.COOLDOWN
    g1["state_until"] = now + 100
    assert sched._pick_poll_candidate(now, silent_minutes=10) is None


def test_scheduler_pick_poll_candidate_skips_silent(
    scheduler_factory, mock_config
):
    """沉默群（超过 silent_minutes）被跳过。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    now = time.time()
    g1 = sched._get_group("g1")
    g1["last_active_ts"] = now - 1000  # 远超 10 分钟
    g1["state"] = GroupState.IDLE
    assert sched._pick_poll_candidate(now, silent_minutes=10) is None


def test_scheduler_pick_poll_candidate_skips_disabled(
    scheduler_factory, mock_config
):
    """未启用群被跳过。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "whitelist"
    mock_config["group_whitelist"] = []  # 无群启用
    now = time.time()
    g1 = sched._get_group("g1")
    g1["last_active_ts"] = now
    g1["state"] = GroupState.IDLE
    assert sched._pick_poll_candidate(now, silent_minutes=10) is None


def test_scheduler_pick_poll_candidate_skips_active_monitoring(
    scheduler_factory, mock_config
):
    """ACTIVE_MONITORING 状态群被跳过。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    now = time.time()
    g1 = sched._get_group("g1")
    g1["last_active_ts"] = now
    g1["state"] = GroupState.ACTIVE_MONITORING
    assert sched._pick_poll_candidate(now, silent_minutes=10) is None


def test_scheduler_pick_poll_candidate_skips_no_activity(
    scheduler_factory, mock_config
):
    """无活跃度（last_active_ts=0）群被跳过。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    sched._get_group("g1")  # last_active_ts=0 默认
    assert sched._pick_poll_candidate(time.time(), silent_minutes=10) is None


# ======================================================================
# get_status
# ======================================================================

def test_scheduler_get_status_structure(scheduler_factory, mock_config):
    """get_status 返回完整状态结构。"""
    sched = scheduler_factory()
    mock_config["group_mode"] = "all"
    status = sched.get_status()
    for key in ("running", "in_active_hours", "current_monitoring",
                "groups", "metrics", "interest_loaded", "replay_active",
                "dry_run", "decision_count"):
        assert key in status
    assert status["running"] is False  # 主循环未启动
    assert status["interest_loaded"] is False
    assert status["decision_count"] == 0


def test_scheduler_get_status_includes_group_info(
    scheduler_factory, mock_config
):
    """get_status 包含各群状态信息。"""
    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        await _seed_message(sched, "g1", "hello", user_id="u1")
        status = sched.get_status()
        assert len(status["groups"]) == 1
        g = status["groups"][0]
        assert g["id"] == "g1"
        assert "state" in g
        assert "enabled" in g
        assert "msg_per_min" in g
    asyncio.run(_run())


# ======================================================================
# _check_state_expiry
# ======================================================================

def test_scheduler_check_state_expiry_expecting_to_idle(scheduler_factory, mock_config):
    """EXPECTING_REPLY 过期 → IDLE。"""
    sched = scheduler_factory()
    g = sched._get_group("g1")
    g["state"] = GroupState.EXPECTING_REPLY
    g["state_until"] = time.time() - 1  # 已过期
    sched._check_state_expiry(g, time.time())
    assert g["state"] == GroupState.IDLE
    assert g["state_until"] == 0.0


def test_scheduler_check_state_expiry_not_expired_keeps_state(scheduler_factory):
    """未过期状态保持不变。"""
    sched = scheduler_factory()
    g = sched._get_group("g1")
    g["state"] = GroupState.EXPECTING_REPLY
    g["state_until"] = time.time() + 100  # 未过期
    sched._check_state_expiry(g, time.time())
    assert g["state"] == GroupState.EXPECTING_REPLY


def test_scheduler_check_state_expiry_zero_until_no_op(scheduler_factory):
    """state_until=0 时无操作。"""
    sched = scheduler_factory()
    g = sched._get_group("g1")
    g["state"] = GroupState.IDLE
    g["state_until"] = 0.0
    sched._check_state_expiry(g, time.time())
    assert g["state"] == GroupState.IDLE


# ======================================================================
# _send 辅助方法（umo 空跳过、replay 跳过）
# ======================================================================

def test_scheduler_send_empty_umo_returns_false(scheduler_factory, mock_log):
    """_send umo 为空 → 返回 False + log warning。"""
    async def _run():
        sched = scheduler_factory()
        ok = await sched._send("", "text")
        assert ok is False
        assert mock_log.has("warning")
    asyncio.run(_run())


def test_scheduler_send_replay_active_returns_false(scheduler_factory, mock_send):
    """_send 在 replay_active 时返回 False 不发送。"""
    async def _run():
        sched = scheduler_factory()
        sched._replay_active = True
        ok = await sched._send("aiocqhttp:g1", "text")
        assert ok is False
        assert mock_send.call_count == 0
    asyncio.run(_run())


# ======================================================================
# _embed 降级机制
# ======================================================================

def test_scheduler_embed_degraded_after_failures(
    scheduler_factory, mock_embed, mock_log
):
    """嵌入连续失败 3 次 → 进入降级模式。"""
    async def _run():
        sched = scheduler_factory()
        mock_embed.set_fail_mode(True)
        # 前 3 次失败
        for _ in range(3):
            await sched._embed(["text"])
        assert sched._embed_degraded is True
        # 第 4 次：降级模式直接返回 None（不调 embed_fn）
        mock_embed.set_fail_mode(False)
        result = await sched._embed(["text"])
        assert result is None
        assert mock_log.has("warning")
    asyncio.run(_run())


def test_scheduler_embed_success_resets_fail_count(scheduler_factory, mock_embed):
    """嵌入成功 → 失败计数重置。"""
    async def _run():
        sched = scheduler_factory()
        mock_embed.set_fail_mode(True)
        await sched._embed(["t1"])  # 失败 1
        await sched._embed(["t2"])  # 失败 2
        assert sched._embed_fail_count == 2
        mock_embed.set_fail_mode(False)
        result = await sched._embed(["t3"])  # 成功
        assert result is not None
        assert sched._embed_fail_count == 0
        assert sched._embed_degraded is False
    asyncio.run(_run())


def test_scheduler_embed_empty_texts_returns_none(scheduler_factory):
    """空 texts → 返回 None。"""
    async def _run():
        sched = scheduler_factory()
        result = await sched._embed([])
        assert result is None
    asyncio.run(_run())
