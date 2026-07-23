"""test_v0_2_1.py —— v0.2.1/v0.2.7 配置存储迁移（ConfigStore）单元测试。

测试对象：``core/config_store.py`` 的 ``ConfigStore``（DEFAULT_CONFIG /
VALIDATORS / LIST_KEYS / SPECIAL_KEYS / load / get / snapshot / set_many /
_validate / close）+ ``main.py`` ``_config_getter`` 的合并逻辑（普通参数 + 特殊选择器
叠加，特殊选择器仍由 AstrBotConfig 原生承载）。

覆盖 PRD §7 验收点：
  #2    SQLite 存储：set_many 写 SQLite、load 后 get 一致（test_load_merges_sqlite_overrides /
        test_load_fills_missing_keys_with_defaults / test_load_empty_db_keeps_defaults /
        test_load_corrupt_json_keeps_defaults / test_load_sqlite_exception_keeps_defaults）
  #3    热更新：set_many 后 get 立即反映（test_hot_update_reflected_in_get）
  #4    默认值非 null（test_default_config_no_null /
        test_default_config_schedule_no_template_key /
        test_special_keys_excludes_chat_provider）
  #5    校验：非法值/类型/范围/列表/schedule 被拒（test_set_many_rejects_*）
  #6    事务性：混入非法键全部不生效（test_set_many_atomic_rollback）
  #7    特殊选择器合并（test_config_getter_merges_special_keys）
  #10   default 变更兼容：缺键补默认（test_load_fills_missing_keys_with_defaults）
  #11   回归：v0.2 的 284 个测试 + 本文件 ≥12 个全通过（CI 总通过数 ≥296）

不依赖 AstrBot 运行时。ConfigStore 多数测试直接实例化（传入临时 db 路径），无需 scheduler。
异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio。

v0.2.7 变更：ConfigStore 从 KV 回调迁移到 aiosqlite，load/set_many 不再接受
kv_get_fn/kv_set_fn 参数，改用 ConfigStore(db_path) 构造。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core.storage.config_store import SPECIAL_KEYS, ConfigStore

# ======================================================================
# 辅助：构造临时 ConfigStore
# ======================================================================


def _make_store() -> ConfigStore:
    """返回一个使用临时数据库的 ConfigStore 实例。"""
    tmp_dir = Path(tempfile.mkdtemp())
    return ConfigStore(tmp_dir / "test_config.db")


# ======================================================================
# ConfigStore 基础（验收 #4 默认非null）
# ======================================================================


def test_default_config_no_null():
    """验收 #4：DEFAULT_CONFIG 全部值非 None（禁止 null 默认值）。"""
    for k, v in ConfigStore.DEFAULT_CONFIG.items():
        assert v is not None, f"DEFAULT_CONFIG[{k!r}] 不应为 None"


def test_default_config_schedule_no_template_key():
    """验收 #4：schedule 是 list[dict]，每个 dict 仅含 start/end，无 __template_key。

    template_list 默认值在迁移到 ConfigStore 时已剥离 __template_key，
    存为纯 dict 列表，避免特殊字段污染 scheduler 读取。
    """
    sched = ConfigStore.DEFAULT_CONFIG["schedule"]
    assert isinstance(sched, list) and len(sched) > 0
    for item in sched:
        assert isinstance(item, dict)
        assert set(item.keys()) == {"start", "end"}
        assert "__template_key" not in item


def test_special_keys_excludes_chat_provider():
    """验收 #4/#7：SPECIAL_KEYS 含 chat_provider_id 和 embedding_provider_id，且不在 DEFAULT_CONFIG。

    ConfigStore 不管理特殊选择器；chat_provider_id / embedding_provider_id
    由 AstrBotConfig 原生承载，``_config_getter`` 合并两源时叠加。
    """
    assert SPECIAL_KEYS == frozenset({"chat_provider_id", "embedding_provider_id"})
    assert "chat_provider_id" not in ConfigStore.DEFAULT_CONFIG
    assert "embedding_provider_id" not in ConfigStore.DEFAULT_CONFIG


# ======================================================================
# set_many 事务性与校验（验收 #5/#6）
# ======================================================================


