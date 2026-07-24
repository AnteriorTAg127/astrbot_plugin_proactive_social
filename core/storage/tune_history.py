"""v0.3.6 调参历史持久化。

独立 SQLite 数据库 ``tune_history.db``，与 ``config.db`` 分离避免影响配置表性能。
本模块禁 import astrbot，保证可离线单元测试。

设计要点：
- **懒连接**（``_ensure_db``），``close()`` 关闭。
- ``record`` 插入一条记录，patch/keywords_patch 用 JSON 序列化，失败不抛异常。
- ``list`` 按 timestamp DESC 分页查询，patch/keywords_patch 反序列化为 dict。
- ``clear`` 清空全部记录，``get_stats`` 返回汇总统计。

v0.3.10 扩展：
- 表新增 8 字段（original/pre_apply/applied_values + diagnosis/plan + status/approved_by/error_msg），
  旧表通过 ``_migrate_legacy_columns`` ALTER TABLE 升级，旧记录新字段为 NULL。
- ``record`` 新增 original_values/diagnosis/plan/status/error_msg 关键字参数（向后兼容）。
- ``list`` 新增 status_filter/include_archived/hide_days 参数，返回 dict 含 8 个新字段；
  旧记录 ``status`` 为 NULL 时按 applied/action 推断（不写回表）。
- 新增 update_plan/update_status/record_apply/dedupe_pending/get_pending/get_by_id 六方法，
  支持批准工作流（pending→approved→applied 状态机）与参数级去重。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import aiosqlite

# v0.3.10 新增的 8 个字段名（全部 TEXT，默认 NULL），用于建表与旧表迁移。
_NEW_COLUMNS: tuple[str, ...] = (
    "original_values_json",
    "pre_apply_values_json",
    "applied_values_json",
    "diagnosis",
    "plan_json",
    "status",
    "approved_by",
    "error_msg",
)


def _loads(value):
    """安全 JSON 反序列化：None 或解析失败返回 None。"""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


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
                    applied INTEGER NOT NULL DEFAULT 0,
                    original_values_json TEXT,
                    pre_apply_values_json TEXT,
                    applied_values_json TEXT,
                    diagnosis TEXT,
                    plan_json TEXT,
                    status TEXT,
                    approved_by TEXT,
                    error_msg TEXT
                )
                """
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tune_history_ts "
                "ON tune_history(timestamp DESC)"
            )
            await self._db.commit()
            # 旧表升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表补字段，需 ALTER。
            await self._migrate_legacy_columns()
        return self._db

    async def _migrate_legacy_columns(self) -> None:
        """检测旧表缺新字段时 ALTER TABLE ADD COLUMN（SQLite 兼容）。

        SQLite 不支持 IF NOT EXISTS for ADD COLUMN，需先 PRAGMA table_info 检查。
        旧记录的 status 字段在 SELECT 时按下面规则推断（在 list/get_by_id 里实现，不写回表）：
        - applied=1 → 'applied'
        - applied=0 AND action='analyze' → 'pending'
        - applied=0 AND action='apply' → 'applied'

        异常时 print 到 stderr 但不抛，避免阻塞插件初始化。
        """
        try:
            db = self._db
            if db is None:
                return
            async with db.execute("PRAGMA table_info(tune_history)") as cursor:
                rows = await cursor.fetchall()
            existing = {row[1] for row in rows}  # row[1] = 列名
            for col in _NEW_COLUMNS:
                if col not in existing:
                    await db.execute(f"ALTER TABLE tune_history ADD COLUMN {col} TEXT")
            await db.commit()
        except Exception as e:
            print(
                f"[tune_history] _migrate_legacy_columns 失败: {e}",
                file=sys.stderr,
            )

    def _row_to_dict(self, row) -> dict:
        """把 SELECT 行转为含全字段的 dict（供 list/get_pending/get_by_id 共用）。

        列序：id, timestamp, action, source, patch_json, keywords_patch_json,
        persona_revision, analysis, expected_effect, applied,
        original_values_json, pre_apply_values_json, applied_values_json,
        diagnosis, plan_json, status, approved_by, error_msg。
        """
        # 旧记录 status 为 NULL 时按 applied/action 推断（不写回表）。
        status = row[15]
        if status is None:
            if row[9] == 1:  # applied=1
                status = "applied"
            elif row[2] == "analyze":  # action='analyze' 且 applied=0
                status = "pending"
            else:  # action='apply' 且 applied=0
                status = "applied"
        return {
            # 旧字段（向后兼容）
            "id": row[0],
            "timestamp": row[1],
            "action": row[2],
            "source": row[3],
            "patch": _loads(row[4]),
            "keywords_patch": _loads(row[5]),
            "persona_revision": row[6],
            "analysis": row[7],
            "expected_effect": row[8],
            "applied": bool(row[9]),
            # v0.3.10 新字段
            "original_values": _loads(row[10]),
            "pre_apply_values": _loads(row[11]),
            "applied_values": _loads(row[12]),
            "diagnosis": row[13],
            "plan": _loads(row[14]),
            "status": status,
            "approved_by": row[16],
            "error_msg": row[17],
        }

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
        original_values: dict | None = None,
        diagnosis: str | None = None,
        plan: list | None = None,
        status: str | None = "pending",
        error_msg: str | None = None,
    ) -> int:
        """插入一条调参历史记录，返回插入的 id（失败返回 0）。

        v0.3.10 新增 original_values/diagnosis/plan/status/error_msg 关键字参数，
        全部默认 None/\"pending\"，旧调用方不传仍可工作。pre_apply_values /
        applied_values / approved_by 在 record 时不写（由 record_apply 写入）。
        """
        try:
            db = await self._ensure_db()
            cursor = await db.execute(
                """
                INSERT INTO tune_history
                    (timestamp, action, source, patch_json, keywords_patch_json,
                     persona_revision, analysis, expected_effect, applied,
                     original_values_json, pre_apply_values_json, applied_values_json,
                     diagnosis, plan_json, status, approved_by, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(original_values, ensure_ascii=False)
                    if original_values is not None
                    else None,
                    None,  # pre_apply_values_json：record 时不写
                    None,  # applied_values_json：record 时不写
                    diagnosis,
                    json.dumps(plan, ensure_ascii=False) if plan is not None else None,
                    status if status is not None else "pending",
                    None,  # approved_by：record 时不写
                    error_msg,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)
        except Exception as e:
            print(f"[tune_history] record 失败: {e}", file=sys.stderr)
            return 0

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        status_filter: str | None = None,
        include_archived: bool = False,
        hide_days: int | None = None,
    ) -> list[dict]:
        """按 timestamp DESC 分页查询历史记录。

        v0.3.10 扩展：
        - ``status_filter``：WHERE status = ?（None 时不过滤）。
        - ``include_archived``：False 时（默认）只返回 status='pending'/
          'pending_diagnosis' 或 timestamp >= now - hide_days*86400 的记录；
          True 时返回全部。
        - ``hide_days``：None 时不做时间过滤（兼容旧调用方，返回全部非归档记录）。
        - 返回 dict 含 8 个新字段；旧记录 status 为 NULL 时按 applied/action 推断。
        """
        try:
            db = await self._ensure_db()
            where_clauses: list[str] = []
            params: list = []
            if status_filter is not None:
                where_clauses.append("status = ?")
                params.append(status_filter)
            if not include_archived:
                if hide_days is not None:
                    where_clauses.append(
                        "(status IN ('pending', 'pending_diagnosis') OR timestamp >= ?)"
                    )
                    params.append(time.time() - hide_days * 86400)
                # hide_days is None：不做时间过滤，等价返回全部。
            where_sql = (
                ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            )
            sql = (
                "SELECT id, timestamp, action, source, patch_json, "
                "keywords_patch_json, persona_revision, analysis, expected_effect, "
                "applied, original_values_json, pre_apply_values_json, "
                "applied_values_json, diagnosis, plan_json, status, approved_by, "
                "error_msg FROM tune_history "
                f"{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            )
            params.extend([limit, offset])
            async with db.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
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

    async def update_plan(self, record_id: int, plan: list) -> bool:
        """两轮模式第二轮：更新指定记录的 plan + status='pending'。

        返回 True 表示更新成功，False 表示记录不存在或异常。
        """
        try:
            db = await self._ensure_db()
            cursor = await db.execute(
                "UPDATE tune_history SET plan_json = ?, status = 'pending' "
                "WHERE id = ?",
                (json.dumps(plan, ensure_ascii=False), record_id),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0
        except Exception as e:
            print(f"[tune_history] update_plan 失败: {e}", file=sys.stderr)
            return False

    async def update_status(
        self, record_id: int, status: str, approved_by: str | None = None
    ) -> bool:
        """通用状态更新。

        status 可选：pending/pending_diagnosis/approved/rejected/applied/failed/
        superseded。approved_by 非 None 时同时更新 approved_by 字段。
        返回 True 表示更新成功，False 表示记录不存在或异常。
        """
        try:
            db = await self._ensure_db()
            if approved_by is not None:
                cursor = await db.execute(
                    "UPDATE tune_history SET status = ?, approved_by = ? WHERE id = ?",
                    (status, approved_by, record_id),
                )
            else:
                cursor = await db.execute(
                    "UPDATE tune_history SET status = ? WHERE id = ?",
                    (status, record_id),
                )
            await db.commit()
            return int(cursor.rowcount or 0) > 0
        except Exception as e:
            print(f"[tune_history] update_status 失败: {e}", file=sys.stderr)
            return False

    async def record_apply(
        self,
        record_id: int,
        pre_apply_values: dict,
        applied_values: dict,
        approved_by: str,
    ) -> bool:
        """apply 成功后更新：写入 pre_apply_values + applied_values + status='applied' + approved_by。

        返回 True 表示更新成功，False 表示记录不存在或异常。
        """
        try:
            db = await self._ensure_db()
            cursor = await db.execute(
                "UPDATE tune_history SET pre_apply_values_json = ?, "
                "applied_values_json = ?, status = 'applied', approved_by = ? "
                "WHERE id = ?",
                (
                    json.dumps(pre_apply_values, ensure_ascii=False),
                    json.dumps(applied_values, ensure_ascii=False),
                    approved_by,
                    record_id,
                ),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0
        except Exception as e:
            print(f"[tune_history] record_apply 失败: {e}", file=sys.stderr)
            return False

    async def dedupe_pending(self, new_record_id: int, new_plan_keys: list[str]) -> int:
        """参数级去重：扫描所有 status='pending' AND id != new_record_id 的记录，
        从其 plan 里删除 new_plan_keys 中的参数；plan 删空则 status='superseded'。

        返回被修改的记录数（异常返回 0）。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT id, plan_json FROM tune_history "
                "WHERE status = 'pending' AND id != ?",
                (new_record_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            modified = 0
            for row in rows:
                rid = row[0]
                plan = _loads(row[1])
                if not isinstance(plan, list):
                    continue
                new_plan = [
                    item
                    for item in plan
                    if isinstance(item, dict) and item.get("key") not in new_plan_keys
                ]
                if len(new_plan) == len(plan):
                    continue  # 无重叠参数，无需修改
                if not new_plan:
                    await db.execute(
                        "UPDATE tune_history SET status = 'superseded' WHERE id = ?",
                        (rid,),
                    )
                else:
                    await db.execute(
                        "UPDATE tune_history SET plan_json = ? WHERE id = ?",
                        (json.dumps(new_plan, ensure_ascii=False), rid),
                    )
                modified += 1
            await db.commit()
            return modified
        except Exception as e:
            print(f"[tune_history] dedupe_pending 失败: {e}", file=sys.stderr)
            return 0

    async def get_pending(self, limit: int = 5) -> list[dict]:
        """返回最新 N 条 status='pending' 记录（按 timestamp DESC）。

        异常返回空列表。返回字段与 list() 一致。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT id, timestamp, action, source, patch_json, "
                "keywords_patch_json, persona_revision, analysis, expected_effect, "
                "applied, original_values_json, pre_apply_values_json, "
                "applied_values_json, diagnosis, plan_json, status, approved_by, "
                "error_msg FROM tune_history "
                "WHERE status = 'pending' ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            print(f"[tune_history] get_pending 失败: {e}", file=sys.stderr)
            return []

    async def get_by_id(self, record_id: int) -> dict | None:
        """单条查询，返回字段与 list() 一致。不存在返回 None。

        异常返回 None。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT id, timestamp, action, source, patch_json, "
                "keywords_patch_json, persona_revision, analysis, expected_effect, "
                "applied, original_values_json, pre_apply_values_json, "
                "applied_values_json, diagnosis, plan_json, status, approved_by, "
                "error_msg FROM tune_history WHERE id = ?",
                (record_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_dict(row)
        except Exception as e:
            print(f"[tune_history] get_by_id 失败: {e}", file=sys.stderr)
            return None

    async def close(self) -> None:
        """关闭数据库连接（插件 terminate 时调用）。"""
        if self._db is not None:
            await self._db.close()
            self._db = None
