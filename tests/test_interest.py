"""test_interest.py —— B 人设兴趣管理。

测试对象：core/interest.py → InterestManager
覆盖点：
- regenerate：mock LLM 成功、JSON 围栏解析、非法 JSON 兜底、embed 失败容错、质心按级别分组
- ensure_loaded：哈希命中（不触发 LLM）、哈希不匹配（重新生成）、npz 往返一致
- summary：loaded / not loaded
- 空人设使用默认人设
- 非法 label 丢弃

对应 PRD §8.1（兴趣分级）、F1（人设兴趣管理）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.decision.interest import (
    InterestManager,
    _compute_persona_hash,
    _parse_interests_json,
    _strip_json_fence,
)
from core.common.models import InterestData, InterestItem, InterestLevel


# ---------------------------------------------------------------------- #
# 辅助函数
# ---------------------------------------------------------------------- #

def test_interest_strip_json_fence_with_fence():
    """去除 ```json ... ``` 围栏。"""
    raw = "```json\n{\"a\": 1}\n```"
    assert _strip_json_fence(raw) == '{"a": 1}'


def test_interest_strip_json_fence_without_fence():
    """无围栏时返回 strip 后原文。"""
    assert _strip_json_fence('{"a": 1}') == '{"a": 1}'


def test_interest_parse_interests_json_valid():
    obj = _parse_interests_json('{"a": 1}')
    assert obj == {"a": 1}


def test_interest_parse_interests_json_invalid_returns_none():
    assert _parse_interests_json("not json") is None
    assert _parse_interests_json("[1,2]") is None  # 非 dict


def test_interest_compute_persona_hash_stable():
    """相同输入哈希一致；不同输入哈希不同。"""
    h1 = _compute_persona_hash("persona", "knowledge")
    h2 = _compute_persona_hash("persona", "knowledge")
    h3 = _compute_persona_hash("persona2", "knowledge")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16  # sha256 前 16 位


def test_interest_compute_persona_hash_boundary_delimiter():
    """边界分隔符避免拼接歧义。"""
    # "ab" + "|||" + "cd" 与 "a" + "|||" + "bcd" 哈希不同
    h1 = _compute_persona_hash("ab", "cd")
    h2 = _compute_persona_hash("a", "bcd")
    assert h1 != h2


# ---------------------------------------------------------------------- #
# regenerate
# ---------------------------------------------------------------------- #

def test_interest_regenerate_success(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """regenerate 成功：1 次 LLM + 1 次 embed，生成 InterestData。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(
        mgr.regenerate("人设", "知识", mock_llm, mock_embed)
    )
    assert isinstance(data, InterestData)
    assert mock_llm.call_count == 1
    assert mock_embed.call_count == 1
    # 默认 LLM JSON 含 4 级，每级 2 examples → 8 条嵌入
    assert data.dim == 8  # mock_embed dim
    # 各级别质心已计算
    assert "core" in data.centroids
    assert "general" in data.centroids
    assert "marginal" in data.centroids
    assert "hate" in data.centroids
    # 权重正确
    assert data.weights["core"] == 1.5
    assert data.weights["marginal"] == 0.6
    # 关键词
    assert "符玄" in data.high_interest_keywords
    assert "骂人" in data.hate_keywords
    # items 4 个
    assert len(data.items) == 4
    # 持久化文件存在
    assert (tmp_data_dir / "interests.npz").exists()


