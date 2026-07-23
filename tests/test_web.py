"""test_web.py —— F Web API 7 handler。

测试对象：core/web.py → build_handlers（用 mock WebBridge）
覆盖点：
- 7 handler 全部返回 (status, json) 且成功 200 + {"ok": True, "data": ...}
- GET /prosocial/decisions：limit 默认/非法/越界 clamp
- POST /prosocial/dryrun：bool 校验、非 bool 拒绝、缺失拒绝
- POST /prosocial/config：合法 patch、bridge 拒绝时 400
- POST /prosocial/groups：合法 patch、非法 mode 拒绝
- bridge 异常 → 500

对应 PRD §8.14（Web API 7 接口合法 JSON + 非法参数被拒）。
"""

from __future__ import annotations

import asyncio

from core.plugin.web import build_handlers

# ---------------------------------------------------------------------- #
# MockBridge
# ---------------------------------------------------------------------- #


class _MockBridge:
    """实现 WebBridge 鸭子接口的 mock，行为可配置。"""

    def __init__(self):
        self.status_data: dict = {"running": True, "groups": []}
        self.decisions_data: list[dict] = [{"ts": 1.0, "group_id": "g1"}]
        self.config_data: dict = {"base_threshold": 0.65}
        self.groups_data: dict = {"mode": "whitelist", "whitelist": [], "groups": []}
        self.config_patch_result: tuple[bool, str] = (True, "")
        self.groups_patch_result: tuple[bool, str] = (True, "")
        self.last_config_patch: dict | None = None
        self.last_groups_patch: dict | None = None
        # F18/F20 扩展
        self.providers_data: dict = {"chat": ["chat1"], "embedding": ["emb1"]}
        self.interests_data: dict = {
            "generated": True,
            "persona_hash": "h",
            "items": [],
            "hate_keywords": [],
            "high_interest_keywords": [],
            "rejected": {"examples": [], "keywords": []},
        }
        self.interests_patch_result: tuple[bool, str] = (True, "")
        self.last_interests_patch: dict | None = None
        # F3 LLM 诊断调参 / v0.2.9 T6.1 透传字段
        self.last_autotune_body: dict | None = None
        # v0.2.9 T6.2：显式捕获三字段（force/keywords_patch/persona_revision）
        # 便于测试断言「web 层 → main.run_autotune」的透传契约
        self.last_autotune_force: bool | None = None
        self.last_autotune_keywords_patch: dict | None = None
        self.last_autotune_persona_revision: str | None = None

    def get_status(self) -> dict:
        return self.status_data

    def get_decisions(self, limit: int) -> list[dict]:
        return self.decisions_data[:limit]

    def get_config_view(self) -> dict:
        return self.config_data

    async def set_config_view(self, patch: dict) -> tuple[bool, str]:
        self.last_config_patch = patch
        return self.config_patch_result

    def get_groups_view(self) -> dict:
        return self.groups_data

    async def set_groups_view(self, patch: dict) -> tuple[bool, str]:
        self.last_groups_patch = patch
        return self.groups_patch_result

    def get_providers_view(self) -> dict:
        return self.providers_data

    def get_interests_view(self) -> dict:
        return self.interests_data

    async def set_interests_view(self, body: dict) -> tuple[bool, str]:
        self.last_interests_patch = body
        return self.interests_patch_result

    def get_export_view(self) -> dict:
        return {
            "config": self.config_data,
            "decisions": self.decisions_data,
            "version": "v0.2.6",
        }

    async def run_autotune(self, body: dict) -> dict:
        # F3：LLM 诊断调参 mock —— 固定返回 analyze 成功结果
        # v0.2.9 T6.2：显式捕获 force / keywords_patch / persona_revision
        # （main.run_autotune 同样会读取这三个字段，mock 镜像其行为）
        self.last_autotune_body = body
        self.last_autotune_force = body.get("force") if isinstance(body, dict) else None
        self.last_autotune_keywords_patch = (
            body.get("keywords_patch") if isinstance(body, dict) else None
        )
        self.last_autotune_persona_revision = (
            body.get("persona_revision") if isinstance(body, dict) else None
        )
        # v0.2.9 F4：响应附带 rate_limit 状态块（前端展示用）
        rate_limit = {
            "used": 1,
            "limit": 4,
            "next_available": 0,
            "cooldown_hours": 3.0,
        }
        if body.get("action") == "apply":
            return {
                "ok": True,
                "applied": True,
                "updated": len(body.get("patch") or {}),
                "rate_limit": rate_limit,
            }
        return {
            "ok": True,
            "analysis": "test analysis",
            "suggested_patch": {},
            "suggested_keywords_patch": None,
            "persona_revision": None,
            "expected_effect": "test effect",
            "applied": False,
            "rate_limit": rate_limit,
        }


