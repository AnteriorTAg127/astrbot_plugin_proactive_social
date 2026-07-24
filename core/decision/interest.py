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

from ..common.models import InterestData, InterestItem, InterestLevel
from ..common.prompts import build_interest_prompt

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


def _compute_persona_hash(
    persona_text: str,
    persona_knowledge: str,
    example_count: int = 3,
    keyword_count: int = 12,
) -> str:
    """计算人设+数量的稳定哈希（sha256 前 16 位）。

    v0.2.8：example_count/keyword_count 纳入哈希输入，避免改数量后命中旧缓存。

    将 persona_text / persona_knowledge / example_count / keyword_count
    一并纳入哈希输入：任一变更都会让内存缓存与 npz 磁盘缓存失效。
    兼容默认参数（example_count=3 / keyword_count=12），旧调用点不受影响。
    persona_text 与 persona_knowledge 用 ``\\n|||\\n`` 分隔，确保两段文本
    边界明确（避免拼接歧义导致 hash 碰撞）。
    """
    payload = (
        f"{persona_text}\n|||\n{persona_knowledge}\n|||\n"
        f"{int(example_count)}\n|||\n{int(keyword_count)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
        # 人工过滤列表（F20）：examples 是 [{label,text}]，keywords 是 [text]。
        # main.py 启动时从 KV "interest_rejected" 加载，regenerate/apply_rejected 据此排除。
        self._rejected: dict[str, list] = {"examples": [], "keywords": []}
        # 确保持久化目录存在（不存在则创建，失败仅 log 不抛）
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("error", f"[ProSocial] interest.py: 创建目录失败 {data_dir}: {e}")

    # ------------------------------------------------------------------ #
    # 人工过滤（F20）：rejected 列表管理
    # ------------------------------------------------------------------ #
    def set_rejected(self, rejected: dict) -> None:
        """从 KV 加载后调用，设置 rejected 列表。

        v0.3.6：keywords 格式从 ``[str]`` 迁移为 ``[{"text": str, "kind": str}]``，
        kind ∈ "high_keyword" | "hate_keyword" | ""。旧格式字符串自动迁移为
        ``{"text": <str>, "kind": ""}``，保证向后兼容。

        容错：非 dict / 缺字段时回退为空结构，不抛异常。
        """
        if isinstance(rejected, dict):
            # 迁移 keywords：旧格式（str）→ 新格式（dict）
            raw_keywords = list(rejected.get("keywords", []) or [])
            migrated_keywords: list[dict] = []
            for k in raw_keywords:
                if isinstance(k, dict):
                    migrated_keywords.append(k)
                elif isinstance(k, str):
                    migrated_keywords.append({"text": k, "kind": ""})
            self._rejected = {
                "examples": list(rejected.get("examples", []) or []),
                "keywords": migrated_keywords,
            }
        else:
            self._rejected = {"examples": [], "keywords": []}

    def get_rejected(self) -> dict:
        """返回当前 rejected 列表（浅拷贝，外部修改不污染内部）。"""
        return {
            "examples": list(self._rejected.get("examples", [])),
            "keywords": list(self._rejected.get("keywords", [])),
        }

    async def ensure_loaded(
        self,
        persona_text: str,
        persona_knowledge: str,
        llm_fn: Callable[[str], Awaitable[str]],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
        example_count: int = 3,
        keyword_count: int = 12,
    ) -> InterestData:
        """有持久化且 persona 哈希未变 -> 直接加载；否则 regenerate。

        - 人设文本为空时使用内置默认人设计算 hash（PRD §6.8）
        - 已加载且 hash 一致 -> 直接返回内存数据
        - interests.npz 存在且 hash 匹配 -> 读盘还原，不触发 LLM
        - 任何加载异常 -> log warning 并 fallback 到 regenerate
        """
        effective_persona = self._effective_persona(persona_text)
        persona_hash = _compute_persona_hash(
            effective_persona, persona_knowledge, example_count, keyword_count
        )

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

        return await self.regenerate(
            persona_text,
            persona_knowledge,
            llm_fn,
            embed_fn,
            example_count=example_count,
            keyword_count=keyword_count,
        )

    async def regenerate(
        self,
        persona_text: str,
        persona_knowledge: str,
        llm_fn: Callable[[str], Awaitable[str]],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
        example_count: int = 3,
        keyword_count: int = 12,
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
        persona_hash = _compute_persona_hash(
            effective_persona, persona_knowledge, example_count, keyword_count
        )

        # 1. LLM 生成兴趣语料（带 1 次重试 + 兜底）
        payload = await self._gen_payload_with_retry(
            effective_persona,
            persona_knowledge,
            llm_fn,
            example_count=example_count,
            keyword_count=keyword_count,
        )

        # 2. 解析为 InterestItem 列表
        items = _build_items_from_payload(payload, self.log)

        # 2.5 关键词（确保是字符串列表）
        high_kw = [
            str(x) for x in payload.get("high_interest_keywords", []) if x is not None
        ]
        hate_kw = [str(x) for x in payload.get("hate_keywords", []) if x is not None]

        # 2.6 过滤 rejected（regenerate 也排除已 rejected 的项，换人设重生成时不回来）
        items, high_kw, hate_kw = self._filter_rejected(items, high_kw, hate_kw)

        # 3-5. 计算质心（复用 _recompute_centroids）
        centroids, dim = await self._recompute_centroids(items, embed_fn)

        # 6. 构造 weights dict（每级别取其第一个 item 的 weight，无则用默认）
        weights: dict[str, float] = {}
        for lv in _LEVEL_ORDER:
            level_items = [it for it in items if it.level == lv]
            if level_items:
                weights[lv.value] = float(level_items[0].weight)
            else:
                weights[lv.value] = _LEVEL_DEFAULT_WEIGHT[lv.value]

        # 7. 构造 InterestData
        data = InterestData(
            centroids=centroids,
            weights=weights,
            high_interest_keywords=high_kw,
            hate_keywords=hate_kw,
            items=items,
            persona_hash=persona_hash,
            dim=dim,
        )

        # 8. 持久化（失败仅 log，不影响内存数据返回）
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
    # 人工过滤（F20）：export_view / reject / apply_rejected
    # ------------------------------------------------------------------ #
    def export_view(self) -> dict:
        """返回纯文本兴趣数据（不含向量/质心），供前端 Tab4 展示。

        结构：
          generated             : 是否已生成（self._data 非 None）
          persona_hash          : 人设哈希
          items                 : [{label,topic,examples,weight}]，4 级
          hate_keywords         : 反感关键词
          high_interest_keywords: 高唤醒关键词
          rejected              : 人工过滤列表 {examples:[{label,text}], keywords:[text]}
        """
        data = self._data
        if data is None:
            return {
                "generated": False,
                "persona_hash": "",
                "items": [],
                "hate_keywords": [],
                "high_interest_keywords": [],
                "rejected": self.get_rejected(),
            }
        items_view = [
            {
                "label": it.level.value,
                "topic": it.topic,
                "examples": list(it.examples),
                "weight": float(it.weight),
            }
            for it in data.items
        ]
        return {
            "generated": True,
            "persona_hash": data.persona_hash,
            "items": items_view,
            "hate_keywords": list(data.hate_keywords),
            "high_interest_keywords": list(data.high_interest_keywords),
            "rejected": self.get_rejected(),
        }

    def reject(self, kind: str, label: str = "", text: str = "") -> tuple[bool, str]:
        """v0.3.6：立即从 items/keywords 移除 + 加入 rejected 列表（同步内存操作）。

        kind=="example"       : 按 (label, text) 去重加入 examples，调 _remove_from_active
        kind=="keyword"       : 检测 text 在 high_interest_keywords 还是 hate_keywords，
                                存储对应 kind，调 _remove_from_active 从两个列表都移除
        kind=="high_keyword"  : 存储 kind="high_keyword"，调 _remove_from_active 从
                                high_interest_keywords 移除
        kind=="hate_keyword"  : 存储 kind="hate_keyword"，调 _remove_from_active 从
                                hate_keywords 移除
        非法 kind 返回 (False, msg)。
        质心重算由调用方（web_bridge）触发后台任务，不在此方法内执行。
        未生成数据时仍加入 rejected（供 regenerate 排除），_remove_from_active 容错跳过。
        """
        if kind == "example":
            existing = {
                (e.get("label", ""), e.get("text", ""))
                for e in self._rejected.get("examples", [])
                if isinstance(e, dict)
            }
            if (label, text) not in existing:
                self._rejected["examples"].append({"label": label, "text": text})
        elif kind == "keyword":
            # 前端过滤按钮：检测 text 在 high 还是 hate 列表，存储对应 kind
            detected_kind = ""
            if self._data is not None:
                if text in self._data.high_interest_keywords:
                    detected_kind = "high_keyword"
                elif text in self._data.hate_keywords:
                    detected_kind = "hate_keyword"
            existing_texts = {
                k.get("text", "")
                for k in self._rejected.get("keywords", [])
                if isinstance(k, dict)
            }
            if text and text not in existing_texts:
                self._rejected["keywords"].append({"text": text, "kind": detected_kind})
        elif kind in ("high_keyword", "hate_keyword"):
            # remove_item/batch_update 调用：存储对应 kind
            existing_texts = {
                k.get("text", "")
                for k in self._rejected.get("keywords", [])
                if isinstance(k, dict)
            }
            if text and text not in existing_texts:
                self._rejected["keywords"].append({"text": text, "kind": kind})
        else:
            return False, f"未知 kind: {kind}"
        # v0.3.6：立即从 active items/keywords 移除（即时反映到前端表格）
        self._remove_from_active(kind, label, text)
        return True, ""

    def restore(self, kind: str, label: str = "", text: str = "") -> tuple[bool, str]:
        """v0.3.6 F2：从 rejected 移除 + 加回 items/keywords（同步内存操作）。

        与 reject 互逆：已过滤项可手动恢复，适用于人类操作和 LLM 操作产生的过滤项。
        kind=="example" : 从 _rejected["examples"] 移除，调 _add_back_to_active 加回 items
        kind=="keyword" : 从 _rejected["keywords"] 查找 text，读取存储的 kind，
                          调 _add_back_to_active 加回对应列表。
                          kind="" 默认加回 high_interest_keywords。
        质心重算由调用方（web_bridge）触发后台任务，不在此方法内执行。
        返回 (ok, msg)；未生成数据时仅从 rejected 移除（下次 regenerate 会包含）。
        """
        if kind == "example":
            before = len(self._rejected.get("examples", []))
            self._rejected["examples"] = [
                e
                for e in self._rejected.get("examples", [])
                if not (
                    isinstance(e, dict)
                    and e.get("label", "") == label
                    and e.get("text", "") == text
                )
            ]
            if len(self._rejected["examples"]) == before:
                return False, f"未找到要恢复的 example: label={label}, text={text}"
            self._add_back_to_active("example", label, text)
        elif kind == "keyword":
            # 查找存储的 kind，决定加回哪个列表
            stored_kind = ""
            found = False
            for k in self._rejected.get("keywords", []):
                if isinstance(k, dict) and k.get("text", "") == text:
                    stored_kind = k.get("kind", "")
                    found = True
                    break
            if not found:
                return False, f"未找到要恢复的 keyword: {text}"
            self._rejected["keywords"] = [
                k
                for k in self._rejected.get("keywords", [])
                if not (isinstance(k, dict) and k.get("text", "") == text)
            ]
            # stored_kind="hate_keyword" → 加回 hate_keywords
            # stored_kind="high_keyword" 或 "" → 加回 high_interest_keywords（默认）
            if stored_kind == "hate_keyword":
                self._add_back_to_active("hate_keyword", "", text)
            else:
                self._add_back_to_active("high_keyword", "", text)
        else:
            return False, f"未知 kind: {kind}"
        return True, ""

    async def apply_rejected(
        self,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[bool, str]:
        """v0.3.6：重算质心兜底（reject 已即时移除，此方法主要触发质心重算 + 持久化）。

        - self._data 为 None → (False, "尚未生成兴趣数据")
        - 成功 → (True, "")，更新 self._data 并 _save_npz 持久化
        - 异常 → (False, str(e))，不修改 self._data
        """
        if self._data is None:
            return False, "尚未生成兴趣数据"
        try:
            data = self._data
            # v0.3.6：reject 已即时移除，_filter_rejected 兜底（防止状态不一致）
            filtered_items, filtered_high_kw, filtered_hate_kw = self._filter_rejected(
                data.items, data.high_interest_keywords, data.hate_keywords
            )
            # 重算质心（复用 regenerate 的批量嵌入 + 均值逻辑）
            centroids, dim = await self._recompute_centroids(filtered_items, embed_fn)
            # 更新 self._data（原地修改，保持引用一致）
            data.items = filtered_items
            data.high_interest_keywords = filtered_high_kw
            data.hate_keywords = filtered_hate_kw
            data.centroids = centroids
            data.dim = dim
            # 持久化
            self._save_npz(data)
            self.log(
                "info",
                f"[ProSocial] interest.py: apply_rejected 完成 "
                f"(items={len(filtered_items)}, dim={dim})",
            )
            return True, ""
        except Exception as e:
            self.log("warning", f"[ProSocial] interest.py: apply_rejected 失败: {e}")
            return False, str(e)

    # ------------------------------------------------------------------ #
    # 增删改查（F2）：add / update / remove
    # ------------------------------------------------------------------ #
    async def add_item(
        self,
        kind: str,
        label: str,
        text: str,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[bool, str]:
        """添加自定义关键词或示例句子。

        kind="example" : 向指定 label 的 InterestItem.examples 追加
        kind="high_keyword" : 向 high_interest_keywords 追加
        kind="hate_keyword" : 向 hate_keywords 追加
        添加后重算质心并持久化。
        """
        if self._data is None:
            return False, "尚未生成兴趣数据"
        if not text or not text.strip():
            return False, "文本不能为空"

        text = text.strip()

        if kind == "example":
            level_map = {lv.value: lv for lv in InterestLevel}
            lv = level_map.get(label)
            if lv is None:
                return False, f"非法 label: {label}"
            # 找到对应 label 的第一个 item 追加
            target_items = [it for it in self._data.items if it.level == lv]
            if not target_items:
                return False, f"未找到 label={label} 的兴趣项"
            target_items[0].examples.append(text)
        elif kind == "high_keyword":
            if text not in self._data.high_interest_keywords:
                self._data.high_interest_keywords.append(text)
        elif kind == "hate_keyword":
            if text not in self._data.hate_keywords:
                self._data.hate_keywords.append(text)
        else:
            return False, f"未知 kind: {kind}"

        # 重算质心并持久化
        centroids, dim = await self._recompute_centroids(self._data.items, embed_fn)
        self._data.centroids = centroids
        self._data.dim = dim
        try:
            self._save_npz(self._data)
        except Exception as e:
            self.log("warning", f"[ProSocial] interest.py: add_item 持久化失败: {e}")
        return True, ""

    async def update_item(
        self,
        kind: str,
        label: str,
        old_text: str,
        new_text: str,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[bool, str]:
        """更新关键词或示例句子。

        kind="example" : 在指定 label 的 InterestItem.examples 中替换 old_text → new_text
        kind="high_keyword" : 在 high_interest_keywords 中替换
        kind="hate_keyword" : 在 hate_keywords 中替换
        替换后重算质心并持久化。
        """
        if self._data is None:
            return False, "尚未生成兴趣数据"
        if not new_text or not new_text.strip():
            return False, "新文本不能为空"

        new_text = new_text.strip()
        found = False

        if kind == "example":
            level_map = {lv.value: lv for lv in InterestLevel}
            lv = level_map.get(label)
            if lv is None:
                return False, f"非法 label: {label}"
            for it in self._data.items:
                if it.level == lv and old_text in it.examples:
                    idx = it.examples.index(old_text)
                    it.examples[idx] = new_text
                    found = True
                    break
        elif kind == "high_keyword":
            if old_text in self._data.high_interest_keywords:
                idx = self._data.high_interest_keywords.index(old_text)
                self._data.high_interest_keywords[idx] = new_text
                found = True
        elif kind == "hate_keyword":
            if old_text in self._data.hate_keywords:
                idx = self._data.hate_keywords.index(old_text)
                self._data.hate_keywords[idx] = new_text
                found = True
        else:
            return False, f"未知 kind: {kind}"

        if not found:
            return False, f"未找到要更新的项: {old_text}"

        # 重算质心并持久化
        centroids, dim = await self._recompute_centroids(self._data.items, embed_fn)
        self._data.centroids = centroids
        self._data.dim = dim
        try:
            self._save_npz(self._data)
        except Exception as e:
            self.log("warning", f"[ProSocial] interest.py: update_item 持久化失败: {e}")
        return True, ""

    async def remove_item(
        self,
        kind: str,
        label: str,
        text: str,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[bool, str]:
        """v0.3.6：移除关键词或示例句子（统一走 reject，所有删除都可恢复）。

        kind="example" : 从指定 label 的 InterestItem.examples 中移除
        kind="high_keyword" : 从 high_interest_keywords 中移除
        kind="hate_keyword" : 从 hate_keywords 中移除
        移除后重算质心并持久化。被移除的项加入 _rejected 列表（可 restore 恢复）。
        """
        if self._data is None:
            return False, "尚未生成兴趣数据"

        # 检查项是否存在（保留原有 "未找到" 错误语义）
        if kind == "example":
            level_map = {lv.value: lv for lv in InterestLevel}
            lv = level_map.get(label)
            if lv is None:
                return False, f"非法 label: {label}"
            found = any(
                it.level == lv and text in it.examples for it in self._data.items
            )
        elif kind == "high_keyword":
            found = text in self._data.high_interest_keywords
        elif kind == "hate_keyword":
            found = text in self._data.hate_keywords
        else:
            return False, f"未知 kind: {kind}"

        if not found:
            return False, f"未找到要移除的项: {text}"

        # v0.3.6：统一调 reject（加入 _rejected + 从 active 移除）
        self.reject(kind, label, text)

        # 重算质心并持久化
        centroids, dim = await self._recompute_centroids(self._data.items, embed_fn)
        self._data.centroids = centroids
        self._data.dim = dim
        try:
            self._save_npz(self._data)
        except Exception as e:
            self.log("warning", f"[ProSocial] interest.py: remove_item 持久化失败: {e}")
        return True, ""

    async def batch_update(
        self,
        adds: list[dict],
        removes: list[dict],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[int, str]:
        """v0.3.5 F5：批量增删关键词/示例句子，最后只重算一次质心。

        adds/removes 结构同 _apply_keywords_patch：[{kind, label, text}, ...]
        kind ∈ example|high_keyword|hate_keyword。
        与逐次 add_item/remove_item 区别：内存批量操作 + 单次 _recompute_centroids
        + 单次 _save_npz，从 N 次嵌入 API 调用降到 1 次。

        返回 (成功操作项数, 错误描述)。
        """
        if self._data is None:
            return 0, "尚未生成兴趣数据"

        valid_kinds = ("example", "high_keyword", "hate_keyword")
        count = 0

        # 批量 add（仅内存操作，不重算质心）
        for item in adds:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            label = str(item.get("label", "") or "")
            text = str(item.get("text", "") or "").strip()
            if kind not in valid_kinds or not text:
                continue
            if kind == "example":
                level_map = {lv.value: lv for lv in InterestLevel}
                lv = level_map.get(label)
                if lv is None:
                    continue
                target_items = [it for it in self._data.items if it.level == lv]
                if not target_items:
                    continue
                if text not in target_items[0].examples:
                    target_items[0].examples.append(text)
                    count += 1
            elif kind == "high_keyword":
                if text not in self._data.high_interest_keywords:
                    self._data.high_interest_keywords.append(text)
                    count += 1
            elif kind == "hate_keyword":
                if text not in self._data.hate_keywords:
                    self._data.hate_keywords.append(text)
                    count += 1

        # 批量 remove（v0.3.6：调 reject 逻辑，加入 _rejected + _remove_from_active）
        for item in removes:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            label = str(item.get("label", "") or "")
            text = str(item.get("text", "") or "")
            if kind not in valid_kinds or not text:
                continue
            # _remove_from_active 返回 True 表示项在 active 中且已移除
            removed = self._remove_from_active(kind, label, text)
            if not removed:
                continue
            # 加入 _rejected（与 reject 内部逻辑一致，LLM 删除也可恢复）
            if kind == "example":
                existing_ex = {
                    (e.get("label", ""), e.get("text", ""))
                    for e in self._rejected.get("examples", [])
                    if isinstance(e, dict)
                }
                if (label, text) not in existing_ex:
                    self._rejected["examples"].append({"label": label, "text": text})
            else:  # high_keyword / hate_keyword
                existing_kw = {
                    k.get("text", "")
                    for k in self._rejected.get("keywords", [])
                    if isinstance(k, dict)
                }
                if text not in existing_kw:
                    self._rejected["keywords"].append({"text": text, "kind": kind})
            count += 1

        if count == 0:
            return 0, ""

        # 单次重算质心 + 单次持久化
        try:
            centroids, dim = await self._recompute_centroids(self._data.items, embed_fn)
            self._data.centroids = centroids
            self._data.dim = dim
        except Exception as e:
            self.log(
                "warning", f"[ProSocial] interest.py: batch_update 重算质心失败: {e}"
            )
            return count, f"重算质心失败: {e}"
        try:
            self._save_npz(self._data)
        except Exception as e:
            self.log(
                "warning", f"[ProSocial] interest.py: batch_update 持久化失败: {e}"
            )
            return count, f"持久化失败: {e}"
        return count, ""

    # ------------------------------------------------------------------ #
    # 内部辅助方法
    # ------------------------------------------------------------------ #

    async def _recompute_centroids(
        self,
        items: list[InterestItem],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> tuple[dict[str, list[float]], int]:
        """按级别收集 examples → 1 次批量嵌入 → 求均值质心。

        regenerate 与 apply_rejected 共用此逻辑。
        返回 (centroids, dim)；嵌入失败时 centroids 为空 dict、dim 为 0。
        """
        level_examples: dict[str, list[str]] = {lv.value: [] for lv in _LEVEL_ORDER}
        all_examples: list[str] = []
        for it in items:
            for ex in it.examples:
                if ex:
                    level_examples[it.level.value].append(ex)
                    all_examples.append(ex)

        embeddings: list[list[float]] = []
        if all_examples:
            try:
                embeddings = await embed_fn(all_examples)
            except Exception as e:
                self.log("warning", f"[ProSocial] interest.py: 批量嵌入失败: {e}")
                embeddings = []
        dim = len(embeddings[0]) if embeddings else 0

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
        return centroids, dim

    def _filter_rejected(
        self,
        items: list[InterestItem],
        high_kw: list[str],
        hate_kw: list[str],
    ) -> tuple[list[InterestItem], list[str], list[str]]:
        """从 items/keywords 中移除 rejected 项。

        examples 按 (label, text) 匹配移除；keywords 按 text 精确匹配移除。
        返回 (filtered_items, filtered_high_kw, filtered_hate_kw)。
        """
        rejected_examples = {
            (e.get("label", ""), e.get("text", ""))
            for e in self._rejected.get("examples", [])
            if isinstance(e, dict)
        }
        # v0.3.6：keywords 格式为 [{"text": str, "kind": str}]，按 text 精确匹配移除
        rejected_keywords = {
            k.get("text", "")
            for k in self._rejected.get("keywords", [])
            if isinstance(k, dict)
        }
        filtered_items = [
            InterestItem(
                level=it.level,
                topic=it.topic,
                examples=[
                    ex
                    for ex in it.examples
                    if (it.level.value, ex) not in rejected_examples
                ],
                weight=it.weight,
            )
            for it in items
        ]
        filtered_high_kw = [k for k in high_kw if k not in rejected_keywords]
        filtered_hate_kw = [k for k in hate_kw if k not in rejected_keywords]
        return filtered_items, filtered_high_kw, filtered_hate_kw

    def _remove_from_active(self, kind: str, label: str, text: str) -> bool:
        """v0.3.6：从 active items/keywords 中移除指定项（内部方法）。

        kind="example"      : 从指定 label 的 InterestItem.examples 移除
        kind="keyword"      : 从 high_interest_keywords 和 hate_keywords 都移除
        kind="high_keyword" : 仅从 high_interest_keywords 移除
        kind="hate_keyword" : 仅从 hate_keywords 移除
        self._data 为 None 时返回 False（未生成数据，容错跳过）。
        """
        if self._data is None:
            return False
        removed = False
        if kind == "example":
            level_map = {lv.value: lv for lv in InterestLevel}
            lv = level_map.get(label)
            if lv is not None:
                for it in self._data.items:
                    if it.level == lv and text in it.examples:
                        it.examples.remove(text)
                        removed = True
                        break
        elif kind == "keyword":
            if text in self._data.high_interest_keywords:
                self._data.high_interest_keywords.remove(text)
                removed = True
            if text in self._data.hate_keywords:
                self._data.hate_keywords.remove(text)
                removed = True
        elif kind == "high_keyword":
            if text in self._data.high_interest_keywords:
                self._data.high_interest_keywords.remove(text)
                removed = True
        elif kind == "hate_keyword":
            if text in self._data.hate_keywords:
                self._data.hate_keywords.remove(text)
                removed = True
        return removed

    def _add_back_to_active(self, kind: str, label: str, text: str) -> bool:
        """v0.3.6：将指定项加回 active items/keywords（内部方法）。

        kind="example"                : 加回指定 label 的 InterestItem.examples
        kind="keyword"/"high_keyword" : 加回 high_interest_keywords
        kind="hate_keyword"           : 加回 hate_keywords
        已存在则不加（去重）；self._data 为 None 时返回 False。
        """
        if self._data is None:
            return False
        added = False
        if kind == "example":
            level_map = {lv.value: lv for lv in InterestLevel}
            lv = level_map.get(label)
            if lv is not None:
                for it in self._data.items:
                    if it.level == lv and text not in it.examples:
                        it.examples.append(text)
                        added = True
                        break
        elif kind in ("keyword", "high_keyword"):
            if text not in self._data.high_interest_keywords:
                self._data.high_interest_keywords.append(text)
                added = True
        elif kind == "hate_keyword":
            if text not in self._data.hate_keywords:
                self._data.hate_keywords.append(text)
                added = True
        return added

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
        example_count: int = 3,
        keyword_count: int = 12,
    ) -> dict:
        """调用 LLM 生成兴趣 JSON，最多重试 1 次；仍失败用内置默认兴趣集兜底。"""
        prompt = build_interest_prompt(
            persona_text,
            persona_knowledge,
            example_count=example_count,
            keyword_count=keyword_count,
        )
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
