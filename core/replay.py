"""历史回放引擎（模块 E 产出，对应 PRD F8）。

ReplayEngine：读取 ``data/plugin_data/astrbot_plugin_proactive_social/replay/<名称>.jsonl``，
按时间戳差值/speed 睡眠，逐条喂入 feed_fn（通常是 scheduler.on_message 的包装）。
回放期间调用方负责"强制不发送"语义（PRD F8：回放强制不发送，等同 DRY_RUN）。

不 import astrbot，仅依赖标准库。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# 回放消息必须包含的字段（缺一返回 None）
_REQUIRED_FIELDS: tuple[str, ...] = ("ts", "group_id", "user_id", "nickname", "text")


class ReplayEngine:
    """历史回放引擎。

    data_dir : 插件数据目录（data/plugin_data/astrbot_plugin_proactive_social/）
    log_fn   : 日志回调 (level, msg)
    """

    def __init__(self, data_dir: Path, log_fn: Callable[[str, str], None]):
        self._data_dir = data_dir
        self._replay_dir = data_dir / "replay"
        self._log = log_fn

    def list_files(self) -> list[str]:
        """列出 replay/ 目录下所有 *.jsonl 文件名（仅文件名，不含路径）。

        目录不存在或为空 → 返回空列表。
        """
        if not self._replay_dir.exists():
            return []
        try:
            return sorted(
                p.name
                for p in self._replay_dir.iterdir()
                if p.is_file() and p.suffix == ".jsonl"
            )
        except Exception as e:
            self._log("warning", f"[ProSocial] replay.py: 列出回放文件失败: {e}")
            return []

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """容错解析单行 JSON。

        - strip 后 json.loads 失败 → 返回 None
        - 缺少任一必要字段（ts/group_id/user_id/nickname/text）→ 返回 None
        - 正常 → 返回 {"ts","group_id","user_id","nickname","text"} 字段齐全的 dict
        """
        if not line:
            return None
        s = line.strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        # 字段存在性 + 类型粗校验
        result: dict = {}
        for field in _REQUIRED_FIELDS:
            if field not in obj:
                return None
            val = obj[field]
            if val is None:
                return None
            if field == "ts":
                try:
                    result[field] = float(val)
                except (TypeError, ValueError):
                    return None
            else:
                result[field] = str(val)
        return result

    async def run(
        self,
        path: Path,
        speed: float,
        feed_fn: Callable[[dict], Awaitable[Any]],
        stop_flag: Callable[[], bool] | Any,
    ) -> dict:
        """按 ts 差/speed 睡眠，逐条 feed_fn(msg)；返回统计 {total, fed, skipped}。

        - speed ≤ 0 当 1.0 处理
        - 文件不存在或无法读取 → 返回 {total:0, fed:0, skipped:0}
        - 解析失败行计入 skipped
        - 每条前检查 stop_flag（callable 返回 True 则中断）；首条不 sleep
        - feed_fn 是 async，await 调用；feed_fn 抛异常计入 skipped 不中断
        """
        total = 0
        fed = 0
        skipped = 0

        eff_speed = float(speed) if speed and float(speed) > 0 else 1.0

        # 读取并解析全部行
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception as e:
            self._log("warning", f"[ProSocial] replay.py: 读取回放文件失败 {path}: {e}")
            return {"total": 0, "fed": 0, "skipped": 0}

        parsed: list[dict] = []
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            total += 1
            msg = self.parse_line(line)
            if msg is None:
                skipped += 1
                continue
            parsed.append(msg)

        # 按 ts 升序回放（避免乱序文件导致 sleep 负值）
        parsed.sort(key=lambda m: m["ts"])

        last_ts: float | None = None
        for msg in parsed:
            # 每条前检查 stop_flag
            if _check_stop(stop_flag):
                break
            # 计算与上一条的 ts 差；首条不 sleep
            if last_ts is not None:
                delta = float(msg["ts"]) - last_ts
                if delta > 0:
                    await asyncio.sleep(max(0.0, delta / eff_speed))
            # 再次检查（sleep 期间可能被停止）
            if _check_stop(stop_flag):
                break
            try:
                await feed_fn(msg)
                fed += 1
            except Exception as e:
                self._log("warning", f"[ProSocial] replay.py: feed_fn 异常: {e}")
                skipped += 1
            last_ts = float(msg["ts"])

        return {"total": total, "fed": fed, "skipped": skipped}


def _check_stop(stop_flag: Any) -> bool:
    """统一处理 stop_flag 的多种形态。

    支持：
    - callable：调用 stop_flag() 返回 bool
    - 对象有 is_set() 方法：调用 stop_flag.is_set()
    - 对象有布尔属性：直接 bool(stop_flag)

    任何异常均视为未停止（返回 False），保证回放不被异常中断。
    """
    try:
        if callable(stop_flag):
            return bool(stop_flag())
        is_set = getattr(stop_flag, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        return bool(stop_flag)
    except Exception:
        return False