def _run(handler, params=None, body=None):
    """asyncio.run 包装调用 handler，返回 (status, json)。"""
    return asyncio.run(handler(params or {}, body))


# ---------------------------------------------------------------------- #
# GET /prosocial/status
# ---------------------------------------------------------------------- #


def test_web_get_status_ok():
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/status"]
    status, body = _run(h)
    assert status == 200
    assert body["ok"] is True
    assert body["data"] == bridge.status_data


def test_web_get_status_bridge_exception_500():
    """bridge.get_status 抛异常 → 500 + ok=false。"""
    bridge = _MockBridge()
    bridge.status_data = None

    def raise_fn():
        raise RuntimeError("boom")

    bridge.get_status = raise_fn
    h = build_handlers(bridge)["GET /prosocial/status"]
    status, body = _run(h)
    assert status == 500
    assert body["ok"] is False
    assert "boom" in body["error"]


# ---------------------------------------------------------------------- #
# GET /prosocial/decisions
# ---------------------------------------------------------------------- #


def test_web_get_decisions_default_limit():
    bridge = _MockBridge()
    bridge.decisions_data = [{"ts": float(i)} for i in range(60)]
    h = build_handlers(bridge)["GET /prosocial/decisions"]
    status, body = _run(h, params={})
    assert status == 200
    assert len(body["data"]) == 50  # 默认 limit=50


def test_web_get_decisions_custom_limit():
    bridge = _MockBridge()
    bridge.decisions_data = [{"ts": float(i)} for i in range(20)]
    h = build_handlers(bridge)["GET /prosocial/decisions"]
    status, body = _run(h, params={"limit": "5"})
    assert status == 200
    assert len(body["data"]) == 5


def test_web_get_decisions_invalid_limit_rejected():
    """limit 非整数 → 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/decisions"]
    status, body = _run(h, params={"limit": "abc"})
    assert status == 400
    assert body["ok"] is False
    assert "limit" in body["error"]


def test_web_get_decisions_clamp_low():
    """limit<1 clamp 到 1。"""
    bridge = _MockBridge()
    bridge.decisions_data = [{"ts": 1.0}]
    h = build_handlers(bridge)["GET /prosocial/decisions"]
    status, body = _run(h, params={"limit": "0"})
    assert status == 200
    assert len(body["data"]) == 1


def test_web_get_decisions_clamp_high():
    """limit>500 clamp 到 500。"""
    bridge = _MockBridge()
    bridge.decisions_data = [{"ts": float(i)} for i in range(600)]
    h = build_handlers(bridge)["GET /prosocial/decisions"]
    status, body = _run(h, params={"limit": "999"})
    assert status == 200
    assert len(body["data"]) == 500


# ---------------------------------------------------------------------- #
# POST /prosocial/dryrun
# ---------------------------------------------------------------------- #


def test_web_post_dryrun_true():
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body={"enabled": True})
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["dry_run"] is True
    assert bridge.last_config_patch == {"dry_run": True}


def test_web_post_dryrun_false():
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body={"enabled": False})
    assert status == 200
    assert body["data"]["dry_run"] is False


def test_web_post_dryrun_non_bool_rejected():
    """非 bool 值（如 "yes"/1）被拒绝。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body={"enabled": "yes"})
    assert status == 400
    assert body["ok"] is False
    assert "enabled" in body["error"]


def test_web_post_dryrun_missing_rejected():
    """缺失 enabled → 拒绝。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body={})
    assert status == 400


def test_web_post_dryrun_none_body_rejected():
    """BUG-3: body=None → 400（与缺 enabled 同等级拒绝）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body=None)
    assert status == 400
    assert body["ok"] is False


def test_web_post_dryrun_non_dict_body_rejected():
    """BUG-3: body 非 dict（如字符串）→ 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body="not a dict")
    assert status == 400
    assert body["ok"] is False


def test_web_post_dryrun_bridge_rejects():
    """bridge.set_config_view 返回 (False, err) → 400。"""
    bridge = _MockBridge()
    bridge.config_patch_result = (False, "save failed")
    h = build_handlers(bridge)["POST /prosocial/dryrun"]
    status, body = _run(h, body={"enabled": True})
    assert status == 400
    assert "save failed" in body["error"]


# ---------------------------------------------------------------------- #
# GET /prosocial/config
# ---------------------------------------------------------------------- #


def test_web_get_config_ok():
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/config"]
    status, body = _run(h)
    assert status == 200
    assert body["data"] == bridge.config_data


# ---------------------------------------------------------------------- #
# POST /prosocial/config
# ---------------------------------------------------------------------- #


def test_web_post_config_valid_patch():
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/config"]
    status, body = _run(h, body={"base_threshold": 0.7})
    assert status == 200
    assert bridge.last_config_patch == {"base_threshold": 0.7}


def test_web_post_config_bridge_rejects():
    """bridge.set_config_view 返回 (False, err) → 400。"""
    bridge = _MockBridge()
    bridge.config_patch_result = (False, "base_threshold 超出范围")
    h = build_handlers(bridge)["POST /prosocial/config"]
    status, body = _run(h, body={"base_threshold": 999})
    assert status == 400
    assert "超出范围" in body["error"]


def test_web_post_config_non_dict_body_rejected():
    """非 dict body → 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/config"]
    status, body = _run(h, body="not a dict")
    assert status == 400
    assert body["ok"] is False


