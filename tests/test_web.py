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

from core.web import build_handlers

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
        # F3 LLM 诊断调参
        self.last_autotune_body: dict | None = None

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
        self.last_autotune_body = body
        if body.get("action") == "apply":
            return {
                "ok": True,
                "applied": True,
                "updated": len(body.get("patch") or {}),
            }
        return {
            "ok": True,
            "analysis": "test analysis",
            "suggested_patch": {},
            "expected_effect": "test effect",
            "applied": False,
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
    assert bridge.last_autotune_body == {"action": "analyze"}


def test_web_post_autotune_apply_ok():
    """apply 成功 → 200 + {ok, applied, updated}。"""
    bridge = _MockBridge()
    h = build_handlers(bridge)["POST /prosocial/autotune"]
    status, body = _run(h, body={"action": "apply", "patch": {"base_threshold": 0.7}})
    assert status == 200
    assert body["ok"] is True
    assert body["applied"] is True
    assert body["updated"] == 1


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
