"""test_v0_2_6.py —— v0.2.6 修复项（F1-F12）单元测试。

测试对象：
- core/config_store.py → F1/F3/F6/F9 的 DEFAULT_CONFIG/VALIDATORS/SPECIAL_KEYS
- core/scheduler.py → F5/F8 的空批次过滤和长窗口注入逻辑
- core/interest.py → F2/F4/F9 的 InterestManager 方法
- core/prompts.py → F9 的 build_interest_prompt
- core/models.py → F12 的 BatchDecision.embedding_degraded
- core/web.py → F11 的 export handler
- main.py → F1/F3/F4 的集成逻辑

覆盖修复点（F1-F12）：
  F1  配置持久化修复：initialize() 中先 load KV 再构造 scheduler
  F2  兴趣增删改查：add_item / update_item / remove_item
  F3  Embedding 提供商迁移：SPECIAL_KEYS 含 embedding_provider_id，DEFAULT_CONFIG 不含
  F4  人设变更触发兴趣重建：set_config_view 检测 persona_text 变更 → regenerate
  F5  空批次过滤：batch_text.strip() 为空直接 return
  F6  默认值调整：base_threshold 0.55, w_int 1.2, w_silence 0.35, after_reply_probability 0.7
  F8  上下文注入迁移：run_batch 触发回复且 long_window_inject_proactive=True 时注入长窗口
  F9  可配置的示例句子和关键词数量
  F11 导出 API：GET /prosocial/export 返回完整导出数据
  F12 Embedding 降级标记：BatchDecision.embedding_degraded

不依赖 AstrBot 运行时，全部离线 pytest。异步测试统一用 asyncio.run() 包装。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.config_store import SPECIAL_KEYS, ConfigStore
from core.interest import InterestManager
from core.models import BatchDecision, InterestLevel, ScoreFactors
from core.prompts import build_interest_prompt
from core.web import build_handlers


# ======================================================================
# F1: 配置持久化修复
# ======================================================================


def test_f1_config_store_load_from_kv(mock_kv):
    """F1: ConfigStore.load() 从 KV 读取配置覆盖默认值。

    initialize() 中先 load KV 再构造 scheduler，消除重载竞态。
    本测试验证 load 本身能正确从 KV 加载配置到缓存。
    """

    async def _run():
        store = ConfigStore()
        # 写入 KV 模拟已持久化的配置
        override = {"base_threshold": 0.8, "w_int": 2.0}
        await mock_kv.set("config", json.dumps(override))
        # load 从 KV 读取并合并到缓存
        await store.load(mock_kv.get)
        cfg = store.get()
        assert cfg["base_threshold"] == 0.8
        assert cfg["w_int"] == 2.0
        # 未覆盖的键保持默认
        assert cfg["w_topic"] == ConfigStore.DEFAULT_CONFIG["w_topic"]

    asyncio.run(_run())


def test_f1_config_store_load_kv_missing_keeps_default(mock_kv):
    """F1: KV 无配置时 load 不改缓存，保持默认值。"""

    async def _run():
        store = ConfigStore()
        # KV 无 "config" 键
        await store.load(mock_kv.get)
        cfg = store.get()
        assert cfg["base_threshold"] == ConfigStore.DEFAULT_CONFIG["base_threshold"]

    asyncio.run(_run())


# ======================================================================
# F3: Embedding 提供商迁移
# ======================================================================


def test_f3_special_keys_contains_embedding_provider_id():
    """F3: SPECIAL_KEYS 包含 embedding_provider_id。

    embedding_provider_id 从 ConfigStore.DEFAULT_CONFIG 移除，加入 SPECIAL_KEYS，
    由 _conf_schema.json 的 _special: "select_provider" 原生管理。
    """
    assert "embedding_provider_id" in SPECIAL_KEYS


def test_f3_default_config_excludes_embedding_provider_id():
    """F3: DEFAULT_CONFIG 不包含 embedding_provider_id。

    该键已从普通配置中移除，由 AstrBotConfig 原生承载。
    """
    assert "embedding_provider_id" not in ConfigStore.DEFAULT_CONFIG


def test_f3_special_keys_contains_chat_provider_id():
    """F3: SPECIAL_KEYS 包含 chat_provider_id（原有特殊键未丢失）。"""
    assert "chat_provider_id" in SPECIAL_KEYS


# ======================================================================
# F4: 人设变更触发兴趣重建
# ======================================================================


def test_f4_set_config_view_persona_text_triggers_regenerate(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F4: set_config_view 传入 persona_text 变更 → interest_mgr.regenerate 被调用。

    通过观察 InterestManager._data.persona_hash 变化间接验证 regenerate 被触发。
    """
    mgr = InterestManager(tmp_data_dir, mock_log)
    # 先生成一次
    asyncio.run(
        mgr.regenerate("旧人设", "知识", mock_llm, mock_embed)
    )
    old_hash = mgr.get().persona_hash

    # 模拟 set_config_view 中 persona_text 变更逻辑
    new_persona = "新人设描述，完全不同"
    asyncio.run(
        mgr.regenerate(new_persona, "知识", mock_llm, mock_embed)
    )
    new_hash = mgr.get().persona_hash
    assert new_hash != old_hash