def test_interest_regenerate_json_with_fence(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """LLM 输出带 ```json 围栏也能正确解析。"""
    fenced = "```json\n" + json.dumps({
        "interests": [
            {"label": "core", "topic": "t", "examples": ["e1"], "weight": 1.5},
            {"label": "hate", "topic": "h", "examples": ["h1"], "weight": 1.0},
        ],
        "hate_keywords": [],
        "high_interest_keywords": ["e1"],
    }, ensure_ascii=False) + "\n```"
    mock_llm.set_return_value(fenced)
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert len(data.items) == 2
    assert data.items[0].level == InterestLevel.CORE


def test_interest_regenerate_invalid_json_fallback(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """LLM 输出非法 JSON → 重试 1 次仍失败 → 用内置默认兴趣集兜底，不抛异常。"""
    mock_llm.set_return_value("not a json at all")
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    # 兜底默认兴趣集有 4 级
    assert len(data.items) == 4
    # LLM 调用 2 次（初始 + 重试）
    assert mock_llm.call_count == 2
    # 应有 warning 日志
    assert mock_log.has("warning")


def test_interest_regenerate_llm_exception_fallback(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """LLM 调用抛异常 → 重试仍失败 → 兜底。"""
    mock_llm.set_fail_mode(True)
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert len(data.items) == 4  # 默认兜底
    assert mock_llm.call_count == 2


def test_interest_regenerate_embed_failure_no_crash(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """embed 失败 → dim=0，无质心，不抛异常。"""
    mock_embed.set_fail_mode(True)
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert data.dim == 0
    assert data.centroids == {}
    # items 仍正常解析
    assert len(data.items) == 4
    assert mock_log.has("warning")


def test_interest_regenerate_centroids_grouped_by_level(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """质心按级别分组求均值：core 2 个 examples 的均值向量。"""
    # 注入特定向量：让 core 的 2 个 examples 嵌入为 [2,0] 和 [4,0] → 均值 [3,0]
    mock_embed.set("符玄怎么配队？", [2.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    mock_embed.set("量子队现版本还强吗？", [4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    core_centroid = data.centroids["core"]
    assert core_centroid[0] == pytest.approx(3.0)


def test_interest_regenerate_invalid_label_discarded(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """非法 label 丢弃并 log warning。"""
    mock_llm.set_return_value(json.dumps({
        "interests": [
            {"label": "core", "topic": "t", "examples": ["e1"], "weight": 1.5},
            {"label": "unknown", "topic": "x", "examples": ["e2"], "weight": 1.0},
        ],
        "hate_keywords": [],
        "high_interest_keywords": [],
    }, ensure_ascii=False))
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert len(data.items) == 1  # unknown 丢弃
    assert mock_log.has("warning")


def test_interest_regenerate_persists_npz(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """regenerate 后 interests.npz 存在。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert (tmp_data_dir / "interests.npz").exists()


# ---------------------------------------------------------------------- #
# ensure_loaded（哈希命中 / 不匹配）
# ---------------------------------------------------------------------- #

def test_interest_ensure_loaded_hash_hit_no_llm(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """哈希命中 → 直接加载 npz，不触发 LLM。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    # 第一次：生成并持久化
    asyncio.run(mgr.regenerate("人设A", "知识A", mock_llm, mock_embed))
    assert mock_llm.call_count == 1
    # 第二次：新 manager，哈希相同 → 加载 npz，不调 LLM
    mgr2 = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr2.ensure_loaded("人设A", "知识A", mock_llm, mock_embed))
    assert mock_llm.call_count == 1  # 未增加
    assert isinstance(data, InterestData)
    assert len(data.items) == 4


def test_interest_ensure_loaded_hash_mismatch_triggers_regenerate(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """哈希不匹配 → 重新生成（触发 LLM）。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("人设A", "知识A", mock_llm, mock_embed))
    assert mock_llm.call_count == 1
    mgr2 = InterestManager(tmp_data_dir, mock_log)
    # 人设变了 → 哈希不匹配 → regenerate
    asyncio.run(mgr2.ensure_loaded("人设B", "知识A", mock_llm, mock_embed))
    assert mock_llm.call_count == 2


def test_interest_ensure_loaded_memory_cache(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """同一 manager 内存命中（_data 已加载且哈希一致）→ 不读盘不调 LLM。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.ensure_loaded("人设X", "知识X", mock_llm, mock_embed))
    assert mock_llm.call_count == 1
    # 再次调用相同人设 → 内存命中
    asyncio.run(mgr.ensure_loaded("人设X", "知识X", mock_llm, mock_embed))
    assert mock_llm.call_count == 1


def test_interest_ensure_loaded_empty_persona_uses_default(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """空人设使用内置默认人设计算哈希（PRD §6.8），不报错。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.ensure_loaded("", "", mock_llm, mock_embed))
    assert isinstance(data, InterestData)
    # 两次空人设应哈希一致
    asyncio.run(mgr.ensure_loaded("", "", mock_llm, mock_embed))
    assert mock_llm.call_count == 1  # 第二次内存命中


# ---------------------------------------------------------------------- #
# npz 往返
# ---------------------------------------------------------------------- #

def test_interest_npz_roundtrip(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """save → load 往返一致：质心、权重、关键词、items、哈希、dim。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    original = asyncio.run(mgr.regenerate("往返人设", "往返知识", mock_llm, mock_embed))
    # 新 manager 加载
    mgr2 = InterestManager(tmp_data_dir, mock_log)
    loaded = asyncio.run(mgr2.ensure_loaded("往返人设", "往返知识", mock_llm, mock_embed))
    assert loaded.persona_hash == original.persona_hash
    assert loaded.dim == original.dim
    assert loaded.weights == original.weights
    assert loaded.high_interest_keywords == original.high_interest_keywords
    assert loaded.hate_keywords == original.hate_keywords
    assert list(loaded.centroids.keys()) == list(original.centroids.keys())
    # 质心数值一致
    for lv in original.centroids:
        assert loaded.centroids[lv] == pytest.approx(original.centroids[lv], rel=1e-6)
    # items 一致
    assert len(loaded.items) == len(original.items)
    for a, b in zip(loaded.items, original.items):
        assert a.level == b.level
        assert a.topic == b.topic
        assert a.weight == b.weight


# ---------------------------------------------------------------------- #
# summary
# ---------------------------------------------------------------------- #

def test_interest_summary_loaded(mock_llm, mock_embed, mock_log, tmp_data_dir):
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    s = mgr.summary()
    assert s["loaded"] is True
    assert s["dim"] == 8
    assert "core" in s["levels"]
    assert s["levels"]["core"]["weight"] == 1.5
    assert "符玄" in s["high_interest_keywords"]


def test_interest_summary_not_loaded(mock_log, tmp_data_dir):
    mgr = InterestManager(tmp_data_dir, mock_log)
    s = mgr.summary()
    assert s["loaded"] is False
    assert s["dim"] == 0
    assert s["persona_hash"] == ""


def test_interest_get_returns_none_before_load(mock_log, tmp_data_dir):
    mgr = InterestManager(tmp_data_dir, mock_log)
    assert mgr.get() is None
