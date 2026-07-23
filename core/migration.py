"""v0.2.7 KV→SQLite 数据迁移（main.py 方法提取，模块 A 产出）。

将 main.py 中 `_migrate_kv_to_sqlite` 改为模块级 async 函数：
- 接收 plugin 实例，方法体内所有 self.xxx 改为 plugin.xxx
- 仅首次执行，迁移完成后标记 _kv_migrated 避免重复

依赖 plugin._config_store / plugin.get_kv_data / plugin.interest_mgr / plugin._log。
"""

from __future__ import annotations

import json


async def migrate_kv_to_sqlite(plugin) -> None:
    """v0.2.7 数据迁移：从旧 AstrBot KV 迁移到 SQLite（仅首次执行）。

    v0.2.7 之前所有数据（config / group_enable / decision_log / metrics /
    fatigue / interest_rejected）存在 AstrBot KV。v0.2.7 迁移到独立 SQLite
    后，若不迁移旧数据，配置回到默认值（group_mode=whitelist）会导致群未启用、
    不采集消息。此方法在首次启动时将旧 KV 数据读出写入 SQLite，迁移完成后
    标记 ``_kv_migrated`` 避免重复执行。
    """
    try:
        migrated = await plugin._config_store.get_kv("_kv_migrated")
        if migrated:
            return
    except Exception:
        return

    plugin._log("info", "v0.2.7 首次启动，从旧 AstrBot KV 迁移数据到 SQLite...")

    # 1. 迁移配置（旧 KV "config" 键存的是整段 JSON 字符串，SQLite 存为 "main" 键）
    try:
        old_config_raw = await plugin.get_kv_data("config", None)
        if old_config_raw:
            stored = (
                json.loads(old_config_raw)
                if isinstance(old_config_raw, str)
                else old_config_raw
            )
            if isinstance(stored, dict):
                # 合并合法键到缓存（绕过 set_many 校验，旧数据已通过 v0.2.6 校验）
                cache = plugin._config_store.get()
                for k in plugin._config_store.DEFAULT_CONFIG:
                    if k in stored:
                        cache[k] = stored[k]
                # 写 SQLite "main" 键（与 load()/set_many() 的键名一致）
                await plugin._config_store.set_kv("main", cache)
                plugin._log("info", "配置已从旧 KV 迁移到 SQLite")
    except Exception as e:
        plugin._log("warning", f"迁移 config 失败（继续用默认值）: {e}")

    # 2. 迁移其他 KV 数据（group_enable/decision_log/metrics/fatigue）
    for key in ("group_enable", "decision_log", "metrics", "fatigue"):
        try:
            old_val = await plugin.get_kv_data(key, None)
            if old_val is None:
                continue
            # AstrBot KV 自动序列化：对象读回来仍是对象；但 interest_rejected
            # 之前存的是 json.dumps 字符串，需 json.loads（下面单独处理）
            if isinstance(old_val, str):
                try:
                    old_val = json.loads(old_val)
                except json.JSONDecodeError:
                    continue
            await plugin._config_store.set_kv(key, old_val)
            plugin._log("info", f"{key} 已从旧 KV 迁移到 SQLite")
        except Exception as e:
            plugin._log("warning", f"迁移 {key} 失败: {e}")

    # 3. interest_rejected（旧 KV 存的是 json.dumps 字符串）
    try:
        old_rej = await plugin.get_kv_data("interest_rejected", None)
        if old_rej:
            rej = json.loads(old_rej) if isinstance(old_rej, str) else old_rej
            await plugin._config_store.set_kv("interest_rejected", rej)
            # 同时更新内存中的 interest_mgr
            plugin.interest_mgr.set_rejected(rej)
            plugin._log("info", "interest_rejected 已从旧 KV 迁移到 SQLite")
    except Exception as e:
        plugin._log("warning", f"迁移 interest_rejected 失败: {e}")

    # 4. 标记迁移完成
    try:
        await plugin._config_store.set_kv("_kv_migrated", True)
        plugin._log("info", "v0.2.7 KV→SQLite 迁移完成")
    except Exception as e:
        plugin._log("warning", f"标记迁移完成失败: {e}")