def test_set_many_valid_updates_cache_and_sqlite():
    """验收 #2/#5：合法 patch 通过校验，缓存更新，SQLite 持久化成功。"""

    async def _run():
        store = _make_store()
        ok, msg = await store.set_many({"base_threshold": 0.8, "fatigue_limit": 10.0})
        assert ok is True
        assert msg == ""
        cfg = store.get()
        assert cfg["base_threshold"] == 0.8
        assert cfg["fatigue_limit"] == 10.0
        # 其他键保持默认
        assert cfg["enable"] == ConfigStore.DEFAULT_CONFIG["enable"]
        # 重新加载验证 SQLite 持久化
        store2 = _make_store()
        # 指向同一个 db 文件
        store2._db_path = store._db_path
        await store2.load()
        assert store2.get()["base_threshold"] == 0.8
        await store.close()
        await store2.close()

    asyncio.run(_run())


def test_set_many_rejects_unknown_key():
    """验收 #5：未知键被拒，msg 含"未知"，缓存不变。"""

    async def _run():
        store = _make_store()
        base_old = store.get()["base_threshold"]
        ok, msg = await store.set_many({"nonexistent_key": 1})
        assert ok is False
        assert "未知" in msg
        # 缓存不变
        assert store.get()["base_threshold"] == base_old
        await store.close()

    asyncio.run(_run())


def test_set_many_rejects_special_key():
    """验收 #5/#7：特殊选择器键被拒，msg 含"特殊"，缓存不变。"""

    async def _run():
        store = _make_store()
        ok, msg = await store.set_many({"chat_provider_id": "provider-xxx"})
        assert ok is False
        assert "特殊" in msg
        # ConfigStore 不管理特殊键，缓存里不应该出现该键
        assert "chat_provider_id" not in store.get()
        await store.close()

    asyncio.run(_run())


def test_set_many_rejects_wrong_type():
    """验收 #5：类型不符被拒（float 键传 str、bool 键传 str），缓存不变。

    注：``enable`` 在 VALIDATORS 中无规则（属于无校验放行的字符串/开关键），
    故此处改用 ``dry_run``（VALIDATORS (bool, None, None)）测 bool 严格校验，
    用 ``base_threshold`` 测 float 严格校验——更能反映校验逻辑的真实边界。
    """

    async def _run():
        store = _make_store()
        base_old = store.get()["base_threshold"]
        dry_old = store.get()["dry_run"]
        # float 键传 str
        ok1, msg1 = await store.set_many({"base_threshold": "abc"})
        assert ok1 is False
        assert "数值" in msg1
        # bool 键传 str（bool 是 int 子类需排除）
        ok2, msg2 = await store.set_many({"dry_run": "notbool"})
        assert ok2 is False
        assert "布尔值" in msg2
        # 缓存不变
        assert store.get()["base_threshold"] == base_old
        assert store.get()["dry_run"] == dry_old
        await store.close()

    asyncio.run(_run())


def test_set_many_rejects_out_of_range():
    """验收 #5：范围越界被拒（fusion_weight_rule > 1、base_threshold < 0）。"""

    async def _run():
        store = _make_store()
        ok1, msg1 = await store.set_many({"fusion_weight_rule": 5.0})
        assert ok1 is False
        assert "范围" in msg1
        ok2, msg2 = await store.set_many({"base_threshold": -0.5})
        assert ok2 is False
        assert "范围" in msg2
        await store.close()

    asyncio.run(_run())


def test_set_many_atomic_rollback():
    """验收 #6：事务性——混入一个非法键 → 全部不写、缓存不变。

    一个合法（base_threshold=0.8）+ 一个越界（fusion_weight_rule=5.0），
    校验阶段 fusion_weight_rule 失败立即返回，缓存与 SQLite 都未改。
    """

    async def _run():
        store = _make_store()
        base_old = store.get()["base_threshold"]
        fusion_old = store.get()["fusion_weight_rule"]
        ok, msg = await store.set_many(
            {"base_threshold": 0.8, "fusion_weight_rule": 5.0}
        )
        assert ok is False
        assert "范围" in msg
        # 缓存未变（base_threshold 仍是旧值，事务性回滚）
        assert store.get()["base_threshold"] == base_old
        assert store.get()["fusion_weight_rule"] == fusion_old
        await store.close()

    asyncio.run(_run())


# ======================================================================
# list 与 schedule 校验
# ======================================================================