def test_web_post_config_none_body_rejected():
    """BUG-3: body=None → 400（修复前 body = body or {} 把 None 静默转空 dict → 200）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/config"]
    status, body = _run(h, body=None)
    assert status == 400
    assert body["ok"] is False


# ---------------------------------------------------------------------- #
# GET /prosocial/groups
# ---------------------------------------------------------------------- #


def test_web_get_groups_ok():
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/groups"]
    status, body = _run(h)
    assert status == 200
    assert body["data"] == bridge.groups_data


# ---------------------------------------------------------------------- #
# POST /prosocial/groups
# ---------------------------------------------------------------------- #


def test_web_post_groups_valid_patch():
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/groups"]
    status, body = _run(h, body={"mode": "all"})
    assert status == 200
    assert bridge.last_groups_patch == {"mode": "all"}


def test_web_post_groups_bridge_rejects():
    bridge = _MockBridge()
    bridge.groups_patch_result = (False, "mode 必须是 whitelist 或 all")
    h = build_handlers(bridge)["POST /prosocial/groups"]
    status, body = _run(h, body={"mode": "invalid"})
    assert status == 400
    assert "mode" in body["error"]


def test_web_post_groups_non_dict_body_rejected():
    """非 dict body（如字符串）→ 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/groups"]
    status, body = _run(h, body="not a dict")
    assert status == 400


def test_web_post_groups_none_body_rejected():
    """BUG-3: body=None → 400（与 post_config 行为一致）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/groups"]
    status, body = _run(h, body=None)
    assert status == 400
    assert body["ok"] is False


# ---------------------------------------------------------------------- #
# POST /prosocial/autotune（F3 LLM 诊断调参）
# ---------------------------------------------------------------------- #


def test_web_post_autotune_analyze_ok():
    """analyze 成功 → 200 + 扁平响应体（ok/analysis/suggested_patch/expected_effect 顶层字段）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "analyze"})
    assert status == 200
    assert body["ok"] is True
    assert body["analysis"] == "test analysis"
    assert body["suggested_patch"] == {}
    assert body["expected_effect"] == "test effect"
    assert body["applied"] is False
    # v0.2.9 T6.1：响应附带 rate_limit 状态块（前端展示用）
    assert "rate_limit" in body
    assert body["rate_limit"]["limit"] == 4
    assert bridge.last_autotune_body == {"action": "analyze"}


def test_web_post_autotune_apply_ok():
    """apply 成功 → 200 + {ok, applied, updated, rate_limit}。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "apply", "patch": {"base_threshold": 0.7}})
    assert status == 200
    assert body["ok"] is True
    assert body["applied"] is True
    assert body["updated"] == 1
    # v0.2.9 T6.1：apply 响应同样附带 rate_limit
    assert "rate_limit" in body


# v0.2.9 T6.2：force / keywords_patch / persona_revision 透传契约
def test_web_post_autotune_force_passthrough():
    """body.force=True 透传到 bridge.run_autotune（main 用以跳过速率限制）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "analyze", "force": True})
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_force is True
    assert bridge.last_autotune_body.get("force") is True


def test_web_post_autotune_force_default_false_when_absent():
    """body 不含 force 时，bridge.last_autotune_force 为 None（main 侧 bool(None or False)=False）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    _run(h, body={"action": "analyze"})
    # mock 直接读 body.get("force")，缺省返回 None；main 侧 bool(None or False)=False
    assert bridge.last_autotune_force is None


def test_web_post_autotune_force_non_bool_rejected():
    """force 非 bool（如 "yes"/1）→ 400（与 post_dryrun.enabled 严格校验同原则）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "analyze", "force": "yes"})
    assert status == 400
    assert body["ok"] is False
    assert "force" in body["error"]
    # bridge 不应被调用
    assert bridge.last_autotune_body is None


