"""test_v0_2_1.py —— v0.2.1 配置存储迁移（ConfigStore）单元测试。

测试对象：``core/config_store.py`` 的 ``ConfigStore``（DEFAULT_CONFIG /
VALIDATORS / LIST_KEYS / SPECIAL_KEYS / load / get / snapshot / set_many /
_validate）+ ``main.py`` ``_config_getter`` 的合并逻辑（普通参数 + 特殊选择器
叠加，特殊选择器仍由 AstrBotConfig 原生承载）。

覆盖 PRD §7 验收点：
  #2    KV 存储：set_many 写 KV、load 后 get 一致（test_load_merges_kv_overrides /
        test_load_fills_missing_keys_with_defaults / test_load_empty_kv_keeps_defaults /
        test_load_corrupt_json_keeps_defaults / test_load_kv_exception_keeps_defaults）
  #3    热更新：set_many 后 get 立即反映（test_hot_update_reflected_in_get）
  #4    默认值非 null（test_default_config_no_null /
        test_default_config_schedule_no_template_key /
        test_special_keys_excludes_chat_provider）
  #5    校验：非法值/类型/范围/列表/schedule 被拒（test_set_many_rejects_*）
  #6    事务性：混入非法键全部不生效（test_set_many_atomic_rollback）
  #7    特殊选择器合并（test_config_getter_merges_special_keys）
  #10   default 变更兼容：缺键补默认（test_load_fills_missing_keys_with_defaults）
  #11   回归：v0.2 的 284 个测试 + 本文件 ≥12 个全通过（CI 总通过数 ≥296）

不依赖 AstrBot 运行时。ConfigStore 多数测试直接实例化，无需 scheduler。
异步测试统一用 ``asyncio.run()`` 包装，不依赖 pytest-asyncio。
``kv_set_fn`` 是 async 的，用 ``unittest.mock.AsyncMock`` 记录调用。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from core.config_store import SPECIAL_KEYS, ConfigStore

# ======================================================================
# 辅助：构造 async kv_get_fn mock
# ======================================================================


def _make_kv_get_fn(raw):
    """返回一个 async kv_get_fn：raw 为 None/str 时返回 raw；为 Exception 实例时抛出。

    模拟 ``self.get_kv_data(key, default)`` 行为——load 内只传 key 一个位置参，
    返回值 None 表示 KV 无值，返回 str 时被 ``json.loads`` 解析。
    """

    if isinstance(raw, BaseException):
        exc = raw

        async def _raise(_key):
            raise exc

        return _raise

    async def _get(_key):
        return raw

    return _get


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
    """验收 #4/#7：SPECIAL_KEYS 仅含 chat_provider_id，且不在 DEFAULT_CONFIG。

    ConfigStore 不管理特殊选择器；chat_provider_id 由 AstrBotConfig 原生承载，
    ``_config_getter`` 合并两源时叠加。
    """
    assert SPECIAL_KEYS == frozenset({"chat_provider_id"})
    assert "chat_provider_id" not in ConfigStore.DEFAULT_CONFIG


# ======================================================================
# set_many 事务性与校验（验收 #5/#6）
# ======================================================================


def test_set_many_valid_updates_cache_and_kv():
    """验收 #2/#5：合法 patch 通过校验，缓存更新，kv_set_fn 被调用一次且 key="config"。"""

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock(return_value=None)
        ok, msg = await store.set_many(
            {"base_threshold": 0.8, "fatigue_limit": 10.0}, set_fn
        )
        assert ok is True
        assert msg == ""
        cfg = store.get()
        assert cfg["base_threshold"] == 0.8
        assert cfg["fatigue_limit"] == 10.0
        # 其他键保持默认
        assert cfg["enable"] == ConfigStore.DEFAULT_CONFIG["enable"]
        # kv_set_fn 调用一次（单键 "config" 存全量 JSON）
        set_fn.assert_awaited_once()
        args = set_fn.await_args.args
        assert args[0] == "config"
        # 第二参数是 JSON 串，反序列化后含本次更新的值
        assert json.loads(args[1])["base_threshold"] == 0.8

    asyncio.run(_run())


def test_set_many_rejects_unknown_key():
    """验收 #5：未知键被拒，msg 含"未知"，缓存与 KV 均不改。"""

    async def _run():
        store = ConfigStore()
        base_old = store.get()["base_threshold"]
        set_fn = AsyncMock()
        ok, msg = await store.set_many({"nonexistent_key": 1}, set_fn)
        assert ok is False
        assert "未知" in msg
        # 缓存不变
        assert store.get()["base_threshold"] == base_old
        # KV 未写
        set_fn.assert_not_awaited()

    asyncio.run(_run())