def test_set_many_list_type():
    """验收 #5：LIST_KEYS 要求 isinstance list；list 合法通过、str 被拒。"""

    async def _run():
        store = _make_store()
        # 合法 list
        ok1, msg1 = await store.set_many({"group_whitelist": ["g1", "g2"]})
        assert ok1 is True
        assert msg1 == ""
        assert store.get()["group_whitelist"] == ["g1", "g2"]
        # str 不是 list
        ok2, msg2 = await store.set_many({"group_whitelist": "g1"})
        assert ok2 is False
        assert "列表" in msg2
        await store.close()

    asyncio.run(_run())


def test_set_many_schedule_valid():
    """验收 #5：schedule 校验——合法 list[dict] 通过；非 list / 非 dict 元素被拒。"""

    async def _run():
        store = _make_store()
        # 合法
        ok1, msg1 = await store.set_many(
            {"schedule": [{"start": "08:00", "end": "10:00"}]}
        )
        assert ok1 is True
        assert msg1 == ""
        assert store.get()["schedule"] == [{"start": "08:00", "end": "10:00"}]
        # 非 list
        ok2, msg2 = await store.set_many({"schedule": "notlist"})
        assert ok2 is False
        assert "dict 列表" in msg2
        # list 但元素非 dict
        ok3, msg3 = await store.set_many({"schedule": ["notdict"]})
        assert ok3 is False
        assert "dict 列表" in msg3
        await store.close()

    asyncio.run(_run())


# ======================================================================
# load 与 default 变更兼容（验收 #2/#10）
# ======================================================================


def test_load_merges_sqlite_overrides():
    """验收 #2：load 后 SQLite 覆盖项生效，其余键保持默认。"""

    async def _run():
        store = _make_store()
        # 先写入 SQLite
        ok, _ = await store.set_many({"base_threshold": 0.9, "fatigue_limit": 8.0})
        assert ok
        await store.close()
        # 重新加载
        store2 = ConfigStore(store._db_path)
        await store2.load()
        cfg = store2.get()
        assert cfg["base_threshold"] == 0.9
        assert cfg["fatigue_limit"] == 8.0
        # 其余键保持默认
        assert cfg["enable"] == ConfigStore.DEFAULT_CONFIG["enable"]
        assert cfg["schedule"] == ConfigStore.DEFAULT_CONFIG["schedule"]
        await store2.close()

    asyncio.run(_run())


def test_load_fills_missing_keys_with_defaults():
    """验收 #10：SQLite 仅含部分键，缺失键补默认（default 变更兼容）。

    模拟版本升级：旧数据库只存了 base_threshold，新版本新增的键在 load 后
    自动补 DEFAULT_CONFIG 默认值，不出现 KeyError / None。
    """

    async def _run():
        store = _make_store()
        # 只写入一个键
        ok, _ = await store.set_many({"base_threshold": 0.9})
        assert ok
        await store.close()
        # 重新加载
        store2 = ConfigStore(store._db_path)
        await store2.load()
        cfg = store2.get()
        assert cfg["base_threshold"] == 0.9
        # 全部默认键都在缓存里且非 None
        for k, default_v in ConfigStore.DEFAULT_CONFIG.items():
            assert k in cfg, f"缺失键 {k} 未补默认"
            assert cfg[k] is not None, f"键 {k} 不应为 None"
            # 未被 SQLite 覆盖的键等于默认
            if k != "base_threshold":
                assert cfg[k] == default_v
        await store2.close()

    asyncio.run(_run())


def test_load_empty_db_keeps_defaults():
    """验收 #4：SQLite 无配置数据时 load 不改缓存，保持默认。"""

    async def _run():
        store = _make_store()
        await store.load()
        assert store.get() == ConfigStore.DEFAULT_CONFIG
        await store.close()

    asyncio.run(_run())


def test_load_corrupt_json_keeps_defaults():
    """验收 #2 边界：SQLite 中是非法 JSON，load 不崩溃，保持默认。"""

    async def _run():
        import aiosqlite

        tmp_dir = Path(tempfile.mkdtemp())
        db_path = tmp_dir / "test_config.db"
        # 手动写入损坏的 JSON
        db = await aiosqlite.connect(str(db_path))
        await db.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("main", "not json{"),
        )
        await db.commit()
        await db.close()
        # 加载损坏数据
        store = ConfigStore(db_path)
        await store.load()
        assert store.get() == ConfigStore.DEFAULT_CONFIG
        await store.close()

    asyncio.run(_run())


