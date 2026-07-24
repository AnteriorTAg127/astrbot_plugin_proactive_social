"""v0.3.6 调参历史持久化。

独立 SQLite 数据库 ``tune_history.db``，与 ``config.db`` 分离避免影响配置表性能。
本模块禁 import astrbot，保证可离线单元测试。

设计要点：
- **懒连接**（``_ensure_db``），``close()`` 关闭。
- ``record`` 插入一条记录，patch/keywords_patch 用 JSON 序列化，失败不抛异常。
- ``list`` 按 timestamp DESC 分页查询，patch/keywords_patch 反序列化为 dict。
- ``clear`` 清空全部记录，``get_stats`` 返回汇总统计。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import aiosqlite


class TuneHistoryStore:
    """LLM 调参历史持久化（独立 SQLite，与 config.db 分离）。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """确保数据库连接和表结构存在（懒初始化）。"""
        if self._db is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(self._db_path))
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tune_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    patch_json TEXT,
                    keywords_patch_json TEXT,
                    persona_revision TEXT,
                    analysis TEXT,
                    expected_effect TEXT,
                    applied INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tune_history_ts "
                "ON tune_history(timestamp DESC)"
            )
            await self._db.commit()
        return self._db

    async def record(
        self,
        *,
        action: str,
        source: str,
        patch: dict | None,
        keywords_patch: dict | None,
        persona_revision: str | None,
        analysis: str,
        expected_effect: str,
        applied: bool,
    ) -> int:
        """插入一条调参历史记录，返回插入的 id（失败返回 0）。"""
        try:
            db = await self._ensure_db()
            cursor = await db.execute(
                """
                INSERT INTO tune_history
                    (timestamp, action, source, patch_json, keywords_patch_json,
                     persona_revision, analysis, expected_effect, applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    action,
                    source,
                    json.dumps(patch, ensure_ascii=False)
                    if patch is not None
                    else None,
                    json.dumps(keywords_patch, ensure_ascii=False)
                    if keywords_patch is not None
                    else None,
                    persona_revision,
                    analysis,
                    expected_effect,
                    int(applied),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)
        except Exception as e:
            print(f"[tune_history] record 失败: {e}", file=sys.stderr)
            return 0

    async def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """按 timestamp DESC 分页查询历史记录。"""
        try:
            db = await self._ensure_db()
            async with db.execute(
                """
                SELECT id, timestamp, action, source, patch_json,
                       keywords_patch_json, persona_revision, analysis,
                       expected_effect, applied
                FROM tune_history
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
            results: list[dict] = []
            for row in rows:
                patch = None
                if row[4] is not None:
                    try:
                        patch = json.loads(row[4])
                    except (json.JSONDecodeError, TypeError):
                        patch = None
                keywords_patch = None
                if row[5] is not None:
                    try:
                        keywords_patch = json.loads(row[5])
                    except (json.JSONDecodeError, TypeError):
                        keywords_patch = None
                results.append(
                    {
                        "id": row[0],
                        "timestamp": row[1],
                        "action": row[2],
                        "source": row[3],
                        "patch": patch,
                        "keywords_patch": keywords_patch,
                        "persona_revision": row[6],
                        "analysis": row[7],
                        "expected_effect": row[8],
                        "applied": bool(row[9]),
                    }
                )
            return results
        except Exception as e:
            print(f"[tune_history] list 失败: {e}", file=sys.stderr)
            return []

    async def clear(self) -> int:
        """清空全部调参历史记录，返回删除条数（异常返回 0）。"""
        try:
            db = await self._ensure_db()
            cursor = await db.execute("DELETE FROM tune_history")
            await db.commit()
            return int(cursor.rowcount or 0)
        except Exception as e:
            print(f"[tune_history] clear 失败: {e}", file=sys.stderr)
            return 0

    async def get_stats(self) -> dict:
        """返回调参历史汇总统计。

        v0.3.7：apply_count 改为统计 ``applied=1`` 的记录数（不再依赖 action="apply"），
        因为 apply 现在更新最近一条 analyze 记录的 applied 字段而非新增记录。
        """
        try:
            db = await self._ensure_db()
            async with db.execute("SELECT COUNT(*) FROM tune_history") as cursor:
                total = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM tune_history WHERE action = ?", ("analyze",)
            ) as cursor:
                analyze_count = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM tune_history WHERE applied = 1"
            ) as cursor:
                apply_count = (await cursor.fetchone())[0]
            async with db.execute("SELECT MAX(timestamp) FROM tune_history") as cursor:
                last_timestamp = (await cursor.fetchone())[0]
            return {
                "total": total,
                "analyze_count": analyze_count,
                "apply_count": apply_count,
                "last_timestamp": last_timestamp,
            }
        except Exception as e:
            print(f"[tune_history] get_stats 失败: {e}", file=sys.stderr)
            return {
                "total": 0,
                "analyze_count": 0,
                "apply_count": 0,
                "last_timestamp": None,
            }

    async def mark_applied(self, source: str) -> bool:
        """v0.3.7：标记最近一条未应用的 analyze 记录为已应用。

        查找最近一条 ``action="analyze" AND applied=0 AND source=相同`` 的记录，
        更新其 ``applied=1``。返回 True 表示找到并更新，False 表示没找到。

        用于 apply 路径避免重复记录：analyze 已记录建议，apply 时只需标记为已应用，
        不再新增 action="apply" 记录，避免同一建议在历史中显示两次。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                """
                SELECT id FROM tune_history
                WHERE action = 'analyze' AND applied = 0 AND source = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (source,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            record_id = row[0]
            await db.execute(
                "UPDATE tune_history SET applied = 1 WHERE id = ?",
                (record_id,),
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"[tune_history] mark_applied 失败: {e}", file=sys.stderr)
            return False

    async def close(self) -> None:
        """关闭数据库连接（插件 terminate 时调用）。"""
        if self._db is not None:
            await self._db.close()
            self._db = None
