"""配置存储管理器（v0.2.7，AIOSQLITE 持久化）。

将全部普通配置从 ``_conf_schema.json`` / ``AstrBotConfig`` 迁移到
独立 SQLite 数据库 + 内存缓存。特殊选择器（``chat_provider_id`` 等）
仍由 ``AstrBotConfig`` 承载，不在 ConfigStore 管理范围——
``main.py`` 的 ``config_getter`` 会合并两源。

设计要点：
- **DEFAULT_CONFIG**：内置全量默认值（从 ``_conf_schema.json`` 迁移，禁止 null）。
- **VALIDATORS**：类型/范围校验表（从 ``main.py`` ``_CONFIG_VALIDATORS`` 迁移）。
- **LIST_KEYS**：list 类型键（校验时特判 ``isinstance(value, list)``）。
- **内存缓存 ``_cache``**：``__init__`` 用 DEFAULT_CONFIG 填充，保证同步可读；
  SQLite 无值时即以默认运行。
- **load**：从 SQLite 读全量 JSON 覆盖默认，缺失键补默认（default 变更兼容）。
- **set_many**：事务性批量写——逐键校验，全过才更新缓存 + 持久化 SQLite。
- **SQLite 设计**：单表 ``config``，单键 ``"main"`` 存整个配置 dict 的 JSON，
  减少 IO、保证原子性。数据库文件位于插件 data 目录下 ``config.db``。
- **连接管理**：懒连接（``_ensure_db``），``close()`` 关闭。

为何弃 KV 转 SQLite：
  AstrBot KV 存储在插件重载时可能被清空或不可用，导致配置丢失。
  使用独立的 SQLite 数据库文件，配置持久化完全由插件自身掌控，
  不受 AstrBot 框架重载机制影响。

校验行为沿用 ``main.py`` ``set_config_view``：bool/int/float 严格 isinstance（int 可
作为 float），不做字符串→数字转换（Web API 传 JSON，数值即数值）。
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

# 由 AstrBotConfig 原生承载的特殊选择器键（ConfigStore 不管理这些，主面板原生渲染）
SPECIAL_KEYS = frozenset({"chat_provider_id", "embedding_provider_id"})


class ConfigStore:
    """配置存储：默认值 + SQLite 持久化覆盖 + 内存缓存。

    普通参数经此管理；特殊选择器（``chat_provider_id``）走 AstrBotConfig 原生，
    二者由 ``main.py`` ``config_getter`` 合并后供 scheduler 读取。
    """

    # 全部普通参数的默认值（从 _conf_schema.json 迁移，不含 SPECIAL_KEYS，禁止 null）。
    # schedule 是 template_list，其 default 的 __template_key 字段已剥离，存为纯 dict 列表。
    DEFAULT_CONFIG: dict = {
        # --- 基础开关 / 人设 ---
        "enable": True,
        "dry_run": False,
        "persona_text": "你是一个友善的群聊机器人。",
        "persona_knowledge": "",
        # --- 群范围 ---
        "group_mode": "whitelist",
        "group_whitelist": [],
        # --- 窗口 ---
        "short_window_size": 8,
        "long_window_size": 20,
        "long_window_top_n": 6,
        "long_window_summarize": False,
        # --- 阈值 / 兴趣修正 ---
        "base_threshold": 0.55,
        "core_interest_modifier": 0.7,
        "general_interest_modifier": 1.0,
        "edge_interest_modifier": 1.3,
        "expecting_modifier": 0.8,
        "personal_threshold": 0.55,
        "hate_similarity_threshold": 0.75,
        # --- 五因子权重 ---
        "w_int": 1.2,
        "w_topic": 0.4,
        "w_resp": 0.8,
        "w_cooldown": 0.5,
        "w_silence": 0.35,
        # --- 批次 / 冷却 ---
        "batch_interval_min": 2.0,
        "batch_interval_max": 5.0,
        "cooldown_messages": 4,
        "expecting_duration": 30,
        "personal_track_timeout": 30,
        "track_irrelevant_msgs": 3,
        # --- 限流 / 缓冲 ---
        "embedding_rate_limit_per_min": 30,
        "buffer_max_size": 200,
        "topic_turn_keywords": ["说正事", "别聊了", "换个话题", "停"],
        # --- 作息 / 轮询 ---
        "schedule": [
            {"start": "09:00", "end": "12:00"},
            {"start": "14:00", "end": "18:00"},
            {"start": "20:00", "end": "23:00"},
        ],
        "schedule_jitter_minutes": 30,
        "poll_interval": 300,
        "poll_jitter": 120,
        "monitoring_duration": 120,
        "group_cooldown": 180,
        # --- 瞥一眼 ---
        "glance_enable": True,
        "glance_group_count": 3,
        "glance_min_score": 0.85,
        "hot_group_msg_limit": 30,
        "silent_group_minutes": 10,
        # --- 回放 ---
        "replay_speed": 1.0,
        # --- 双通道融合 ---
        "enable_rule_channel": True,
        "enable_vector_channel": True,
        "fusion_weight_rule": 0.4,
        "dynamic_fusion_enabled": False,
        "dynamic_alpha_wake": 0.8,
        "dynamic_alpha_short_expect": 0.2,
        # --- 规则通道 ---
        "rule_direct_wakeup_words": [],
        "rule_context_wakeup_words": [],
        "rule_context_threshold": 50,
        "rule_question_enabled": True,
        "rule_question_threshold": 65,
        "rule_score_normalize": 100.0,
        # --- 疲劳 ---
        "fatigue_recovery_rate": 0.1,
        "fatigue_limit": 5.0,
        "fatigue_cost_active": 1.2,
        "fatigue_cost_passive": 0.8,
        "fatigue_cost_track": 0.6,
        "fatigue_cost_glance": 1.5,
        "fatigue_high_modifier": 1.2,
        "fatigue_medium_modifier": 1.1,
        "fatigue_suppress_enabled": True,
        # --- 惯性 / 等待窗口 ---
        "after_reply_probability": 0.7,
        "probability_duration": 30,
        "wait_window_duration_ms": 3000,
        "wait_window_max_extra": 3,
        "proactive_temp_boost": 0.5,
        "proactive_boost_duration": 60,
        # --- 回复关键词匹配（v0.2.5）---
        "reply_keyword_enabled": True,
        "reply_keyword_top_n": 5,
        "reply_keyword_boost_factor": 0.25,
        "reply_keyword_ttl_seconds": 60,
        "reply_keyword_min_score_to_trigger": 0.5,
        "reply_keyword_early_clear_low_score": 0.1,
        # --- 兴趣生成（v0.2.6）---
        "interest_example_count": 3,
        "interest_keyword_count": 12,
        "long_window_inject_proactive": True,
    }

    # 类型/范围校验表：(类型, 下限, 上限)；None 表示不校验该侧。
    # list 类型键（LIST_KEYS）与 schedule / group_mode 不在此表，在 _validate 中特判。
    VALIDATORS: dict[str, tuple[type, float | None, float | None]] = {
        "dry_run": (bool, None, None),
        "base_threshold": (float, 0.0, 2.0),
        "personal_threshold": (float, 0.0, 2.0),
        "hate_similarity_threshold": (float, 0.0, 1.0),
        "w_int": (float, 0.0, 5.0),
        "w_topic": (float, 0.0, 5.0),
        "w_resp": (float, 0.0, 5.0),
        "w_cooldown": (float, 0.0, 5.0),
        "w_silence": (float, 0.0, 5.0),
        "core_interest_modifier": (float, 0.0, 3.0),
        "general_interest_modifier": (float, 0.0, 3.0),
        "edge_interest_modifier": (float, 0.0, 3.0),
        "expecting_modifier": (float, 0.0, 2.0),
        "batch_interval_min": (float, 0.1, 60.0),
        "batch_interval_max": (float, 0.1, 60.0),
        "cooldown_messages": (int, 0, 1000),
        "expecting_duration": (int, 0, 3600),
        "personal_track_timeout": (int, 0, 3600),
        "track_irrelevant_msgs": (int, 0, 100),
        "poll_interval": (int, 1, 86400),
        "poll_jitter": (int, 0, 86400),
        "monitoring_duration": (int, 1, 86400),
        "group_cooldown": (int, 0, 86400),
        "glance_enable": (bool, None, None),
        "glance_group_count": (int, 1, 50),
        "glance_min_score": (float, 0.0, 1.0),
        "hot_group_msg_limit": (int, 1, 10000),
        "silent_group_minutes": (int, 0, 1440),
        # v0.2 双通道融合 / 疲劳 / 惯性 / 等待窗口校验
        "enable_rule_channel": (bool, None, None),
        "enable_vector_channel": (bool, None, None),
        "fusion_weight_rule": (float, 0.0, 1.0),
        "dynamic_fusion_enabled": (bool, None, None),
        "dynamic_alpha_wake": (float, 0.0, 1.0),
        "dynamic_alpha_short_expect": (float, 0.0, 1.0),
        "rule_context_threshold": (int, 0, 150),
        "rule_question_enabled": (bool, None, None),
        "rule_question_threshold": (int, 0, 100),
        "rule_score_normalize": (float, 1.0, 1000.0),
        "fatigue_recovery_rate": (float, 0.0, 10.0),
        "fatigue_limit": (float, 0.0, 100.0),
        "fatigue_cost_active": (float, 0.0, 10.0),
        "fatigue_cost_passive": (float, 0.0, 10.0),
        "fatigue_cost_track": (float, 0.0, 10.0),
        "fatigue_cost_glance": (float, 0.0, 10.0),
        "fatigue_high_modifier": (float, 0.0, 3.0),
        "fatigue_medium_modifier": (float, 0.0, 3.0),
        "fatigue_suppress_enabled": (bool, None, None),
        "after_reply_probability": (float, 0.0, 1.0),
        "probability_duration": (int, 0, 3600),
        "wait_window_duration_ms": (int, 0, 60000),
        "wait_window_max_extra": (int, 0, 100),
        "proactive_temp_boost": (float, 0.0, 1.0),
        "proactive_boost_duration": (int, 0, 3600),
        # v0.2.5 回复关键词匹配校验
        "reply_keyword_enabled": (bool, None, None),
        "reply_keyword_top_n": (int, 1, 20),
        "reply_keyword_boost_factor": (float, 0.0, 2.0),
        "reply_keyword_ttl_seconds": (int, 1, 3600),
        "reply_keyword_min_score_to_trigger": (float, 0.0, 1.0),
        "reply_keyword_early_clear_low_score": (float, 0.0, 1.0),
        # v0.2.6 兴趣生成 / 长窗口注入
        "interest_example_count": (int, 1, 10),
        "interest_keyword_count": (int, 3, 30),
        "long_window_inject_proactive": (bool, None, None),
    }

    # list 类型键（校验时 isinstance list）；schedule 单独特判 dict 列表
    LIST_KEYS = frozenset(
        {
            "group_whitelist",
            "topic_turn_keywords",
            "rule_direct_wakeup_words",
            "rule_context_wakeup_words",
        }
    )

    def __init__(self, db_path: Path):
        # 用 DEFAULT_CONFIG 浅拷贝填充缓存（默认值不可变项可直接共享；
        # list/dict 默认值被改时为外部写入，DEFAULT_CONFIG 本体不应被改）
        self._cache = dict(self.DEFAULT_CONFIG)
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """确保数据库连接和表结构存在（懒初始化）。"""
        if self._db is None:
            # 确保父目录存在
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(self._db_path))
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
            )
            await self._db.commit()
        return self._db

    async def load(self) -> None:
        """从 SQLite 读取覆盖项，merge 进缓存。

        数据库无值 / 异常 / 非法 JSON 时保持默认（缓存不变），保证插件可用。
        缺失键补默认（default 变更兼容：版本升级新增键时自动补齐）。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", ("main",)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return
            raw = row[0]
            if not raw:
                return
            try:
                stored = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(stored, dict):
                return
            # merge：SQLite 覆盖默认；缺失键保持默认（已在 _cache，default 变更兼容）
            for k in self.DEFAULT_CONFIG:
                if k in stored:
                    self._cache[k] = stored[k]
        except Exception:
            # SQLite 读取异常，保持默认，不抛
            return

    def get(self) -> dict:
        """返回内存缓存引用（供 config_getter 合并用，直接返回引用即可）。

        scheduler 每次决策实时读取，热更新语义：set_many 改缓存后立即生效。
        """
        return self._cache

    def snapshot(self) -> dict:
        """返回缓存浅拷贝（供 Web API GET，避免外部修改污染缓存）。"""
        return dict(self._cache)

    async def set_many(self, updates: dict) -> tuple[bool, str]:
        """批量校验 + 写入（事务性）。

        全部校验通过才更新缓存并持久化 SQLite；任一失败立即返回 ``(False, 原因)``，
        缓存与 SQLite 均不改。返回 ``(True, "")`` 表示成功。
        """
        if not isinstance(updates, dict):
            return False, "updates 必须是 JSON 对象"
        # 1. 逐键校验，全过才继续
        for k, v in updates.items():
            if k in SPECIAL_KEYS:
                return False, f"{k} 是特殊选择器，请在主面板配置"
            if k not in self.DEFAULT_CONFIG:
                return False, f"未知配置项: {k}"
            ok, msg = self._validate(k, v)
            if not ok:
                return False, msg
        # 2. 全部校验通过，更新缓存
        for k, v in updates.items():
            self._cache[k] = v
        # 3. 持久化全量到 SQLite（单键 "main" 存整个 dict 的 JSON）
        try:
            db = await self._ensure_db()
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                ("main", json.dumps(self._cache, ensure_ascii=False)),
            )
            await db.commit()
        except Exception as e:
            # SQLite 写失败：缓存已改但未持久化，下次 load 会丢失本次缓存改动
            # 不回滚缓存（已生效的热更新仍有效），仅返回失败让上层感知
            return False, f"SQLite 写入失败: {e}"
        return True, ""

    async def close(self) -> None:
        """关闭数据库连接（插件 terminate 时调用）。"""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------ #
    # 通用 KV 存储（复用同一 SQLite db，替代 AstrBot KV 存储）
    # ------------------------------------------------------------------ #
    # metrics / decision_log / fatigue / group_enable / interest_rejected
    # 等非配置数据经此存取，每个 key 存一段 JSON 串。与配置的 "main" 键共用
    # config 表，彻底脱离 AstrBot KV（避免插件重载时 KV 不可用/被清空）。

    async def get_kv(self, key: str, default=None):
        """通用 KV 读取：从 SQLite 读 key 对应的 JSON 值。

        key 不存在 / 异常 / 非法 JSON 时返回 ``default``。
        """
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT value FROM config WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return default
            raw = row[0]
            if raw is None:
                return default
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                return default
        except Exception:
            return default

    async def set_kv(self, key: str, value) -> None:
        """通用 KV 写入：将 value 序列化为 JSON 存入 SQLite。

        value 必须是 JSON 可序列化对象（dict/list/str/int/float/bool/None）。
        """
        try:
            db = await self._ensure_db()
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            await db.commit()
        except Exception:
            # 写失败不抛（调用方自行决定是否感知；与原 KV 行为一致）
            pass

    async def delete_kv(self, key: str) -> None:
        """通用 KV 删除（可选操作，目前未使用但保留接口）。"""
        try:
            db = await self._ensure_db()
            await db.execute("DELETE FROM config WHERE key = ?", (key,))
            await db.commit()
        except Exception:
            pass

    def _validate(self, key: str, value) -> tuple[bool, str]:
        """校验单键，返回 ``(ok, msg)``。

        - LIST_KEYS：要求 ``isinstance(value, list)``
        - schedule：要求 dict 列表
        - group_mode：要求 whitelist / all
        - VALIDATORS：按类型严格 isinstance + 范围检查
        - 无规则的键（如 enable / persona_text 等字符串/开关）：通过
        """
        if key in self.LIST_KEYS:
            if not isinstance(value, list):
                return False, f"{key} 必须是列表"
            return True, ""
        if key == "schedule":
            if not isinstance(value, list) or not all(
                isinstance(x, dict) for x in value
            ):
                return False, f"{key} 必须是 dict 列表"
            return True, ""
        if key == "group_mode":
            if value not in ("whitelist", "all"):
                return False, f"{key} 必须是 whitelist 或 all"
            return True, ""
        rule = self.VALIDATORS.get(key)
        if rule is None:
            # 无校验规则的键（enable / persona_text / *_provider_id /
            # short_window_size / long_window_* / embedding_rate_limit_per_min /
            # buffer_max_size / schedule_jitter_minutes / replay_speed 等）直接通过
            return True, ""
        typ, lo, hi = rule
        if typ is bool:
            if not isinstance(value, bool):
                return False, f"{key} 必须是布尔值"
            return True, ""
        if typ is int:
            # bool 是 int 子类，需排除（沿用 main.py 行为）
            if isinstance(value, bool) or not isinstance(value, int):
                return False, f"{key} 必须是整数"
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                return False, f"{key} 超出范围 [{lo}, {hi}]"
            return True, ""
        if typ is float:
            # 允许 int → float（isinstance(value, (int, float))）；bool 需排除
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, f"{key} 必须是数值"
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                return False, f"{key} 超出范围 [{lo}, {hi}]"
            return True, ""
        # 未知类型（不应出现），保守放行
        return True, ""