def test_set_many_rejects_special_key():
    """验收 #5/#7：特殊选择器键被拒，msg 含"特殊"，缓存与 KV 均不改。"""

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock()
        ok, msg = await store.set_many(
            {"chat_provider_id": "provider-xxx"}, set_fn
        )
        assert ok is False
        assert "特殊" in msg
        # ConfigStore 不管理特殊键，缓存里不应该出现该键
        assert "chat_provider_id" not in store.get()
        set_fn.assert_not_awaited()

    asyncio.run(_run())


def test_set_many_rejects_wrong_type():
    """验收 #5：类型不符被拒（float 键传 str、bool 键传 str），缓存不变。

    注：``enable`` 在 VALIDATORS 中无规则（属于无校验放行的字符串/开关键），
    故此处改用 ``dry_run``（VALIDATORS (bool, None, None)）测 bool 严格校验，
    用 ``base_threshold`` 测 float 严格校验——更能反映校验逻辑的真实边界。
    """

    async def _run():
        store = ConfigStore()
        base_old = store.get()["base_threshold"]
        dry_old = store.get()["dry_run"]
        set_fn = AsyncMock()
        # float 键传 str
        ok1, msg1 = await store.set_many({"base_threshold": "abc"}, set_fn)
        assert ok1 is False
        assert "数值" in msg1
        # bool 键传 str（bool 是 int 子类需排除）
        ok2, msg2 = await store.set_many({"dry_run": "notbool"}, set_fn)
        assert ok2 is False
        assert "布尔值" in msg2
        # 缓存不变
        assert store.get()["base_threshold"] == base_old
        assert store.get()["dry_run"] == dry_old
        set_fn.assert_not_awaited()

    asyncio.run(_run())


def test_set_many_rejects_out_of_range():
    """验收 #5：范围越界被拒（fusion_weight_rule > 1、base_threshold < 0）。"""

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock()
        ok1, msg1 = await store.set_many({"fusion_weight_rule": 5.0}, set_fn)
        assert ok1 is False
        assert "范围" in msg1
        ok2, msg2 = await store.set_many({"base_threshold": -0.5}, set_fn)
        assert ok2 is False
        assert "范围" in msg2
        set_fn.assert_not_awaited()

    asyncio.run(_run())


def test_set_many_atomic_rollback():
    """验收 #6：事务性——混入一个非法键 → 全部不写、缓存不变。

    一个合法（base_threshold=0.8）+ 一个越界（fusion_weight_rule=5.0），
    校验阶段 fusion_weight_rule 失败立即返回，缓存与 KV 都未改。
    """

    async def _run():
        store = ConfigStore()
        base_old = store.get()["base_threshold"]
        fusion_old = store.get()["fusion_weight_rule"]
        set_fn = AsyncMock()
        ok, msg = await store.set_many(
            {"base_threshold": 0.8, "fusion_weight_rule": 5.0}, set_fn
        )
        assert ok is False
        assert "范围" in msg
        # 缓存未变（base_threshold 仍是旧值，事务性回滚）
        assert store.get()["base_threshold"] == base_old
        assert store.get()["fusion_weight_rule"] == fusion_old
        set_fn.assert_not_awaited()

    asyncio.run(_run())


# ======================================================================
# list 与 schedule 校验
# ======================================================================


def test_set_many_list_type():
    """验收 #5：LIST_KEYS 要求 isinstance list；list 合法通过、str 被拒。"""

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock()
        # 合法 list
        ok1, msg1 = await store.set_many(
            {"group_whitelist": ["g1", "g2"]}, set_fn
        )
        assert ok1 is True
        assert msg1 == ""
        assert store.get()["group_whitelist"] == ["g1", "g2"]
        # str 不是 list
        ok2, msg2 = await store.set_many({"group_whitelist": "g1"}, set_fn)
        assert ok2 is False
        assert "列表" in msg2

    asyncio.run(_run())


def test_set_many_schedule_valid():
    """验收 #5：schedule 校验——合法 list[dict] 通过；非 list / 非 dict 元素被拒。"""

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock()
        # 合法
        ok1, msg1 = await store.set_many(
            {"schedule": [{"start": "08:00", "end": "10:00"}]}, set_fn
        )
        assert ok1 is True
        assert msg1 == ""
        assert store.get()["schedule"] == [
            {"start": "08:00", "end": "10:00"}
        ]
        # 非 list
        ok2, msg2 = await store.set_many({"schedule": "notlist"}, set_fn)
        assert ok2 is False
        assert "dict 列表" in msg2
        # list 但元素非 dict
        ok3, msg3 = await store.set_many({"schedule": ["notdict"]}, set_fn)
        assert ok3 is False
        assert "dict 列表" in msg3

    asyncio.run(_run())


# ======================================================================
# load 与 default 变更兼容（验收 #2/#10）
# ======================================================================


