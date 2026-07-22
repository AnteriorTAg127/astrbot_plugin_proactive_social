"""test_v0_2_2.py —— v0.2.2 F18/F20 后端测试。

测试对象：
- core/interest.py → InterestManager 的 F20 新增方法（export_view/reject/apply_rejected/
  set_rejected/get_rejected/_filter_rejected/_recompute_centroids 复用）
- core/web.py → build_handlers 的 3 个新 handler（providers GET / interests GET / interests POST）

覆盖点（PRD §7 验收 #1/#3/#4/#5）：
- export_view：未生成/已生成结构、4 级 items + keywords + rejected
- reject：example 按 (label,text) 去重、keyword 按 text 去重、非法 kind 静默忽略
- apply_rejected：移除 example/keyword + 重算质心 + 持久化、未生成返回 False
- regenerate 兼容：换人设重生成时排除已 rejected 项
- set_rejected/get_rejected：往返 + 浅拷贝隔离 + 非 dict 容错
- web handlers：providers/interests GET 成功、interests POST reject/apply/非法 action/None body
"""

from __future__ import annotations

import asyncio

from core.interest import InterestManager
from core.models import InterestLevel
from core.web import build_handlers

# ---------------------------------------------------------------------- #
# MockBridge for web tests
# ----------------------------------------------------------------------


class _MockBridge:
    """实现 WebBridge 鸭子接口的 mock，行为可配置。"""

    def __init__(self):
        self.providers_data: dict = {"chat": ["c1", "c2"], "embedding": ["e1"]}
        self.interests_data: dict = {
            "generated": True,
            "persona_hash": "h",
            "items": [
                {"label": "core", "topic": "t", "examples": ["e"], "weight": 1.5}
            ],
            "hate_keywords": ["bad"],
            "high_interest_keywords": ["good"],
            "rejected": {"examples": [], "keywords": []},
        }
        self.interests_patch_result: tuple[bool, str] = (True, "")
        self.last_interests_patch: dict | None = None

    def get_providers_view(self) -> dict:
        return self.providers_data

    def get_interests_view(self) -> dict:
        return self.interests_data

    async def set_interests_view(self, body: dict) -> tuple[bool, str]:
        self.last_interests_patch = body
        return self.interests_patch_result


def _run(handler, params=None, body=None):
    return asyncio.run(handler(params or {}, body))


# ====================================================================== #
# F20: InterestManager.export_view
# ====================================================================== #


def test_export_view_not_generated(mock_log, tmp_data_dir):
    """未生成时 export_view 返回 generated=False 空结构。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    view = mgr.export_view()
    assert view["generated"] is False
    assert view["persona_hash"] == ""
    assert view["items"] == []
    assert view["hate_keywords"] == []
    assert view["high_interest_keywords"] == []
    assert view["rejected"] == {"examples": [], "keywords": []}


def test_export_view_generated(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """已生成时 export_view 返回完整结构（4 级 items + keywords + rejected）。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    view = mgr.export_view()
    assert view["generated"] is True
    assert view["persona_hash"] != ""
    assert len(view["items"]) == 4
    # 第一项是 core
    core = view["items"][0]
    assert core["label"] == "core"
    assert core["topic"] == "星穹铁道配队"
    assert "符玄怎么配队？" in core["examples"]
    assert core["weight"] == 1.5
    # keywords
    assert "骂人" in view["hate_keywords"]
    assert "符玄" in view["high_interest_keywords"]
    # rejected 默认空
    assert view["rejected"] == {"examples": [], "keywords": []}