def test_web_post_autotune_keywords_patch_passthrough():
    """body.keywords_patch 透传到 bridge（main 用以调 interest_mgr.add/remove）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    kp = {
        "add": [
            {"kind": "high_keyword", "label": "core", "text": "Python"},
        ],
        "remove": [
            {"kind": "hate_keyword", "label": "hate", "text": "广告"},
        ],
    }
    status, body = _run(h, body={"action": "apply", "keywords_patch": kp})
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_keywords_patch == kp
    assert bridge.last_autotune_body.get("keywords_patch") == kp


def test_web_post_autotune_keywords_patch_non_dict_rejected():
    """keywords_patch 非 dict（如字符串/列表）→ 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "apply", "keywords_patch": "not a dict"})
    assert status == 400
    assert body["ok"] is False
    assert "keywords_patch" in body["error"]
    assert bridge.last_autotune_body is None


def test_web_post_autotune_keywords_patch_null_allowed():
    """keywords_patch=None 显式允许（表示无关键词增删）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(
        h,
        body={
            "action": "apply",
            "keywords_patch": None,
            "patch": {"base_threshold": 0.6},
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_keywords_patch is None


def test_web_post_autotune_persona_revision_passthrough():
    """body.persona_revision 透传到 bridge（main 合并入 persona_text 走重建路径）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    rev = "你是一只爱聊编程的猫娘，说话带「喵」尾音。"
    status, body = _run(h, body={"action": "apply", "persona_revision": rev})
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_persona_revision == rev
    assert bridge.last_autotune_body.get("persona_revision") == rev


def test_web_post_autotune_persona_revision_non_str_rejected():
    """persona_revision 非 str（如 dict/数字）→ 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(
        h, body={"action": "apply", "persona_revision": {"text": "不是字符串"}}
    )
    assert status == 400
    assert body["ok"] is False
    assert "persona_revision" in body["error"]
    assert bridge.last_autotune_body is None


def test_web_post_autotune_persona_revision_null_allowed():
    """persona_revision=None 显式允许（表示无人设改写）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(
        h,
        body={
            "action": "apply",
            "persona_revision": None,
            "patch": {"base_threshold": 0.6},
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_persona_revision is None


def test_web_post_autotune_force_with_apply_passthrough():
    """force 字段在 apply action 下也透传（main 侧 apply 不限速但保留字段）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(
        h,
        body={
            "action": "apply",
            "force": True,
            "keywords_patch": {"add": [], "remove": []},
            "persona_revision": "新人设",
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert bridge.last_autotune_force is True
    assert bridge.last_autotune_keywords_patch == {"add": [], "remove": []}
    assert bridge.last_autotune_persona_revision == "新人设"


def test_web_post_autotune_none_body_rejected():
    """body=None → 400（与 post_config/post_groups 行为一致）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body=None)
    assert status == 400
    assert body["ok"] is False


def test_web_post_autotune_non_dict_body_rejected():
    """非 dict body（如字符串）→ 400。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body="not a dict")
    assert status == 400
    assert body["ok"] is False


def test_web_post_autotune_bridge_exception_500():
    """bridge.run_autotune 抛异常 → 500 + ok=false。"""
    bridge = _MockBridge()

    async def raise_fn(body):
        raise RuntimeError("autotune boom")

    bridge.run_autotune = raise_fn
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "analyze"})
    assert status == 500
    assert body["ok"] is False
    assert "autotune boom" in body["error"]


# ---------------------------------------------------------------------- #
# 全部 12 handler 存在
# ---------------------------------------------------------------------- #


def test_web_build_handlers_returns_twelve():
    """build_handlers 返回恰好 12 个 handler（v0.2.8 新增 autotune POST）。"""
    bridge = _MockBridge()
    handlers = build_handlers(bridge)
    expected = {
        "GET /prosocial/status",
        "GET /prosocial/decisions",
        "POST /prosocial/dryrun",
        "GET /prosocial/config",
        "POST /prosocial/config",
        "GET /prosocial/groups",
        "POST /prosocial/groups",
        "GET /prosocial/providers",
        "GET /prosocial/interests",
        "POST /prosocial/interests",
        "GET /prosocial/export",
        "POST /prosocial/autotune",
    }
    assert set(handlers.keys()) == expected
    assert len(handlers) == 12


def test_web_all_handlers_async_callable():
    """12 个 handler 都是 async 可调用。"""
    bridge = _MockBridge()
    handlers = build_handlers(bridge)
    for key, h in handlers.items():
        assert callable(h)
        # asyncio.run 验证可调用且不抛
        asyncio.run(h({}, None))


def test_web_response_format_uniform():
    """成功/失败响应格式统一（含 ok 字段）。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["GET /prosocial/status"]
    _, body = _run(h)
    assert "ok" in body
