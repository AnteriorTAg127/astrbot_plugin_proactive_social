"""共享 fixtures for astrbot_plugin_proactive_social 离线 pytest 套件。

提供 mock 注入回调（mock_llm / mock_embed / mock_send / mock_kv / mock_config /
mock_log）与临时数据目录，供全部 test_*.py 复用。所有 mock 可被测试覆盖配置。

设计要点：
- **mock_embed**：默认按文本 sha256 取种子 → numpy RandomState 生成 dim 维向量 →
  单位化，保证相似度断言可重现；测试可经 .set(text, vector) 注入特定向量。
- **mock_llm**：默认返回合法兴趣 JSON（附录 A 格式）；测试可经
  set_response(prompt_substr, text) 或 set_return_value(text) 注入任意返回。
- **mock_kv**：内存 dict 模拟 KV 存储，支持默认值；同时暴露 dict 接口便于断言。
- **mock_config**：可变 dict，决策引擎每次实时读取（模拟 live 配置）；改动即时生效。
- **tmp_data_dir**：pytest tmp_path 子目录，测试间隔离，自动清理。
- 异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# 把插件根目录加入 sys.path，使 tests 能 import core.*
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


# ======================================================================
# mock_embed —— 确定性嵌入函数
# ======================================================================


class _MockEmbed:
    """确定性嵌入函数。

    默认行为：对每条文本用 sha256 取 4 字节种子 → numpy RandomState 生成
    ``dim`` 维向量 → 单位化，保证相同文本永远得到相同向量，相似度断言可重现。

    测试可经 ``set(text, vector)`` 注入特定向量覆盖默认；
    ``set_fail_mode(True)`` 让调用抛异常（测试嵌入降级路径）。
    """

    def __init__(self, dim: int = 8):
        self.dim = dim
        self._overrides: dict[str, list[float]] = {}
        self.call_count: int = 0
        self.calls: list[list[str]] = []  # 每次调用的 texts 快照
        self._fail_mode: bool = False

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.calls.append(list(texts))
        if self._fail_mode:
            raise RuntimeError("mock_embed forced failure")
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        if text in self._overrides:
            return list(self._overrides[text])
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:4], "big")
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dim).astype(np.float64)
        norm = float(np.linalg.norm(v))
        if norm == 0.0:
            v = np.zeros(self.dim, dtype=np.float64)
            v[0] = 1.0
            return v.tolist()
        return (v / norm).tolist()

    def set(self, text: str, vector: list[float]) -> None:
        """为指定文本注入固定向量（覆盖默认 hash 向量）。"""
        self._overrides[text] = list(vector)

    def set_fail_mode(self, fail: bool = True) -> None:
        self._fail_mode = fail

    def reset(self) -> None:
        self._overrides.clear()
        self.call_count = 0
        self.calls.clear()
        self._fail_mode = False


@pytest.fixture
def mock_embed():
    """确定性嵌入函数（dim=8）。"""
    return _MockEmbed(dim=8)


# ======================================================================
# mock_llm —— LLM 回调
# ======================================================================

# 默认合法兴趣 JSON（附录 A 格式），供 interest 测试与默认场景使用
DEFAULT_INTEREST_JSON = json.dumps(
    {
        "interests": [
            {
                "label": "core",
                "topic": "星穹铁道配队",
                "examples": ["符玄怎么配队？", "量子队现版本还强吗？"],
                "weight": 1.5,
            },
            {
                "label": "general",
                "topic": "生活闲聊",
                "examples": ["今天天气不错", "吃饭了没"],
                "weight": 1.0,
            },
            {
                "label": "marginal",
                "topic": "新闻时事",
                "examples": ["最近有什么新闻", "看看热搜"],
                "weight": 0.6,
            },
            {
                "label": "hate",
                "topic": "恶意言论",
                "examples": ["骂人的话", "恶意刷屏"],
                "weight": 1.0,
            },
        ],
        "hate_keywords": ["骂人", "刷屏"],
        "high_interest_keywords": ["符玄", "量子队", "银狼"],
    },
    ensure_ascii=False,
)


class _MockLLM:
    """Mock LLM 回调。

    默认返回 DEFAULT_INTEREST_JSON。测试可：
    - set_return_value(text)：设置默认返回值
    - set_response(prompt_substr, text)：当 prompt 含 substr 时返回 text
    - set_fail_mode(True)：调用抛异常
    """

    def __init__(self):
        self.return_value: str = DEFAULT_INTEREST_JSON
        self._responses_by_prompt: dict[str, str] = {}
        self.call_count: int = 0
        self.calls: list[str] = []
        self._fail_mode: bool = False

    async def __call__(self, prompt: str) -> str:
        self.call_count += 1
        self.calls.append(prompt)
        if self._fail_mode:
            raise RuntimeError("mock_llm forced failure")
        for substr, resp in self._responses_by_prompt.items():
            if substr in prompt:
                return resp
        return self.return_value

    def set_response(self, prompt_substr: str, response: str) -> None:
        self._responses_by_prompt[prompt_substr] = response

    def set_return_value(self, text: str) -> None:
        self.return_value = text

    def set_fail_mode(self, fail: bool = True) -> None:
        self._fail_mode = fail

    def reset(self) -> None:
        self.return_value = DEFAULT_INTEREST_JSON
        self._responses_by_prompt.clear()
        self.call_count = 0
        self.calls.clear()
        self._fail_mode = False


@pytest.fixture
def mock_llm():
    return _MockLLM()


# ======================================================================
# mock_send —— 发送回调
# ======================================================================


class _MockSend:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.return_value: bool = True
        self._fail_mode: bool = False

    async def __call__(self, umo: str, text: str) -> bool:
        self.calls.append((umo, text))
        if self._fail_mode:
            return False
        return self.return_value

    def set_return_value(self, ok: bool) -> None:
        self.return_value = ok

    def set_fail_mode(self, fail: bool = True) -> None:
        self._fail_mode = fail

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()
        self.return_value = True
        self._fail_mode = False


@pytest.fixture
def mock_send():
    return _MockSend()


# ======================================================================
# mock_kv —— 内存 KV 存储
# ======================================================================


class _MockKV:
    """内存 dict KV，模拟 AstrBot PluginKVStoreMixin。"""

    def __init__(self):
        self._data: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def clear(self) -> None:
        self._data.clear()


@pytest.fixture
def mock_kv():
    return _MockKV()


# ======================================================================
# mock_config —— 可变 live 配置 dict
# ======================================================================


def default_config() -> dict:
    """返回 PRD §3 全部默认配置的可变 dict 副本。

    决策引擎每次决策实时读取此 dict，测试改后立即生效（模拟 live 配置）。
    """
    return {
        "enable": True,
        "dry_run": False,
        "embedding_provider_id": "",
        "chat_provider_id": "",
        "persona_text": "你是一个友善的群聊机器人。",
        "persona_knowledge": "",
        "group_mode": "whitelist",
        "group_whitelist": [],
        "short_window_size": 8,
        "long_window_size": 20,
        "long_window_top_n": 6,
        "long_window_summarize": False,
        "base_threshold": 0.55,
        "core_interest_modifier": 0.7,
        "general_interest_modifier": 1.0,
        "edge_interest_modifier": 1.3,
        "expecting_modifier": 0.8,
        "personal_threshold": 0.55,
        "hate_similarity_threshold": 0.75,
        "w_int": 1.2,
        "w_topic": 0.4,
        "w_resp": 0.8,
        "w_cooldown": 0.5,
        "w_silence": 0.35,
        "batch_interval_min": 2.0,
        "batch_interval_max": 5.0,
        "cooldown_messages": 4,
        "expecting_duration": 30,
        "personal_track_timeout": 30,
        "track_irrelevant_msgs": 3,
        "embedding_rate_limit_per_min": 30,
        "buffer_max_size": 200,
        "topic_turn_keywords": ["说正事", "别聊了", "换个话题", "停"],
        "schedule": [
            {"start": "09:00", "end": "12:00"},
            {"start": "14:00", "end": "18:00"},
            {"start": "20:00", "end": "23:00"},
        ],
        "schedule_jitter_minutes": 30,
        "poll_interval": 300,
        "poll_jitter": 120,
        "monitoring_duration": 120,
        "group_cooldown": 180,
        # v0.3.7：测试环境禁用主动消息最小间隔（避免破坏连续触发断言）
        "proactive_min_interval": 0,
        "glance_enable": True,
        "glance_group_count": 3,
        "glance_min_score": 0.85,
        "hot_group_msg_limit": 30,
        "silent_group_minutes": 10,
        "replay_speed": 1.0,
        # --- v0.2 双通道融合 / 规则 / 疲劳 / 惯性（与 _conf_schema.json default 一致）---
        "enable_rule_channel": True,
        "enable_vector_channel": True,
        "fusion_weight_rule": 0.4,
        "dynamic_fusion_enabled": False,
        "dynamic_alpha_wake": 0.8,
        "dynamic_alpha_short_expect": 0.2,
        "rule_direct_wakeup_words": [],
        "rule_context_wakeup_words": [],
        "rule_context_threshold": 50,
        "rule_question_enabled": True,
        "rule_question_threshold": 65,
        "rule_score_normalize": 100.0,
        "fatigue_recovery_rate": 0.1,
        "fatigue_limit": 5.0,
        "fatigue_cost_active": 1.2,
        "fatigue_cost_passive": 0.8,
        "fatigue_cost_track": 0.6,
        "fatigue_cost_glance": 1.5,
        "fatigue_high_modifier": 1.2,
        "fatigue_medium_modifier": 1.1,
        "fatigue_suppress_enabled": True,
        "after_reply_probability": 0.7,
        "probability_duration": 30,
        "wait_window_duration_ms": 3000,
        "wait_window_max_extra": 3,
        "proactive_temp_boost": 0.5,
        "proactive_boost_duration": 60,
        # v0.2.5 回复关键词匹配
        "reply_keyword_enabled": True,
        "reply_keyword_top_n": 5,
        "reply_keyword_boost_factor": 0.25,
        "reply_keyword_ttl_seconds": 60,
        "reply_keyword_min_score_to_trigger": 0.5,
        "reply_keyword_early_clear_low_score": 0.1,
        # v0.2.6 兴趣生成 / 长窗口注入
        "interest_example_count": 3,
        "interest_keyword_count": 12,
        "long_window_inject_proactive": True,
        # v0.2.8 管线注入 / 自适应 / 频率上限
        "reply_via_pipeline": True,
        "adaptive_threshold_enabled": True,
        "max_proactive_per_hour": 5,
        "max_proactive_per_day": 20,
        # v0.2.9 LLM 调参全权接管 + 自动触发 + 速率限制
        "autotune_safe_rate_hi": 0.30,
        "autotune_safe_rate_lo": 0.05,
        "autotune_auto_trigger_enabled": True,
        "autotune_auto_apply": False,
        "autotune_min_decisions": 30,
        "autotune_cooldown_hours": 3.0,
        "autotune_max_per_day": 4,
        # v0.3.5 短批次合并 / emoji 过滤 / 强制触发 / 对话状态
        # 注：batch_min_text_length 测试默认 0（禁用短批次合并）——既有测试构造的
        # 批次文本多为 ≤5 字短消息（"符玄"/"hi" 等），若启用会触发回填导致评估
        # 不发生、决策日志为空，破坏 477 既有测试。生产环境默认 12 由
        # ConfigStore.DEFAULT_CONFIG 提供。
        "batch_min_text_length": 0,
        "batch_short_merge_max_attempts": 2,
        "emoji_filter_enabled": True,
        "autotune_force_rate_threshold": 0.50,
        "autotune_force_cooldown_hours": 1.0,
        "conversation_state_enabled": True,
        "conversation_state_window": 10,
        "conversation_state_monologue_ratio": 0.6,
        "conversation_state_argument_msg_len": 20,
    }


@pytest.fixture
def mock_config():
    """可变配置 dict（PRD 默认值）。决策引擎实时读取，测试改后立即生效。"""
    return default_config()


# ======================================================================
# mock_log —— 日志回调
# ======================================================================


class _MockLog:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, level: str, msg: str) -> None:
        self.calls.append((level, msg))

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()

    def by_level(self, level: str) -> list[str]:
        return [msg for lv, msg in self.calls if lv == level]

    def has(self, level: str, substr: str = "") -> bool:
        return any(lv == level and substr in msg for lv, msg in self.calls)


@pytest.fixture
def mock_log():
    return _MockLog()


# ======================================================================
# tmp_data_dir —— 临时数据目录
# ======================================================================


@pytest.fixture
def tmp_data_dir(tmp_path) -> Path:
    """临时数据目录（pytest tmp_path 子目录），测试间隔离。"""
    d = tmp_path / "prosocial_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ======================================================================
# make_interest_data —— InterestData 工厂
# ======================================================================


@pytest.fixture
def make_interest_data():
    """返回构造 InterestData 的辅助函数，便于测试快速搭建兴趣数据。"""
    from core.common.models import InterestData, InterestItem

    def _make(
        centroids: dict[str, list[float]] | None = None,
        weights: dict[str, float] | None = None,
        high_kw: list[str] | None = None,
        hate_kw: list[str] | None = None,
        items: list[InterestItem] | None = None,
        persona_hash: str = "testhash",
        dim: int = 8,
    ) -> InterestData:
        return InterestData(
            centroids=centroids if centroids is not None else {},
            weights=weights
            if weights is not None
            else {"core": 1.5, "general": 1.0, "marginal": 0.6, "hate": 1.0},
            high_interest_keywords=high_kw if high_kw is not None else [],
            hate_keywords=hate_kw if hate_kw is not None else [],
            items=items if items is not None else [],
            persona_hash=persona_hash,
            dim=dim,
        )

    return _make


# ======================================================================
# scheduler_factory —— SocialScheduler 工厂
# ======================================================================


@pytest.fixture
def scheduler_factory(
    mock_config, mock_llm, mock_embed, mock_send, mock_kv, mock_log, tmp_data_dir
):
    """构造 SocialScheduler 的工厂 fixture。

    返回一个可调用对象，调用后返回已注入全部 mock 的 scheduler。
    测试可经 mock_config 改配置、mock_embed 注入向量、mock_kv 模拟 KV。
    工厂每次调用创建全新 scheduler（不共享状态）。
    """
    from core.decision.interest import InterestManager
    from core.storage.ratelimit import TokenBucketRateLimiter
    from core.scheduler import SocialScheduler

    def _make() -> SocialScheduler:
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
        )
        # 模拟 start() 的预加载：group_enable_cache 就绪（默认空 dict = 全部启用）
        sched._group_enable_cache = {}
        return sched

    return _make


# ======================================================================
# 辅助：当前日期字符串（跨日测试用）
# ======================================================================


@pytest.fixture
def today_str():
    return datetime.now().strftime("%Y-%m-%d")