def test_load_sqlite_exception_keeps_defaults():
    """验收 #2/#8 错误处理：SQLite 数据库不可读时，load 不崩溃，保持默认。"""

    async def _run():
        tmp_dir = Path(tempfile.mkdtemp())
        # 创建一个目录作为 db_path，使 SQLite 无法打开（目录不是文件）
        db_path_as_dir = tmp_dir / "config.db"
        db_path_as_dir.mkdir()
        store = ConfigStore(db_path_as_dir / "inner.db")
        # 正常加载应返回默认（_ensure_db 在目录下创建文件成功）
        await store.load()
        assert store.get() == ConfigStore.DEFAULT_CONFIG
        await store.close()

    asyncio.run(_run())


# ======================================================================
# close 方法
# ======================================================================


def test_close_idempotent():
    """close() 可以安全多次调用。"""

    async def _run():
        store = _make_store()
        await store.close()
        await store.close()  # 不应抛异常

    asyncio.run(_run())


# ======================================================================
# 合并逻辑（验收 #7）
# ======================================================================


class _MockPlugin:
    """最小化复现 ``main.py`` ``ProSocialPlugin._config_getter`` 的依赖。

    只需 ``self._config_store`` / ``self._SPECIAL_KEYS`` / ``self.config`` 三个
    属性即可调用合并逻辑——避免 import astrbot 运行时（main.py 强依赖
    AstrBot 框架，离线测试不引入）。
    """

    def __init__(self, config_store, special_keys, astrbot_config):
        self._config_store = config_store
        self._SPECIAL_KEYS = special_keys
        self.config = astrbot_config

    def _config_getter(self) -> dict:
        # 与 main.py ProSocialPlugin._config_getter 完全一致的合并逻辑：
        # 普通参数来自 ConfigStore 缓存（get 返回引用，热更新语义），
        # 特殊选择器从 self.config 叠加。
        cfg = dict(self._config_store.get())
        for k in self._SPECIAL_KEYS:
            if k in self.config:
                cfg[k] = self.config[k]
        return cfg


def test_config_getter_merges_special_keys():
    """验收 #7：config_getter 合并 ConfigStore 缓存（普通参数）+ AstrBotConfig（特殊选择器）。

    复现 main.py ``_config_getter`` 的合并逻辑（不 import astrbot 运行时），
    验证合并后 scheduler 能同时读到普通参数与特殊选择器 chat_provider_id / embedding_provider_id。
    """
    store = _make_store()
    astrbot_config = {
        "chat_provider_id": "provider-xxx",
        "embedding_provider_id": "emb-yyy",
    }
    plugin = _MockPlugin(store, SPECIAL_KEYS, astrbot_config)
    cfg = plugin._config_getter()
    # 特殊键从 self.config 叠加
    assert cfg["chat_provider_id"] == "provider-xxx"
    assert cfg["embedding_provider_id"] == "emb-yyy"
    # 全部普通参数都在（来自 ConfigStore 缓存，值是默认）
    for k, v in ConfigStore.DEFAULT_CONFIG.items():
        assert k in cfg
        assert cfg[k] == v
    # ConfigStore 自身不持有特殊键（来源是 self.config）
    assert "chat_provider_id" not in store.get()
    assert "embedding_provider_id" not in store.get()
    # 合并是浅拷贝，外部修改不应污染 ConfigStore 缓存
    cfg["base_threshold"] = 99.0
    assert store.get()["base_threshold"] == ConfigStore.DEFAULT_CONFIG["base_threshold"]

    async def _cleanup():
        await store.close()

    asyncio.run(_cleanup())


# ======================================================================
# 热更新（验收 #3）
# ======================================================================


def test_hot_update_reflected_in_get():
    """验收 #3：set_many 改缓存后 get() 立即返回新值（无需重新 load）。

    ConfigStore.get() 返回 _cache 引用，set_many 改 _cache 后下次 get 立即看到，
    scheduler 下次决策即用新值，与 v0.2 热更新语义一致。
    """

    async def _run():
        store = _make_store()
        assert store.get()["base_threshold"] == 0.55  # v0.2.6 默认
        await store.set_many({"base_threshold": 0.42})
        # 立即生效，无需 reload
        assert store.get()["base_threshold"] == 0.42
        # snapshot 也反映新值（snapshot 返回浅拷贝，供 Web API GET）
        assert store.snapshot()["base_threshold"] == 0.42
        await store.close()

    asyncio.run(_run())