def test_export_view_includes_rejected(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """export_view 的 rejected 字段反映当前 rejected 列表。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    mgr.reject("example", label="core", text="foo")
    mgr.reject("keyword", text="bar")
    view = mgr.export_view()
    assert len(view["rejected"]["examples"]) == 1
    assert view["rejected"]["examples"][0] == {"label": "core", "text": "foo"}
    assert view["rejected"]["keywords"] == ["bar"]


# ====================================================================== #
# F20: InterestManager.reject
# ====================================================================== #


def test_reject_example_dedup(mock_log, tmp_data_dir):
    """reject example 按 (label, text) 去重。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("example", label="core", text="foo")
    mgr.reject("example", label="core", text="foo")  # 重复
    mgr.reject("example", label="general", text="foo")  # 不同 label 不算重复
    r = mgr.get_rejected()
    assert len(r["examples"]) == 2
    assert {"label": "core", "text": "foo"} in r["examples"]
    assert {"label": "general", "text": "foo"} in r["examples"]


def test_reject_keyword_dedup(mock_log, tmp_data_dir):
    """reject keyword 按 text 去重。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("keyword", text="foo")
    mgr.reject("keyword", text="foo")  # 重复
    mgr.reject("keyword", text="bar")
    r = mgr.get_rejected()
    assert len(r["keywords"]) == 2
    assert "foo" in r["keywords"]
    assert "bar" in r["keywords"]


def test_reject_invalid_kind_ignored(mock_log, tmp_data_dir):
    """非法 kind 静默忽略，不抛异常。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("invalid_kind", label="core", text="foo")
    mgr.reject("invalid_kind", text="bar")
    r = mgr.get_rejected()
    assert r["examples"] == []
    assert r["keywords"] == []


def test_reject_keyword_empty_text_ignored(mock_log, tmp_data_dir):
    """reject keyword 空 text 不加入。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("keyword", text="")
    r = mgr.get_rejected()
    assert r["keywords"] == []


# ====================================================================== #
# F20: InterestManager.set_rejected / get_rejected
# ====================================================================== #


def test_set_get_rejected_roundtrip(mock_log, tmp_data_dir):
    """set_rejected / get_rejected 往返一致。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    rejected = {
        "examples": [
            {"label": "core", "text": "foo"},
            {"label": "hate", "text": "bar"},
        ],
        "keywords": ["kw1", "kw2"],
    }
    mgr.set_rejected(rejected)
    assert mgr.get_rejected() == rejected


def test_get_rejected_returns_copy(mock_log, tmp_data_dir):
    """get_rejected 返回浅拷贝，外部修改不污染内部。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("keyword", text="foo")
    r = mgr.get_rejected()
    r["keywords"].append("bar")
    r["examples"].append({"label": "x", "text": "y"})
    r2 = mgr.get_rejected()
    assert r2["keywords"] == ["foo"]
    assert r2["examples"] == []


def test_set_rejected_non_dict_fallback(mock_log, tmp_data_dir):
    """set_rejected 非 dict 时回退空结构。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("keyword", text="foo")
    mgr.set_rejected("not a dict")  # type: ignore
    assert mgr.get_rejected() == {"examples": [], "keywords": []}
    mgr.set_rejected(None)  # type: ignore
    assert mgr.get_rejected() == {"examples": [], "keywords": []}


def test_set_rejected_missing_fields(mock_log, tmp_data_dir):
    """set_rejected 缺字段时回退空列表。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.set_rejected({"examples": [{"label": "c", "text": "t"}]})  # 缺 keywords
    r = mgr.get_rejected()
    assert len(r["examples"]) == 1
    assert r["keywords"] == []


# ====================================================================== #
# F20: InterestManager.apply_rejected
# ====================================================================== #


def test_apply_rejected_no_data(mock_log, tmp_data_dir, mock_embed):
    """未生成时 apply_rejected 返回 (False, '尚未生成兴趣数据')。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    ok, msg = asyncio.run(mgr.apply_rejected(mock_embed))
    assert ok is False
    assert "尚未生成" in msg


def test_apply_rejected_removes_example(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """apply_rejected 移除被 reject 的 example。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    data_before = mgr.get()
    assert len(data_before.items[0].examples) == 2  # core 2 examples
    mgr.reject("example", label="core", text="符玄怎么配队？")
    ok, msg = asyncio.run(mgr.apply_rejected(mock_embed))
    assert ok is True
    assert msg == ""
    data_after = mgr.get()
    assert len(data_after.items[0].examples) == 1
    assert "符玄怎么配队？" not in data_after.items[0].examples
    # 其余级别 examples 不受影响
    general = [it for it in data_after.items if it.level == InterestLevel.GENERAL][0]
    assert len(general.examples) == 2


def test_apply_rejected_removes_keyword(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """apply_rejected 移除被 reject 的 keyword。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert "骂人" in mgr.get().hate_keywords
    mgr.reject("keyword", text="骂人")
    ok, _ = asyncio.run(mgr.apply_rejected(mock_embed))
    assert ok is True
    assert "骂人" not in mgr.get().hate_keywords
    # high_interest_keywords 不受影响
    assert "符玄" in mgr.get().high_interest_keywords


def test_apply_rejected_recomputes_centroids(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """apply_rejected 重算质心（core 减 1 example 后质心变化）。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    orig_centroid = list(mgr.get().centroids["core"])
    mgr.reject("example", label="core", text="符玄怎么配队？")
    asyncio.run(mgr.apply_rejected(mock_embed))
    new_centroid = mgr.get().centroids["core"]
    # 只剩 1 example，质心 = 该 example 向量，与原均值不同
    assert new_centroid != orig_centroid


def test_apply_rejected_persists_npz(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """apply_rejected 后 npz 持久化，新 manager 加载到过滤后数据。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    mgr.reject("example", label="core", text="符玄怎么配队？")
    mgr.reject("keyword", text="骂人")
    asyncio.run(mgr.apply_rejected(mock_embed))
    # 新 manager 加载（persona hash 一致）
    mgr2 = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr2.ensure_loaded("p", "k", mock_llm, mock_embed))
    core = [it for it in data.items if it.level == InterestLevel.CORE][0]
    assert "符玄怎么配队？" not in core.examples
    assert "骂人" not in data.hate_keywords


def test_apply_rejected_embed_failure_no_crash(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """apply_rejected 嵌入失败时 centroids 为空，不抛异常。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    mgr.reject("example", label="core", text="符玄怎么配队？")
    mock_embed.set_fail_mode(True)
    ok, msg = asyncio.run(mgr.apply_rejected(mock_embed))
    # 嵌入失败不抛异常，apply_rejected 仍返回 True（_recompute_centroids 内部容错）
    assert ok is True
    # 质心为空（dim=0）
    assert mgr.get().centroids == {}
    assert mgr.get().dim == 0


# ====================================================================== #
# F20: regenerate 兼容（排除 rejected）
# ====================================================================== #


def test_regenerate_excludes_rejected_example(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """regenerate 也排除 rejected example（换人设重生成时不回来）。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("example", label="core", text="符玄怎么配队？")
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    core = [it for it in data.items if it.level == InterestLevel.CORE][0]
    assert "符玄怎么配队？" not in core.examples
    assert len(core.examples) == 1  # 只剩 1 个


def test_regenerate_excludes_rejected_keyword(
    mock_llm, mock_embed, mock_log, tmp_data_dir
):
    """regenerate 也排除 rejected keyword。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    mgr.reject("keyword", text="骂人")
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    assert "骂人" not in data.hate_keywords
    # 其余 keyword 保留
    assert "刷屏" in data.hate_keywords


def test_regenerate_no_rejected_unchanged(mock_llm, mock_embed, mock_log, tmp_data_dir):
    """无 rejected 时 regenerate 行为不变（4 级各 2 examples）。"""
    mgr = InterestManager(tmp_data_dir, mock_log)
    data = asyncio.run(mgr.regenerate("p", "k", mock_llm, mock_embed))
    for it in data.items:
        assert len(it.examples) == 2


# ====================================================================== #
# F18/F20: web handlers
# ====================================================================== #


def test_web_get_providers_ok():
    """GET /prosocial/providers 成功返回 chat/embedding id 列表。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/providers"]
    status, body = _run(h)
    assert status == 200
    assert body["ok"] is True
    assert body["data"] == {"chat": ["c1", "c2"], "embedding": ["e1"]}


def test_web_get_providers_bridge_exception_500():
    """bridge.get_providers_view 抛异常 → 500。"""
    bridge = _MockBridge()

    def raise_fn():
        raise RuntimeError("boom")

    bridge.get_providers_view = raise_fn
    h = build_handlers(bridge)["GET /prosocial/providers"]
    status, body = _run(h)
    assert status == 500
    assert body["ok"] is False
    assert "boom" in body["error"]


def test_web_get_interests_ok():
    """GET /prosocial/interests 成功返回兴趣数据视图。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/interests"]
    status, body = _run(h)
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["generated"] is True
    assert body["data"]["persona_hash"] == "h"


def test_web_get_interests_bridge_exception_500():
    """bridge.get_interests_view 抛异常 → 500。"""
    bridge = _MockBridge()

    def raise_fn():
        raise RuntimeError("boom")

    bridge.get_interests_view = raise_fn
    h = build_handlers(bridge)["GET /prosocial/interests"]
    status, body = _run(h)
    assert status == 500
    assert body["ok"] is False


def test_web_post_interests_reject_example():
    """POST interests {action:reject, kind:example} 成功。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(
        h,
        body={"action": "reject", "kind": "example", "label": "core", "text": "foo"},
    )
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_interests_patch == {
        "action": "reject",
        "kind": "example",
        "label": "core",
        "text": "foo",
    }


def test_web_post_interests_reject_keyword():
    """POST interests {action:reject, kind:keyword} 成功。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body={"action": "reject", "kind": "keyword", "text": "bar"})
    assert status == 200
    assert body["ok"] is True


def test_web_post_interests_apply():
    """POST interests {action:apply} 成功。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body={"action": "apply"})
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_interests_patch == {"action": "apply"}


def test_web_post_interests_bridge_rejects():
    """bridge.set_interests_view 返回 (False, err) → 400。"""
    bridge = _MockBridge()
    bridge.interests_patch_result = (False, "尚未生成兴趣数据")
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body={"action": "apply"})
    assert status == 400
    assert body["ok"] is False
    assert "尚未生成" in body["error"]


def test_web_post_interests_none_body_rejected():
    """body=None → 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body=None)
    assert status == 400
    assert body["ok"] is False


def test_web_post_interests_non_dict_body_rejected():
    """body 非 dict → 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body="not a dict")
    assert status == 400
    assert body["ok"] is False


def test_web_post_interests_unknown_action_rejected():
    """bridge 拒绝未知 action → 400（bridge 返回 (False, '未知 action')）。"""
    bridge = _MockBridge()
    bridge.interests_patch_result = (False, "未知 action")
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body={"action": "unknown"})
    assert status == 400
    assert body["ok"] is False
    assert "未知" in body["error"]


def test_web_post_interests_bridge_exception_500():
    """bridge.set_interests_view 抛异常 → 500。"""
    bridge = _MockBridge()

    async def raise_fn(body):
        raise RuntimeError("boom")

    bridge.set_interests_view = raise_fn
    h = build_handlers(bridge)["POST /prosocial/interests"]
    status, body = _run(h, body={"action": "apply"})
    assert status == 500
    assert body["ok"] is False
    assert "boom" in body["error"]
