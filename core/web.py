"""Web API 处理逻辑（模块 F）。

设计要点：
- 本文件**严禁 import astrbot**，仅用标准库，保证 core/ 可离线单元测试。
- main.py 实现 `WebBridge` 鸭子类型接口（get_status / get_decisions / get_config_view /
  set_config_view / get_groups_view / set_groups_view），并负责通过
  `context.register_web_api` 注册路由、把本模块返回的 `(status, json)` 封装为 HTTP 响应。
- `build_handlers(bridge)` 返回 7 个 async handler，签名统一为
  `async (params: dict, body: dict | None) -> tuple[int, dict]`。
- 统一响应格式：成功 `(200, {"ok": True, "data": ...})`；
  失败 `(400, {"ok": False, "error": "..."})`；内部异常 `(500, {"ok": False, "error": "..."})`。

参考 AstrBot `docs/zh/dev/star/guides/plugin-pages.md`：前端 Page 通过
`window.AstrBotPluginPage` bridge 调用，endpoint 为不含插件名前缀的相对路径
（如 `prosocial/status`），bridge 对普通 JSON 响应 resolve 为完整对象，非 2xx reject。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# handler 类型别名：async (params, body) -> (http_status, json_body)
Handler = Callable[[dict, dict | None], Awaitable[tuple[int, dict]]]


class WebBridge:
    """main.py 实现此接口（鸭子类型），web.py 只依赖这些方法。

    同步方法：get_status / get_decisions / get_config_view / get_groups_view
    异步方法：set_config_view / set_groups_view（返回 (ok, error)）
    """

    def get_status(self) -> dict:  # pragma: no cover - 接口声明
        ...

    def get_decisions(self, limit: int) -> list[dict]:  # pragma: no cover
        ...

    def get_config_view(self) -> dict:  # pragma: no cover
        ...

    async def set_config_view(
        self, patch: dict
    ) -> tuple[bool, str]:  # pragma: no cover
        ...

    def get_groups_view(self) -> dict:  # pragma: no cover
        ...

    async def set_groups_view(
        self, patch: dict
    ) -> tuple[bool, str]:  # pragma: no cover
        ...


def _ok(data: Any) -> tuple[int, dict]:
    """成功响应：200 + {"ok": True, "data": ...}"""
    return 200, {"ok": True, "data": data}


def _err(msg: str, status: int = 400) -> tuple[int, dict]:
    """失败响应：默认 400 + {"ok": False, "error": msg}"""
    return status, {"ok": False, "error": msg}


def build_handlers(bridge: WebBridge) -> dict[str, Handler]:
    """构造 7 个 Web API handler，key 形如 'GET /prosocial/status'。

    main.py 遍历此 dict，按 METHOD/PATH 注册到 `context.register_web_api`，
    并在自身 handler 中解析 query/body 调用对应函数，把返回的 (status, json) 转为响应。
    """

    async def get_status(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            return _ok(bridge.get_status())
        except Exception as e:  # 任何异常不致插件崩溃
            return _err(str(e), 500)

    async def get_decisions(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            raw = params.get("limit", 50)
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                return _err("limit 必须是整数")
            # clamp 到 [1, 500]，避免极端值
            if limit < 1:
                limit = 1
            elif limit > 500:
                limit = 500
            return _ok(bridge.get_decisions(limit))
        except Exception as e:
            return _err(str(e), 500)

    async def post_dryrun(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            # BUG-3: 显式拒绝 None / 非 dict body（原 body = body or {} 把 None 静默转空 dict，
            # 随后 body.get("enabled") 缺失 → 返回 400，但 None 与 {} 行为不一致；统一前置拦截）
            if body is None:
                return _err("请求体不能为空")
            if not isinstance(body, dict):
                return _err("请求体必须是 JSON 对象")
            enabled = body.get("enabled")
            # 严格 bool 校验：不接受 "yes"/1 等隐式真值
            if not isinstance(enabled, bool):
                return _err("enabled 必须是布尔值")
            # 复用 config 写入通道（set_config_view 负责类型/范围校验与 save_config）
            ok, err = await bridge.set_config_view({"dry_run": enabled})
            if not ok:
                return _err(err)
            return _ok({"dry_run": enabled})
        except Exception as e:
            return _err(str(e), 500)

    async def get_config(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            return _ok(bridge.get_config_view())
        except Exception as e:
            return _err(str(e), 500)

    async def post_config(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            # BUG-3: 显式拒绝 None body（原 body = body or {} 把 None 静默转空 dict，
            # 空 patch 合法 → 200，与 dryrun 缺 enabled → 400 行为不一致；PRD §8.14 要求非法参数被拒）
            if body is None:
                return _err("请求体不能为空")
            if not isinstance(body, dict):
                return _err("请求体必须是 JSON 对象")
            ok, err = await bridge.set_config_view(body)
            if not ok:
                return _err(err)
            # 返回已更新键数（set_config_view 事务性：ok 则全量写入；特殊键由 set_many 拒绝）
            return _ok({"updated": len(body)})
        except Exception as e:
            return _err(str(e), 500)

    async def get_groups(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            return _ok(bridge.get_groups_view())
        except Exception as e:
            return _err(str(e), 500)

    async def post_groups(params: dict, body: dict | None) -> tuple[int, dict]:
        try:
            # BUG-3: 显式拒绝 None body（同 post_config，保持三个 POST handler 行为一致）
            if body is None:
                return _err("请求体不能为空")
            if not isinstance(body, dict):
                return _err("请求体必须是 JSON 对象")
            ok, err = await bridge.set_groups_view(body)
            if not ok:
                return _err(err)
            return _ok(bridge.get_groups_view())
        except Exception as e:
            return _err(str(e), 500)

    return {
        "GET /prosocial/status": get_status,
        "GET /prosocial/decisions": get_decisions,
        "POST /prosocial/dryrun": post_dryrun,
        "GET /prosocial/config": get_config,
        "POST /prosocial/config": post_config,
        "GET /prosocial/groups": get_groups,
        "POST /prosocial/groups": post_groups,
    }
