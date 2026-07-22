"""人设兴趣管理（模块 B 产出）。

职责：
1. 启动时根据人设文本调用 1 次 LLM 生成兴趣语料 JSON（PRD 附录 A）。
2. 收集所有级别兴趣的示例句，1 次批量嵌入，按级别求均值质心。
3. 持久化到 ``data/plugin_data/astrbot_plugin_proactive_social/interests.npz``。
4. 再次加载时若 persona 哈希未变则直接读盘，避免重复 LLM/嵌入调用。

本模块禁止 import astrbot，LLM / 嵌入 / 日志能力全部通过注入回调使用，
保证可离线单元测试。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

from .models import InterestData, InterestItem, InterestLevel
from .prompts import build_interest_prompt

# 人设文本为空时使用的内置默认人设（PRD §6.8）
_DEFAULT_PERSONA_TEXT = "你是一个友善的群聊机器人。"

# 内置最小默认兴趣集：LLM 调用 / JSON 解析全部失败时兜底，确保引擎可用
_DEFAULT_INTERESTS_PAYLOAD: dict[str, Any] = {
    "interests": [
        {
            "label": "core",
            "topic": "群聊日常",
            "examples": ["今天聊点啥？", "有人在吗？"],
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
    "hate_keywords": [],
    "high_interest_keywords": ["有人在吗", "聊聊"],
}

# 各级别默认权重（LLM 输出缺项或解析失败时回退用）
_LEVEL_DEFAULT_WEIGHT: dict[str, float] = {
    InterestLevel.CORE.value: 1.5,
    InterestLevel.GENERAL.value: 1.0,
    InterestLevel.MARGINAL.value: 0.6,
    InterestLevel.HATE.value: 1.0,
}

# 按固定顺序遍历四个级别（与 npz 字段命名、向量切片顺序保持一致）
_LEVEL_ORDER: tuple[InterestLevel, ...] = (
    InterestLevel.CORE,
    InterestLevel.GENERAL,
    InterestLevel.MARGINAL,
    InterestLevel.HATE,
)


def _compute_persona_hash(persona_text: str, persona_knowledge: str) -> str:
    """计算人设文本哈希（sha256 前 16 位）。

    persona_text 与 persona_knowledge 用 ``\\n|||\\n`` 分隔，确保两段文本
    边界明确（避免拼接歧义导致 hash 碰撞）。
    """
    raw = (persona_text + "\n|||\n" + persona_knowledge).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _strip_json_fence(text: str) -> str:
    """去除 LLM 输出常见的 ```json ... ``` 围栏，返回内部 JSON 文本。"""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _parse_interests_json(raw: str) -> dict | None:
    """解析 LLM 输出为兴趣 dict；失败返回 None（不抛异常）。"""
    try:
        cleaned = _strip_json_fence(raw)
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _build_items_from_payload(payload: dict, log_fn) -> list[InterestItem]:
    """从 LLM/默认 payload 解析为 InterestItem 列表。

    - label 必须是 core/general/marginal/hate 之一，非法项丢弃并 log warning
    - examples / weight 做容错转换，缺项用默认值
    """
    items: list[InterestItem] = []
    level_map = {lv.value: lv for lv in InterestLevel}
    for raw_item in payload.get("interests", []):
        if not isinstance(raw_item, dict):
            continue
        label = raw_item.get("label")
        if label not in level_map:
            log_fn("warning", f"[ProSocial] interest.py: 非法 label {label!r} 已丢弃")
            continue
        topic = str(raw_item.get("topic", "")).strip()
        examples_raw = raw_item.get("examples", [])
        if not isinstance(examples_raw, list):
            examples_raw = []
        examples = [str(x) for x in examples_raw if x is not None]
        try:
            weight = float(raw_item.get("weight", _LEVEL_DEFAULT_WEIGHT[label]))
        except (TypeError, ValueError):
            weight = _LEVEL_DEFAULT_WEIGHT[label]
        items.append(
            InterestItem(
                level=level_map[label],
                topic=topic,
                examples=examples,
                weight=weight,
            )
        )
    return items


class InterestManager:
    """人设兴趣管理器：生成、向量化、持久化、加载。

    生命周期：
        ensure_loaded() —— 启动时调用，命中持久化则直接加载，否则 regenerate
        regenerate()    —— 强制重建（/prosocial persona reload）
        get()           —— 取当前内存中的 InterestData
        summary()       —— 输出摘要供 persona show / Dashboard
    """

    def __init__(self, data_dir: Path, log_fn: Callable[[str, str], None]):
        """初始化。

        data_dir : 持久化目录（data/plugin_data/astrbot_plugin_proactive_social/）
        log_fn   : 日志回调 (level, msg)，level ∈ info/warning/error/debug
        """
        self._data_dir = data_dir
        self._npz_path = data_dir / "interests.npz"
        self.log = log_fn
        self._data: InterestData | None = None
        # 确保持久化目录存在（不存在则创建，失败仅 log 不抛）
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("error", f"[ProSocial] interest.py: 创建目录失败 {data_dir}: {e}")

    async def ensure_loaded(
        self,
        persona_text: str,
        persona_knowledge: str,
        llm_fn: Callable[[str], Awaitable[str]],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> InterestData:
        """有持久化且 persona 哈希未变 -> 直接加载；否则 regenerate。

        - 人设文本为空时使用内置默认人设计算 hash（PRD §6.8）
        - 已加载且 hash 一致 -> 直接返回内存数据
        - interests.npz 存在且 hash 匹配 -> 读盘还原，不触发 LLM
        - 任何加载异常 -> log warning 并 fallback 到 regenerate
        """
        effective_persona = self._effective_persona(persona_text)
        persona_hash = _compute_persona_hash(effective_persona, persona_knowledge)

        # 内存命中
        if self._data is not None and self._data.persona_hash == persona_hash:
            return self._data

        # 尝试读盘
        if self._npz_path.exists():
            try:
                data = self._load_npz(persona_hash)
                if data is not None:
                    self._data = data
                    self.log(
                        "info",
                        "[ProSocial] interest.py: 命中持久化，直接加载 interests.npz",
                    )
                    return data
                # hash 不匹配 -> 落到 regenerate
                self.log(
                    "info",
                    "[ProSocial] interest.py: persona 哈希变化，重新生成兴趣数据",
                )
            except Exception as e:
                self.log(
                    "warning",
                    f"[ProSocial] interest.py: 加载 interests.npz 失败，回退重建: {e}",
                )

        return await self.regenerate(persona_text, persona_knowledge, llm_fn, embed_fn)

    async def regenerate(
        self,
        persona_text: str,
        persona_knowledge: str,
        llm_fn: Callable[[str], Awaitable[str]],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> InterestData:
        """1 次 LLM（build_interest_prompt）+ 1 次批量 embed -> 各级别质心 -> 存 interests.npz。

        流程：
          1. LLM 生成兴趣 JSON（最多 2 次尝试，仍失败用内置默认兴趣集兜底）
          2. 解析为 InterestItem 列表（非法 label 丢弃）
          3. 收集所有 examples，一次性批量嵌入
          4. 按级别分组求均值质心（空 examples 级别质心为 None）
          5. 构造 InterestData 并持久化
        """
        effective_persona = self._effective_persona(persona_text)
        persona_hash = _compute_persona_hash(effective_persona, persona_knowledge)

        # 1. LLM 生成兴趣语料（带 1 次重试 + 兜底）
        payload = await self._gen_payload_with_retry(
            effective_persona, persona_knowledge, llm_fn
        )

        # 2. 解析为 InterestItem 列表
        items = _build_items_from_payload(payload, self.log)

        # 3. 收集所有 examples 并按 level 分组
        level_examples: dict[str, list[str]] = {lv.value: [] for lv in _LEVEL_ORDER}
        all_examples: list[str] = []
        for it in items:
            for ex in it.examples:
                if ex:
                    level_examples[it.level.value].append(ex)
                    all_examples.append(ex)

        # 4. 批量嵌入（1 次）
        embeddings: list[list[float]] = []
        if all_examples:
            try:
                embeddings = await embed_fn(all_examples)
            except Exception as e:
                self.log("warning", f"[ProSocial] interest.py: 批量嵌入失败: {e}")
                embeddings = []
        dim = len(embeddings[0]) if embeddings else 0

        # 5. 按级别分组求均值质心（按 all_examples 拼装顺序切片）
        centroids: dict[str, list[float]] = {}
        idx = 0
        for lv in _LEVEL_ORDER:
            ex_list = level_examples[lv.value]
            if not ex_list or dim == 0:
                continue
            count = len(ex_list)
            slice_emb = embeddings[idx : idx + count]
            idx += count
            if not slice_emb:
                continue
            arr = np.asarray(slice_emb, dtype=np.float64)
            centroid = arr.mean(axis=0)
            centroids[lv.value] = [float(x) for x in centroid.tolist()]

        # 6. 构造 weights dict（每级别取其第一个 item 的 weight，无则用默认）
        weights: dict[str, float] = {}
        for lv in _LEVEL_ORDER:
            level_items = [it for it in items if it.level == lv]
            if level_items:
                weights[lv.value] = float(level_items[0].weight)
            else:
                weights[lv.value] = _LEVEL_DEFAULT_WEIGHT[lv.value]

        # 7. 关键词（确保是字符串列表）
        high_kw = [
            str(x) for x in payload.get("high_interest_keywords", []) if x is not None
        ]
        hate_kw = [str(x) for x in payload.get("hate_keywords", []) if x is not None]

        # 8. 构造 InterestData
        data = InterestData(
            centroids=centroids,
            weights=weights,
            high_interest_keywords=high_kw,
            hate_keywords=hate_kw,
            items=items,
            persona_hash=persona_hash,
            dim=dim,
        )

        # 9. 持久化（失败仅 log，不影响内存数据返回）
        try:
            self._save_npz(data)
        except Exception as e:
            self.log(
                "warning", f"[ProSocial] interest.py: 持久化 interests.npz 失败: {e}"
            )

        self._data = data
        self.log(
            "info",
            f"[ProSocial] interest.py: 兴趣数据已重建 "
            f"(hash={persona_hash}, dim={dim}, items={len(items)})",
        )
        return data

    def get(self) -> InterestData | None:
        """返回当前已加载的 InterestData（可能为 None）。"""
        return self._data

    def summary(self) -> dict:
        """返回摘要结构，供 /prosocial persona show 与 Dashboard 展示。"""
        data = self._data
        if data is None:
            return {
                "persona_hash": "",
                "dim": 0,
                "levels": {
                    lv.value: {"count": 0, "weight": 0.0, "topics": []}
                    for lv in _LEVEL_ORDER
                },
                "high_interest_keywords": [],
                "hate_keywords": [],
                "loaded": False,
            }

        levels: dict[str, dict] = {}
        for lv in _LEVEL_ORDER:
            level_items = [it for it in data.items if it.level == lv]
            weight = data.weights.get(lv.value, _LEVEL_DEFAULT_WEIGHT[lv.value])
            levels[lv.value] = {
                "count": len(level_items),
                "weight": float(weight),
                "topics": [it.topic for it in level_items],
            }

        return {
            "persona_hash": data.persona_hash,
            "dim": int(data.dim),
            "levels": levels,
            "high_interest_keywords": list(data.high_interest_keywords),
            "hate_keywords": list(data.hate_keywords),
            "loaded": True,
        }

    # ------------------------------------------------------------------ #
    # 内部辅助方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _effective_persona(persona_text: str) -> str:
        """空人设回退为内置默认人设（PRD §6.8）。"""
        if persona_text and persona_text.strip():
            return persona_text.strip()
        return _DEFAULT_PERSONA_TEXT

    async def _gen_payload_with_retry(
        self,
        persona_text: str,
        persona_knowledge: str,
        llm_fn: Callable[[str], Awaitable[str]],
    ) -> dict:
        """调用 LLM 生成兴趣 JSON，最多重试 1 次；仍失败用内置默认兴趣集兜底。"""
        prompt = build_interest_prompt(persona_text, persona_knowledge)
        for attempt in range(2):
            try:
                raw = await llm_fn(prompt)
                payload = _parse_interests_json(raw)
                if payload is not None:
                    return payload
                self.log(
                    "warning",
                    f"[ProSocial] interest.py: LLM 输出 JSON 解析失败 (attempt={attempt + 1})",
                )
            except Exception as e:
                self.log(
                    "warning",
                    f"[ProSocial] interest.py: LLM 调用失败 (attempt={attempt + 1}): {e}",
                )
        self.log("warning", "[ProSocial] interest.py: 使用内置默认兴趣集兜底")
        return _DEFAULT_INTERESTS_PAYLOAD

    def _load_npz(self, expected_hash: str) -> InterestData | None:
        """从 interests.npz 还原 InterestData；hash 不匹配返回 None。

        存储格式（与 _save_npz 对应）：
          - meta             : 0-d numpy.str_ 数组，内容为 JSON 字符串
          - centroid_<level> : 1-d float64 数组；无质心时为零长度数组
        meta JSON 字段：
          persona_hash / dim / weights / high_interest_keywords /
          hate_keywords / has_centroid / items
        """
        with np.load(self._npz_path, allow_pickle=False) as npz:
            # 0-d 字符串数组用 .item() 取出原始 str
            meta_raw = npz["meta"].item()
            meta = json.loads(meta_raw)

            if meta.get("persona_hash") != expected_hash:
                return None

            dim = int(meta.get("dim", 0))
            weights = {k: float(v) for k, v in meta.get("weights", {}).items()}
            high_kw = list(meta.get("high_interest_keywords", []))
            hate_kw = list(meta.get("hate_keywords", []))
            has_centroid = dict(meta.get("has_centroid", {}))

            # 还原质心
            centroids: dict[str, list[float]] = {}
            for lv in _LEVEL_ORDER:
                if not has_centroid.get(lv.value, False):
                    continue
                vec = npz[f"centroid_{lv.value}"]
                if vec.size == 0:
                    continue
                centroids[lv.value] = [float(x) for x in vec.tolist()]

            # 还原 items
            level_map = {lv.value: lv for lv in InterestLevel}
            items: list[InterestItem] = []
            for it in meta.get("items", []):
                lv = level_map.get(it.get("level"))
                if lv is None:
                    continue
                try:
                    weight = float(it.get("weight", _LEVEL_DEFAULT_WEIGHT[lv.value]))
                except (TypeError, ValueError):
                    weight = _LEVEL_DEFAULT_WEIGHT[lv.value]
                items.append(
                    InterestItem(
                        level=lv,
                        topic=str(it.get("topic", "")),
                        examples=list(it.get("examples", [])),
                        weight=weight,
                    )
                )

        return InterestData(
            centroids=centroids,
            weights=weights,
            high_interest_keywords=high_kw,
            hate_keywords=hate_kw,
            items=items,
            persona_hash=meta.get("persona_hash", ""),
            dim=dim,
        )

    def _save_npz(self, data: InterestData) -> None:
        """持久化 InterestData 到 interests.npz。

        字段布局见 _load_npz 文档；向量字段单独存为 numpy 数组，元数据
        统一存为 meta JSON 字符串字段，加载逻辑清晰且能完整往返。
        """
        has_centroid = {
            lv.value: (
                lv.value in data.centroids and data.centroids[lv.value] is not None
            )
            for lv in _LEVEL_ORDER
        }
        meta = {
            "persona_hash": data.persona_hash,
            "dim": int(data.dim),
            "weights": {k: float(v) for k, v in data.weights.items()},
            "high_interest_keywords": list(data.high_interest_keywords),
            "hate_keywords": list(data.hate_keywords),
            "has_centroid": has_centroid,
            "items": [
                {
                    "level": it.level.value,
                    "topic": it.topic,
                    "examples": list(it.examples),
                    "weight": float(it.weight),
                }
                for it in data.items
            ],
        }
        meta_str = json.dumps(meta, ensure_ascii=False)

        save_kwargs: dict[str, Any] = {"meta": np.array(meta_str)}
        for lv in _LEVEL_ORDER:
            key = f"centroid_{lv.value}"
            if has_centroid.get(lv.value):
                save_kwargs[key] = np.asarray(
                    data.centroids[lv.value], dtype=np.float64
                )
            else:
                save_kwargs[key] = np.array([], dtype=np.float64)

        np.savez(self._npz_path, **save_kwargs)