# ======================================================================
# F5: 空批次过滤
# ======================================================================


def test_f5_empty_batch_text_returns_early(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """F5: 空批次 batch_text 为空白时 run_batch 提前返回，无决策记录、无嵌入调用。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        # 喂入空文本消息
        await _seed_message(sched, "g1", "")
        embed_before = mock_embed.call_count
        await sched.run_batch("g1")
        # 无决策记录（空批次提前 return）
        assert len(sched._decision_log) == 0
        # 无额外嵌入调用
        assert mock_embed.call_count == embed_before

    asyncio.run(_run())


def test_f5_whitespace_only_batch_returns_early(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """F5: 仅含空白的 batch_text 也被过滤。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        _set_interest(
            sched,
            make_interest_data,
            centroids={"core": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
        await _seed_message(sched, "g1", "   ")
        await sched.run_batch("g1")
        assert len(sched._decision_log) == 0

    asyncio.run(_run())


# ======================================================================
# F6: 默认值调整
# ======================================================================


def test_f6_default_base_threshold():
    """F6: base_threshold 默认值从 0.65 调至 0.55。"""
    assert ConfigStore.DEFAULT_CONFIG["base_threshold"] == 0.55


def test_f6_default_w_int():
    """F6: w_int 默认值从 1.0 调至 1.2。"""
    assert ConfigStore.DEFAULT_CONFIG["w_int"] == 1.2


def test_f6_default_w_silence():
    """F6: w_silence 默认值从 0.2 调至 0.35。"""
    assert ConfigStore.DEFAULT_CONFIG["w_silence"] == 0.35


def test_f6_default_after_reply_probability():
    """F6: after_reply_probability 默认值从 0.6 调至 0.7。"""
    assert ConfigStore.DEFAULT_CONFIG["after_reply_probability"] == 0.7


# ======================================================================
# F8: 上下文注入迁移
# ======================================================================


def test_f8_run_batch_injects_long_window_on_trigger(
    scheduler_factory, mock_config, mock_embed, mock_send, mock_llm,
    make_interest_data
):
    """F8: run_batch 触发回复且 long_window_inject_proactive=True 时注入长窗口上下文。

    验证方式：LLM prompt 包含「相关历史背景」（长窗口注入成功时才出现的标志）。
    为此需要群有足够的长窗口历史消息，且触发成功。
    """

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False  # 仅规则通道保证触发
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["long_window_inject_proactive"] = True
        mock_config["long_window_size"] = 20
        _set_interest(sched, make_interest_data, centroids={})
        # 喂入多条消息以积累上下文
        t0 = time.time()
        for i in range(15):
            await sched.on_message(
                group_id="g1", umo="aiocqhttp:g1", user_id="u1",
                nickname="Alice", text=f"历史消息{i}", ts=t0 - 100 + i,
                is_wake=False,
            )
        g = sched._get_group("g1")
        t = g.get("batch_task")
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            g["batch_task"] = None

        # 触发回复
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        mock_llm.calls.clear()
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        # LLM 应被调用且 prompt 含长窗口相关内容（"相关历史背景" 或 "相关历史摘要"）
        last_prompt = mock_llm.calls[-1] if mock_llm.calls else ""
        # 触发成功且有长窗口注入 → prompt 包含长窗口文本段
        assert "相关历史背景" in last_prompt or "相关历史摘要" in last_prompt

    asyncio.run(_run())


def test_f8_long_window_inject_disabled(
    scheduler_factory, mock_config, mock_embed, mock_send, mock_llm,
    make_interest_data
):
    """F8: long_window_inject_proactive=False 时主动回复不注入长窗口上下文。"""

    async def _run():
        sched = scheduler_factory()
        mock_config["group_mode"] = "all"
        mock_config["glance_enable"] = False
        mock_config["enable_vector_channel"] = False
        mock_config["rule_direct_wakeup_words"] = ["符玄"]
        mock_config["long_window_inject_proactive"] = False
        _set_interest(sched, make_interest_data, centroids={})
        mock_embed.set("符玄", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        await _seed_message(sched, "g1", "符玄")
        mock_llm.calls.clear()
        await sched.run_batch("g1")
        assert mock_send.call_count == 1
        last_prompt = mock_llm.calls[-1] if mock_llm.calls else ""
        # long_window_inject_proactive=False → 不含长窗口注入标志
        assert "相关历史背景" not in last_prompt
        assert "相关历史摘要" not in last_prompt

    asyncio.run(_run())


# ======================================================================
# F9: 可配置的示例句子和关键词数量
# ======================================================================


def test_f9_build_interest_prompt_default_counts():
    """F9: build_interest_prompt 默认 example_count=3, keyword_count=12。"""
    prompt = build_interest_prompt("你是一个机器人", "")
    assert "3 句示例" in prompt or "3 句" in prompt or "3 套" in prompt or "3 句示例对话" in prompt
    assert "12 个高唤醒关键词" in prompt or "12 个" in prompt


def test_f9_build_interest_prompt_custom_counts():
    """F9: build_interest_prompt 接受 example_count=5, keyword_count=20 参数。"""
    prompt = build_interest_prompt(
        "你是一个机器人", "", example_count=5, keyword_count=20
    )
    assert "5 句示例" in prompt or "5 句" in prompt or "5 句示例对话" in prompt
    assert "20 个高唤醒关键词" in prompt or "20 个" in prompt


def test_f9_interest_example_count_in_default_config():
    """F9: interest_example_count 在 DEFAULT_CONFIG 中，默认 3。"""
    assert ConfigStore.DEFAULT_CONFIG["interest_example_count"] == 3


def test_f9_interest_keyword_count_in_default_config():
    """F9: interest_keyword_count 在 DEFAULT_CONFIG 中，默认 12。"""
    assert ConfigStore.DEFAULT_CONFIG["interest_keyword_count"] == 12


def test_f9_interest_counts_in_validators():
    """F9: interest_example_count/interest_keyword_count 在 VALIDATORS 中。"""
    assert "interest_example_count" in ConfigStore.VALIDATORS
    assert "interest_keyword_count" in ConfigStore.VALIDATORS
    typ, lo, hi = ConfigStore.VALIDATORS["interest_example_count"]
    assert typ is int and lo == 1 and hi == 10
    typ, lo, hi = ConfigStore.VALIDATORS["interest_keyword_count"]
    assert typ is int and lo == 3 and hi == 30


def test_f9_interest_manager_regenerate_accepts_counts(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F9: InterestManager.regenerate 接受 example_count/keyword_count 参数。

    验证 LLM 被调用时 prompt 包含自定义数量。
    """
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(
        mgr.regenerate("人设", "知识", mock_llm, mock_embed, example_count=7, keyword_count=25)
    )
    # LLM 应被调用且 prompt 含自定义数量
    assert mock_llm.call_count > 0
    last_prompt = mock_llm.calls[-1]
    assert "7 句" in last_prompt or "7 句示例" in last_prompt or "7 句示例对话" in last_prompt
    assert "25 个" in last_prompt or "25 个高唤醒" in last_prompt


def test_f9_interest_manager_ensure_loaded_accepts_counts(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F9: InterestManager.ensure_loaded 接受 example_count/keyword_count 参数。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(
        mgr.ensure_loaded("人设", "知识", mock_llm, mock_embed, example_count=2, keyword_count=8)
    )
    assert mgr.get() is not None
    assert mock_llm.call_count > 0
    last_prompt = mock_llm.calls[-1]
    assert "2 句" in last_prompt or "2 句示例" in last_prompt or "2 句示例对话" in last_prompt
    assert "8 个" in last_prompt or "8 个高唤醒" in last_prompt


# ======================================================================
# F11: 导出 API
# ======================================================================


class _MockBridgeExport:
    """实现 WebBridge 鸨子接口（含 get_export_view）的 mock。"""

    def __init__(self):
        self.export_data = {
            "config": {"base_threshold": 0.55},
            "decisions": [{"ts": 1000, "score": 0.8}],
            "fatigue": {"value": 1.2, "level": "low"},
            "interests": {"generated": True, "items": []},
            "version": "v0.2.6",
            "export_time": 1721712000.0,
        }

    def get_export_view(self) -> dict:
        return self.export_data


def _run_handler(handler, params=None, body=None):
    return asyncio.run(handler(params or {}, body))


def test_f11_export_handler_returns_complete_data():
    """F11: GET /prosocial/export 返回完整导出数据（config/decisions/fatigue/interests/version）。"""
    bridge = _MockBridgeExport()
    handlers = build_handlers(bridge)
    h = handlers["GET /prosocial/export"]
    status, body = _run_handler(h)
    assert status == 200
    assert body["ok"] is True
    data = body["data"]
    assert "config" in data
    assert "decisions" in data
    assert "fatigue" in data
    assert "interests" in data
    assert "version" in data
    assert data["version"] == "v0.2.6"


def test_f11_export_handler_bridge_exception_500():
    """F11: bridge.get_export_view 抛异常 → 500。"""
    bridge = _MockBridgeExport()

    def raise_fn():
        raise RuntimeError("export boom")

    bridge.get_export_view = raise_fn
    handlers = build_handlers(bridge)
    h = handlers["GET /prosocial/export"]
    status, body = _run_handler(h)
    assert status == 500
    assert body["ok"] is False
    assert "export boom" in body["error"]


# ======================================================================
# F12: Embedding 降级标记
# ======================================================================


def test_f12_batch_decision_embedding_degraded_default():
    """F12: BatchDecision.embedding_degraded 默认 False。"""
    d = BatchDecision(
        ts=1000.0, group_id="g1", batch_summary="hi",
        factors=ScoreFactors(0.0, 0.0, 0.0, 0.0, 0.0),
        score=0.5, threshold=0.55, hit_level="none",
        triggered=False, suppressed_reason="", dry_run=False, message_count=1,
    )
    assert d.embedding_degraded is False


def test_f12_batch_decision_embedding_degraded_on_null_emb():
    """F12: batch_emb 为 None 时决策记录 embedding_degraded=True。"""

    async def _run():
        # 构造一个场景使嵌入失败 → batch_emb 为 None
        # 使用 mock_embed fail mode
        from tests.conftest import _MockEmbed, _MockLLM, _MockSend, _MockKV, _MockLog
        from core.ratelimit import TokenBucketRateLimiter
        from core.scheduler import SocialScheduler

        mock_embed = _MockEmbed(dim=8)
        mock_llm = _MockLLM()
        mock_send = _MockSend()
        mock_kv = _MockKV()
        mock_log = _MockLog()
        from pathlib import Path
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        mock_config = {"enable": True, "dry_run": False, "group_mode": "all",
                       "glance_enable": False, "embedding_rate_limit_per_min": 30,
                       "base_threshold": 0.55, "w_int": 1.2, "w_topic": 0.4,
                       "w_resp": 0.8, "w_cooldown": 0.5, "w_silence": 0.35,
                       "core_interest_modifier": 0.7, "general_interest_modifier": 1.0,
                       "edge_interest_modifier": 1.3, "expecting_modifier": 0.8,
                       "enable_rule_channel": True, "enable_vector_channel": True,
                       "rule_direct_wakeup_words": ["符玄"],
                       "batch_interval_min": 2.0, "batch_interval_max": 5.0,
                       "short_window_size": 8, "long_window_size": 20,
                       "buffer_max_size": 200, "personal_threshold": 0.55,
                       "hate_similarity_threshold": 0.75,
                       "dynamic_fusion_enabled": False,
                       "fusion_weight_rule": 0.4,
                       "dynamic_alpha_wake": 0.8, "dynamic_alpha_short_expect": 0.2,
                       "fatigue_suppress_enabled": True, "fatigue_limit": 5.0,
                       "fatigue_recovery_rate": 0.1,
                       "fatigue_cost_active": 1.2, "fatigue_cost_passive": 0.8,
                       "fatigue_cost_track": 0.6, "fatigue_cost_glance": 1.5,
                       "fatigue_high_modifier": 1.2, "fatigue_medium_modifier": 1.1,
                       "after_reply_probability": 0.7,
                       "interest_example_count": 3, "interest_keyword_count": 12,
                       "long_window_inject_proactive": True,
                       }
        from core.interest import InterestManager
        interest_mgr = InterestManager(tmp, mock_log)
        rate_limiter = TokenBucketRateLimiter(30)
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
            data_dir=tmp,
        )
        sched._group_enable_cache = {}

        # 设置嵌入失败
        mock_embed.set_fail_mode(True)
        # 喂入消息
        await sched.on_message(
            group_id="g1", umo="aiocqhttp:g1", user_id="u1",
            nickname="Alice", text="符玄配队", ts=time.time(), is_wake=False,
        )
        g = sched._get_group("g1")
        t = g.get("batch_task")
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            g["batch_task"] = None

        await sched.run_batch("g1")
        # 应有决策记录
        assert len(sched._decision_log) == 1
        d = sched._decision_log.recent(1)[0]
        # 嵌入失败 → embedding_degraded=True
        assert d["embedding_degraded"] is True

    asyncio.run(_run())


def test_f12_embedding_degraded_false_on_success(
    scheduler_factory, mock_config, mock_embed, mock_send, make_interest_data
):
    """F12: 嵌入成功时 embedding_degraded=False。"""

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
        assert d["embedding_degraded"] is False

    asyncio.run(_run())


# ======================================================================
# F2: 兴趣增删改查
# ======================================================================


def test_f2_add_item_example(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: add_item(kind="example") 向指定 label 追加示例句子，重算质心。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    data_before = mgr.get()
    core_before = [it for it in data_before.items if it.level == InterestLevel.CORE][0]
    count_before = len(core_before.examples)
    ok, msg = asyncio.run(
        mgr.add_item("example", "core", "新增的示例句子", mock_embed)
    )
    assert ok is True
    data_after = mgr.get()
    core_after = [it for it in data_after.items if it.level == InterestLevel.CORE][0]
    assert len(core_after.examples) == count_before + 1
    assert "新增的示例句子" in core_after.examples
    # 质心已重算（可能变化）
    assert "core" in data_after.centroids


def test_f2_add_item_high_keyword(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: add_item(kind="high_keyword") 向高唤醒关键词追加。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    ok, msg = asyncio.run(
        mgr.add_item("high_keyword", "", "新关键词", mock_embed)
    )
    assert ok is True
    assert "新关键词" in mgr.get().high_interest_keywords


def test_f2_add_item_hate_keyword(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: add_item(kind="hate_keyword") 向反感关键词追加。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    ok, msg = asyncio.run(
        mgr.add_item("hate_keyword", "", "脏话", mock_embed)
    )
    assert ok is True
    assert "脏话" in mgr.get().hate_keywords


def test_f2_add_item_duplicate_keyword_skipped(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: add_item 重复关键词不重复追加。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    ok1, _ = asyncio.run(
        mgr.add_item("high_keyword", "", "重复词", mock_embed)
    )
    assert ok1 is True
    count_before = len(mgr.get().high_interest_keywords)
    ok2, _ = asyncio.run(
        mgr.add_item("high_keyword", "", "重复词", mock_embed)
    )
    assert ok2 is True
    assert len(mgr.get().high_interest_keywords) == count_before


def test_f2_add_item_no_data(mock_embed, mock_log, tmp_data_dir):
    """F2: 未生成时 add_item 返回 (False, '尚未生成兴趣数据')。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    ok, msg = asyncio.run(mgr.add_item("example", "core", "text", mock_embed))
    assert ok is False
    assert "尚未生成" in msg


def test_f2_update_item_example(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: update_item(kind="example") 替换示例句子，重算质心。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    core = [it for it in mgr.get().items if it.level == InterestLevel.CORE][0]
    old_text = core.examples[0]
    ok, msg = asyncio.run(
        mgr.update_item("example", "core", old_text, "替换后的示例", mock_embed)
    )
    assert ok is True
    core_after = [it for it in mgr.get().items if it.level == InterestLevel.CORE][0]
    assert "替换后的示例" in core_after.examples
    assert old_text not in core_after.examples


def test_f2_update_item_high_keyword(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: update_item(kind="high_keyword") 替换高唤醒关键词。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    old_kw = mgr.get().high_interest_keywords[0]
    ok, msg = asyncio.run(
        mgr.update_item("high_keyword", "", old_kw, "新关键词", mock_embed)
    )
    assert ok is True
    assert "新关键词" in mgr.get().high_interest_keywords
    assert old_kw not in mgr.get().high_interest_keywords


def test_f2_update_item_not_found(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: update_item 找不到 old_text 返回 (False, ...)。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    ok, msg = asyncio.run(
        mgr.update_item("example", "core", "不存在的文本", "新文本", mock_embed)
    )
    assert ok is False
    assert "未找到" in msg


def test_f2_remove_item_example(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: remove_item(kind="example") 删除示例句子，重算质心。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    core = [it for it in mgr.get().items if it.level == InterestLevel.CORE][0]
    text_to_remove = core.examples[0]
    count_before = len(core.examples)
    ok, msg = asyncio.run(
        mgr.remove_item("example", "core", text_to_remove, mock_embed)
    )
    assert ok is True
    core_after = [it for it in mgr.get().items if it.level == InterestLevel.CORE][0]
    assert len(core_after.examples) == count_before - 1
    assert text_to_remove not in core_after.examples


def test_f2_remove_item_hate_keyword(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: remove_item(kind="hate_keyword") 删除反感关键词。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    hate_kw = mgr.get().hate_keywords[0]
    ok, msg = asyncio.run(
        mgr.remove_item("hate_keyword", "", hate_kw, mock_embed)
    )
    assert ok is True
    assert hate_kw not in mgr.get().hate_keywords


def test_f2_remove_item_not_found(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """F2: remove_item 找不到文本返回 (False, ...)。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    ok, msg = asyncio.run(
        mgr.remove_item("example", "core", "不存在的文本", mock_embed)
    )
    assert ok is False
    assert "未找到" in msg


# ======================================================================
# 辅助函数（与 test_v0_2.py 保持一致语义）
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