def test_load_merges_kv_overrides():
    """验收 #2：load 后 KV 覆盖项生效，其余键保持默认。"""

    async def _run():
        store = ConfigStore()
        raw = json.dumps({"base_threshold": 0.9, "fatigue_limit": 8.0})
        await store.load(_make_kv_get_fn(raw))
        cfg = store.get()
        assert cfg["base_threshold"] == 0.9
        assert cfg["fatigue_limit"] == 8.0
        # 其余键保持默认
        assert cfg["enable"] == ConfigStore.DEFAULT_CONFIG["enable"]
        assert cfg["schedule"] == ConfigStore.DEFAULT_CONFIG["schedule"]

    asyncio.run(_run())


def test_load_fills_missing_keys_with_defaults():
    """验收 #10：KV 仅含部分键，缺失键补默认（default 变更兼容）。

    模拟版本升级：旧 KV 只存了 base_threshold，新版本新增的键在 load 后
    自动补 DEFAULT_CONFIG 默认值，不出现 KeyError / None。
    """

    async def _run():
        store = ConfigStore()
        raw = json.dumps({"base_threshold": 0.9})
        await store.load(_make_kv_get_fn(raw))
        cfg = store.get()
        assert cfg["base_threshold"] == 0.9
        # 全部默认键都在缓存里且非 None
        for k, default_v in ConfigStore.DEFAULT_CONFIG.items():
            assert k in cfg, f"缺失键 {k} 未补默认"
            assert cfg[k] is not None, f"键 {k} 不应为 None"
            # 未被 KV 覆盖的键等于默认
            if k != "base_threshold":
                assert cfg[k] == default_v

    asyncio.run(_run())


def test_load_empty_kv_keeps_defaults():
    """验收 #4：KV 为空（None / 空串），保持全部默认。"""

    async def _run():
        store = ConfigStore()
        await store.load(_make_kv_get_fn(None))
        assert store.get() == ConfigStore.DEFAULT_CONFIG
        # 再测空串（``if not raw: return`` 同样处理）
        store2 = ConfigStore()
        await store2.load(_make_kv_get_fn(""))
        assert store2.get() == ConfigStore.DEFAULT_CONFIG

    asyncio.run(_run())


def test_load_corrupt_json_keeps_defaults():
    """验收 #2 边界：KV 是非法 JSON，load 不崩溃，保持默认。"""

    async def _run():
        store = ConfigStore()
        await store.load(_make_kv_get_fn("not json{"))
        assert store.get() == ConfigStore.DEFAULT_CONFIG

    asyncio.run(_run())


def test_load_kv_exception_keeps_defaults():
    """验收 #2/#8 错误处理：KV 读取抛异常，load 不崩溃，保持默认。"""

    async def _run():
        store = ConfigStore()
        await store.load(_make_kv_get_fn(RuntimeError("kv boom")))
        assert store.get() == ConfigStore.DEFAULT_CONFIG

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
    验证合并后 scheduler 能同时读到普通参数与特殊选择器 chat_provider_id。
    """
    store = ConfigStore()
    astrbot_config = {"chat_provider_id": "provider-xxx"}
    plugin = _MockPlugin(store, SPECIAL_KEYS, astrbot_config)
    cfg = plugin._config_getter()
    # 特殊键从 self.config 叠加
    assert cfg["chat_provider_id"] == "provider-xxx"
    # 全部普通参数都在（来自 ConfigStore 缓存，值是默认）
    for k, v in ConfigStore.DEFAULT_CONFIG.items():
        assert k in cfg
        assert cfg[k] == v
    # ConfigStore 自身不持有 chat_provider_id（来源是 self.config）
    assert "chat_provider_id" not in store.get()
    # 合并是浅拷贝，外部修改不应污染 ConfigStore 缓存
    cfg["base_threshold"] = 99.0
    assert store.get()["base_threshold"] == ConfigStore.DEFAULT_CONFIG["base_threshold"]


# ======================================================================
# 热更新（验收 #3）
# ======================================================================


def test_hot_update_reflected_in_get():
    """验收 #3：set_many 改缓存后 get() 立即返回新值（无需重新 load）。

    ConfigStore.get() 返回 _cache 引用，set_many 改 _cache 后下次 get 立即看到，
    scheduler 下次决策即用新值，与 v0.2 热更新语义一致。
    """

    async def _run():
        store = ConfigStore()
        set_fn = AsyncMock(return_value=None)
        assert store.get()["base_threshold"] == 0.65  # 默认
        await store.set_many({"base_threshold": 0.42}, set_fn)
        # 立即生效，无需 reload
        assert store.get()["base_threshold"] == 0.42
        # snapshot 也反映新值（snapshot 返回浅拷贝，供 Web API GET）
        assert store.snapshot()["base_threshold"] == 0.42

    asyncio.run(_run())
