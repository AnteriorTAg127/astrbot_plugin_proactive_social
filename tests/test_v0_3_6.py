"""test_v0_3_6.py —— v0.3.6 测试用例（Module E）。

测试对象：
- core/decision/interest.py → reject/restore/remove_item 即时移除与恢复（F1/F2）
- core/storage/tune_history.py → TuneHistoryStore 持久化（F3）
- core/plugin/web.py → tune_history handler 透传（API）

覆盖 16 项：
  F1 reject 即时移除（3 项）/ F2 restore 恢复（4 项）/
  F3 调参历史持久化（6 项）/ API 透传（3 项）

测试策略：
- InterestManager 用真实实例 + 内存构造 InterestData，直接断言 _rejected/_data。
- TuneHistoryStore 用 tempfile 临时 SQLite 文件，每用例独立。
- web handler 复用 test_web.py 的 _MockBridge 思路，新建 _MockBridgeV036 避免影响既有测试。
- 异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio（与 conftest/test_v0_3_5 一致）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from core.common.models import InterestData, InterestItem, InterestLevel
from core.decision.interest import InterestManager
from core.plugin.web import build_handlers
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
    """构造一个已加载兴趣数据的 InterestManager。"""
    tmpdir = Path(tempfile.mkdtemp())
    mgr = InterestManager(tmpdir, _silent_log)
    # 直接构造内存数据
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


# ======================================================================
# F1: reject 即时移除（3 项）
# ======================================================================


def test_reject_example_immediate_remove():
    """F1-1: reject example 立即从 items 移除。"""
    mgr = _make_interest_mgr_with_data()
    assert any(
        it.level == InterestLevel.CORE and "你好" in it.examples
        for it in mgr._data.items
    )
    ok, msg = mgr.reject("example", label="core", text="你好")
    assert ok is True
    assert msg == ""
    # 立即从 active items 移除
    assert not any(
        it.level == InterestLevel.CORE and "你好" in it.examples
        for it in mgr._data.items
    )
    # 加入 rejected
    assert {"label": "core", "text": "你好"} in mgr._rejected["examples"]


def test_reject_keyword_immediate_remove():
    """F1-2: reject keyword 立即从列表移除（auto 检测 kind）。"""
    mgr = _make_interest_mgr_with_data()
    assert "闲聊" in mgr._data.high_interest_keywords
    ok, msg = mgr.reject("keyword", text="闲聊")
    assert ok is True
    assert msg == ""
    # 立即从 high_interest_keywords 移除
    assert "闲聊" not in mgr._data.high_interest_keywords
    # 加入 rejected（kind 自动检测为 high_keyword）
    assert any(
        k.get("text") == "闲聊" and k.get("kind") == "high_keyword"
        for k in mgr._rejected["keywords"]
    )


def test_remove_item_uses_reject():
    """F1-3: remove_item 走 reject 路径（删除后进 _rejected 可恢复）。"""
    mgr = _make_interest_mgr_with_data()

    async def _run():
        async def mock_embed(texts):
            return [[0.1] for _ in texts]

        # 移除一个 example
        ok, msg = await mgr.remove_item("example", "core", "你好", mock_embed)
        assert ok is True
        assert msg == ""
        # 应在 _rejected 中
        assert {"label": "core", "text": "你好"} in mgr._rejected["examples"]
        # active 中应已移除
        assert not any(
            it.level == InterestLevel.CORE and "你好" in it.examples
            for it in mgr._data.items
        )

    asyncio.run(_run())


# ======================================================================
# F2: restore 恢复（4 项）
# ======================================================================


def test_restore_example_back_to_items():
    """F2-1: restore example 恢复到 items。"""
    mgr = _make_interest_mgr_with_data()
    mgr.reject("example", label="core", text="你好")
    # 恢复
    ok, msg = mgr.restore("example", label="core", text="你好")
    assert ok is True
    assert msg == ""
    # 应重新出现在 items 中
    assert any(
        it.level == InterestLevel.CORE and "你好" in it.examples
        for it in mgr._data.items
    )
    # _rejected 中应移除
    assert {"label": "core", "text": "你好"} not in mgr._rejected["examples"]


def test_restore_keyword_high():
    """F2-2: restore keyword 恢复到 high_interest_keywords（high_keyword kind）。"""
    mgr = _make_interest_mgr_with_data()
    mgr.reject("keyword", text="闲聊")  # 自动检测为 high_keyword
    ok, msg = mgr.restore("keyword", text="闲聊")
    assert ok is True
    assert msg == ""
    assert "闲聊" in mgr._data.high_interest_keywords
    assert not any(k.get("text") == "闲聊" for k in mgr._rejected["keywords"])


def test_restore_keyword_hate():
    """F2-3: restore keyword 恢复到 hate_keywords（hate_keyword kind）。"""
    mgr = _make_interest_mgr_with_data()
    mgr.reject("hate_keyword", text="骂人")
    ok, msg = mgr.restore("keyword", text="骂人")
    assert ok is True
    assert msg == ""
    assert "骂人" in mgr._data.hate_keywords
    assert not any(k.get("text") == "骂人" for k in mgr._rejected["keywords"])


def test_restore_nonexistent():
    """F2-4: restore 不存在的项返回失败。"""
    mgr = _make_interest_mgr_with_data()
    ok, msg = mgr.restore("example", label="core", text="不存在的文本")
    assert ok is False
    assert "未找到" in msg


# ======================================================================
# F3: 调参历史持久化（6 项）
# ======================================================================


def test_tune_history_record():
    """F3-1: record 插入记录。"""
    store = _make_tune_store()

    async def _run():
        rid = await store.record(
            action="analyze",
            source="manual",
            patch={"base_threshold": 0.6},
            keywords_patch={"add": []},
            persona_revision=None,
            analysis="test analysis",
            expected_effect="test effect",
            applied=False,
        )
        assert rid > 0
        await store.close()

    asyncio.run(_run())


def test_tune_history_list():
    """F3-2: list 返回记录列表（新→旧排序，字段完整）。"""
    store = _make_tune_store()

    async def _run():
        await store.record(
            action="analyze",
            source="manual",
            patch={"a": 1},
            keywords_patch=None,
            persona_revision=None,
            analysis="a1",
            expected_effect="e1",
            applied=False,
        )
        await store.record(
            action="apply",
            source="auto",
            patch={"b": 2},
            keywords_patch={"add": [{"kind": "high_keyword", "text": "x"}]},
            persona_revision="new persona",
            analysis="",
            expected_effect="",
            applied=True,
        )
        records = await store.list(limit=10)
        assert len(records) == 2
        # 新→旧排序
        assert records[0]["action"] == "apply"
        assert records[1]["action"] == "analyze"
        # 字段完整
        r = records[0]
        assert r["source"] == "auto"
        assert r["patch"] == {"b": 2}
        assert r["keywords_patch"] == {"add": [{"kind": "high_keyword", "text": "x"}]}
        assert r["persona_revision"] == "new persona"
        assert r["applied"] is True
        await store.close()

    asyncio.run(_run())


def test_tune_history_clear():
    """F3-3: clear 清空所有记录。"""
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
        deleted = await store.clear()
        assert deleted == 1
        records = await store.list()
        assert records == []
        await store.close()

    asyncio.run(_run())


def test_tune_history_stats():
    """F3-4: get_stats 返回统计。"""
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
        await store.record(
            action="apply",
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
        assert stats["analyze_count"] == 2
        assert stats["apply_count"] == 1
        assert stats["last_timestamp"] is not None
        await store.close()

    asyncio.run(_run())


def test_tune_history_empty_list():
    """F3-5: list 空数据库返回空列表。"""
    store = _make_tune_store()

    async def _run():
        records = await store.list()
        assert records == []
        stats = await store.get_stats()
        assert stats["total"] == 0
        assert stats["last_timestamp"] is None
        await store.close()

    asyncio.run(_run())


def test_tune_history_empty_stats():
    """F3-6: get_stats 空数据库返回零值。"""
    store = _make_tune_store()

    async def _run():
        stats = await store.get_stats()
        assert stats == {
            "total": 0,
            "analyze_count": 0,
            "apply_count": 0,
            "last_timestamp": None,
        }
        await store.close()

    asyncio.run(_run())


# ======================================================================
# API 测试（3 项）
# ======================================================================


class _MockBridgeV036:
    """v0.3.6 测试专用 mock bridge（仅实现 tune_history / interests 接口）。"""

    def __init__(self):
        self.interests_patch_result = (True, "")
        self.last_interests_patch = None
        self.tune_history_data = {
            "records": [
                {
                    "id": 1,
                    "timestamp": 1784883877.68,
                    "action": "analyze",
                    "source": "manual",
                    "patch": {"base_threshold": 0.6},
                    "keywords_patch": None,
                    "persona_revision": None,
                    "analysis": "test",
                    "expected_effect": "effect",
                    "applied": False,
                }
            ],
            "stats": {
                "total": 1,
                "analyze_count": 1,
                "apply_count": 0,
                "last_timestamp": 1784883877.68,
            },
        }
        self.clear_result = (True, "")

    async def set_interests_view(self, body):
        self.last_interests_patch = body
        return self.interests_patch_result

    async def get_tune_history_view(self, limit=50, offset=0):
        return self.tune_history_data

    async def clear_tune_history_view(self):
        return self.clear_result


def test_api_restore_interest():
    """API-1: POST /prosocial/interests action=restore 透传到 bridge.set_interests_view。"""
    bridge = _MockBridgeV036()
    handlers = build_handlers(bridge)
    h = handlers["POST /prosocial/interests"]

    async def _run():
        return await h(
            {},
            {"action": "restore", "kind": "example", "label": "core", "text": "你好"},
        )

    status, body = asyncio.run(_run())
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_interests_patch == {
        "action": "restore",
        "kind": "example",
        "label": "core",
        "text": "你好",
    }


def test_api_get_tune_history():
    """API-2: GET /prosocial/tune_history 返回历史记录。"""
    bridge = _MockBridgeV036()
    handlers = build_handlers(bridge)
    h = handlers["GET /prosocial/tune_history"]

    async def _run():
        return await h({"limit": "10", "offset": "0"}, None)

    status, body = asyncio.run(_run())
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["stats"]["total"] == 1
    assert body["data"]["records"][0]["action"] == "analyze"


def test_api_delete_tune_history():
    """API-3: DELETE /prosocial/tune_history 调用 clear。"""
    bridge = _MockBridgeV036()
    handlers = build_handlers(bridge)
    h = handlers["DELETE /prosocial/tune_history"]

    async def _run():
        return await h({}, None)

    status, body = asyncio.run(_run())
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["cleared"] is True
