# Changelog

## [0.3.1] - 2026-07-24

### Changed
- Core 目录按职责分类到 6 个子包：`common/`（models/prompts 共享数据结构）+ `decision/`（adaptive/engine/fatigue/fusion/inertia/interest/reply_keyword/rule_engine 决策引擎 8 文件）+ `storage/`（config_store/metrics/migration/ratelimit/tune_controller 存储与限流 5 文件）+ `tracking/`（buffer/context/tracker 上下文与跟踪 3 文件）+ `scheduler/`（scheduler/batch_pipeline/bot_events/autotune_collector/replay 调度器与 mixin 5 文件）+ `plugin/`（autotune/callbacks/commands/formatting/web/web_bridge 插件 mixin 6 文件）
- 全部 import 路径更新为子包完整路径：同子包内 `from .module` 单点相对；跨子包 `from ..subpackage.module` 双点相对
- `core/scheduler/__init__.py` re-export `SocialScheduler`，保持 `from core.scheduler import SocialScheduler` 既有调用方式不变
- 修复 `core/scheduler/batch_pipeline.py` 4 处 except 兜底分支的相对 import 遗漏（`from .models` → `from ..common.models`、`from .prompts` → `from ..common.prompts`）
- 修复 `tests/test_ratelimit.py` 2 处 monkeypatch 字符串路径（`core.ratelimit.` → `core.storage.ratelimit.`）

### Notes
- 纯内部目录重组，不改变任何对外行为、配置、数据格式
- 477 既有测试零回归
- v0.3.0 的 9 个 mixin 文件（callbacks/autotune/web_bridge/commands/formatting/migration/batch_pipeline/bot_events/autotune_collector）原位于 `core/` 根，本次归入对应职责子包

## [0.3.0] - 2026-07-24

### Changed
- main.py 采用 mixin 模式重构：1756 行 → 305 行，8 类职责拆分到 6 个新文件（core/{callbacks,autotune,web_bridge,commands,formatting,migration}.py），ProSocialPlugin 多继承 CommandsMixin/WebBridgeMixin/TuneMixin/CallbacksMixin
- core/scheduler.py 采用 mixin 模式重构：1786 行 → 704 行，6+ 类职责拆分到 3 个新文件（core/{batch_pipeline,bot_events,autotune_collector}.py），SocialScheduler 多继承 BatchPipelineMixin/BotEventsMixin/AutotuneStatsMixin
- /prosocial 指令组注册搬回 main.py（处理逻辑仍在 CommandsMixin._handle_*），避免框架将指令识别为 core.commands 模块
- initialize() 增加孤儿 handler 自清理逻辑，规避框架 _unbind_plugin 清理盲区（热重载时清理遗留 handler）

### Notes
- 纯内部重构，不改变任何对外行为、配置、数据格式
- 477 既有测试零回归
- 每个文件 ≤ 800 行，单一职责聚焦（batch_pipeline.py 844 行为例外，run_batch 单方法 576 行无法再拆）

## [0.2.9] - 2026-07-24

### Added
- **LLM 调参速率限制器**（F4，`core/tune_controller.py` 新建 `TuneRateLimiter`）：纯标准库 deque 实现，对所有 `llm_autotune` 调用（手动 + 自动）施加冷却（`autotune_cooldown_hours`，默认 3 小时）与日上限（`autotune_max_per_day`，默认 4 次）双重限制。冷却未到 → `reason="cooldown"`；已达日上限 → `reason="daily_cap"`；`cooldown=0` 或 `max_per_day=0` 表示不限该维度。ADMIN 可经 `force=True` 跳过限制但仍 `record()` 计入配额。状态持久化到 SQLite KV 键 `tune_rate_state`（v0.2.7 ConfigStore.get_kv/set_kv），插件重载后冷却与日上限计数连续。
- **触发率越界自动调参**（F3，scheduler `_maybe_autotune`）：复用 `AdaptiveThreshold` 评估周期（每 EVAL_EVERY=20 样本一次），刚评估后读取窗口触发率（`adaptive.window_rate()`），当样本数 ≥ `autotune_min_decisions`（默认 30）且触发率 > `autotune_safe_rate_hi`（默认 0.30）或 < `autotune_safe_rate_lo`（默认 0.05）时，后台 `asyncio.create_task` 调用注入的 `autotune_trigger_fn`（main.py 提供，包装 `llm_autotune("analyze")`）。`autotune_auto_apply=true` 则成功后自动 apply。自动触发同样受 F4 速率限制约束。
- **LLM 全视野调参**（F1，`_build_tune_prompt` 重写）：prompt 注入全量配置快照（`ConfigStore.snapshot()` 全部 ~75 项普通键减 DENYLIST + `chat_provider_id`/`embedding_provider_id` 解析为 provider 名称）+ 兴趣数据（`interest_mgr.export_view()`：items/hate_keywords/high_interest_keywords/rejected）+ 人设文本 `persona_text`/`persona_knowledge`（已在配置内，显式高亮）+ 作息 schedule + 群白名单（group_mode/group_whitelist）+ `AdaptiveThreshold` 的 mult 与窗口触发率。LLM 输出格式扩展为三段：`suggested_patch`（标量配置）/ `suggested_keywords_patch`（兴趣关键词增删建议）/ `persona_revision`（可选人设改写建议）。
- **LLM 扩展可写键**（F2，denylist 模式）：用 `TUNE_DENYLIST = frozenset({"enable","dry_run","group_whitelist","group_mode","chat_provider_id","embedding_provider_id"})`（6 项操作/安全敏感键）替换 v0.2.8 的 18 项 `TUNE_WHITELIST`。可写键 = `set(DEFAULT_CONFIG) - TUNE_DENYLIST`（约 70 项，含 `persona_text`/`persona_knowledge`/`schedule`/各权重/疲劳/惯性/瞥眼/规则通道/回复关键词等）。LLM 输出含 DENYLIST 键时被丢弃并在 analysis 末尾注明 `[已过滤安全敏感键: ...]`。
- **llm_autotune apply 分流**（F2）：`apply` 路径分流为四类——①标量配置键 → `ConfigStore.set_many`（沿用既有类型/范围校验器）；②patch 含 `persona_text`/`persona_knowledge`/`interest_example_count`/`interest_keyword_count` → 触发后台 `interest_mgr.regenerate()`（asyncio.create_task 不阻塞响应）；③`suggested_keywords_patch` → 经 `interest_mgr.add_item`/`remove_item`（复用 v0.2.6 F2 CRUD）+ `apply_rejected`（v0.2.2）重算质心；④`persona_revision` → 合并入 `persona_text` 走同路径。
- **AdaptiveThreshold 扩展**（F3，`core/adaptive.py`）：`record(score, triggered)` 返回值由 `None` 改为 `bool`（True 表示本次调用触发了评估，即 `_since_eval` 归零）；新增 `window_rate() -> float`（返回当前窗口触发率，无样本返回 0.0）与 `window_size() -> int`。常量 HI_RATE/LO_RATE 保持 0.30/0.05 不变（与 v0.2.9 新增的 `autotune_safe_rate_*` 独立，前者管本地 mult，后者管 LLM 自动触发）。
- **collect_tune_stats 扩展**（F1/F3，scheduler）：`config` 字段从子集改为全量配置快照（减 DENYLIST 由 main 过滤）；新增 `adaptive_summary` 字段（每群一项：`group_id`/`mult`/`window_rate`/`samples`）。
- 7 项新配置项（全部非 null 默认）：`autotune_safe_rate_hi`(0.30) / `autotune_safe_rate_lo`(0.05) / `autotune_auto_trigger_enabled`(true) / `autotune_auto_apply`(false) / `autotune_min_decisions`(30) / `autotune_cooldown_hours`(3.0) / `autotune_max_per_day`(4)
- 35 项 v0.2.9 单元测试（TuneRateLimiter 8 / AdaptiveThreshold 扩展 4 / TUNE_DENYLIST 3 / apply 分流 6 / 速率限制 5 / scheduler 自动触发集成 4 / prompt 全视野 3 / collect_tune_stats 扩展 2）；web post_autotune 10 项测试由 agent-f 在 test_web.py 实现

### Changed
- `main.py` `TUNE_WHITELIST`（18 项 frozenset）→ `TUNE_DENYLIST`（6 项 frozenset）；`_writable_keys` 类方法动态计算可写键集
- `main.py` `llm_autotune` 签名扩展为 `llm_autotune(action, patch=None, *, style="", guidance="", force=False, keywords_patch=None, persona_revision=None) -> dict`；入口先经 `TuneRateLimiter.allow()`（`force=True` 跳过），被限返回 `{ok: False, error: "rate_limited", reason, retry_after/used/limit}`；成功后 `record()`
- `main.py` 新增 `_tune_limiter` 单例（TuneRateLimiter），`initialize` 从 SQLite KV `tune_rate_state` 恢复状态，`terminate` 持久化
- `main.py` `cmd_tune` 扩展：`/prosocial tune [style]`（受速率限制）/ `/prosocial tune apply` / `/prosocial tune force [style]`（ADMIN 跳过限制）/ `/prosocial tune status`（显示速率限制状态 + 上次建议摘要 + 自动触发开关）
- `core/scheduler.py` `__init__` 新增 `autotune_trigger_fn: Callable[[], Awaitable[dict]] | None` 注入参数（默认 None，既有行为不变）；`run_batch` 在 `adaptive.record(...)` 返回 True 且 `autotune_auto_trigger_enabled=true` 时调 `_maybe_autotune`
- `core/web.py` `WebBridge.run_autotune` 透传 `force`/`keywords_patch`/`persona_revision`；`post_autotune` handler 三字段类型前置校验；响应含 `rate_limit` 状态块
- Dashboard autotune 面板增强：新增「自动触发」状态指示灯（绿/红圆点）+「⚡ 强制分析」按钮 +「自动应用建议」复选框；analysis 展示区新增「LLM 视野说明」折叠块；suggested_patch 表分三段（标量配置/关键词变更/人设改写）；新增「LLM 调参」配置分组（7 项控件）
- Web API handler 总数保持 12（复用既有 autotune handler，仅扩展字段）
- 插件版本 v0.2.8 → v0.2.9

### Notes
- LLM 调参建议经 DENYLIST 过滤 + ConfigStore 校验器 + interest_mgr 去重三重保护，DENYLIST 键被丢弃时 analysis 末尾注明
- 自动触发同样受速率限制约束：触发条件满足但 `TuneRateLimiter.allow()` 返回 False 时不触发，写日志事件 `autotune_skipped: rate_limited`
- `_install_astrbot_mocks()` 测试辅助：main.py 强依赖 astrbot 运行时无法离线 import，测试通过 sys.modules 注入最小化 mock（AstrBotConfig=dict / logger=MagicMock / filter=_FakeFilter / Star=基类 / register=透传装饰器等）+ 包内子模块加载方式，使 main.py 可被 import 测试

## [0.2.8] - 2026-07-23

### Fixed
- **主动回复在追踪页无内容**（F1）：主动回复改为注入 AstrBot 标准消息管线（`platform_inst.handle_msg(abm)`），合成 `AstrBotMessage` 携带 `prosocial:` 前缀 message_id 与虚拟发送者，触发 waking_check → LLM stage → trace 自动记录 → 对话历史自动保存。`on_llm_request` 钩子检测前缀并通过 `extra_user_content_parts` + `mark_as_temp()` 注入接话风格提示（不污染历史）。注入失败/未配置时降级回直连 LLM 旧路径。彻底解决 Dashboard 追踪页看不到主动回复内容的问题。
- **兴趣关键词/句子个数配置无效**（F4）：`_compute_persona_hash` 把 `example_count` / `keyword_count` 纳入哈希输入（4 段 payload），数量变更使内存缓存与 interests.npz 磁盘缓存同时失效；`set_config_view` 在 patch 含 `interest_example_count` / `interest_keyword_count` 时触发后台重建；`/prosocial persona reload` 从 cfg 读数量传入 regenerate。修改数量后重启不再命中旧缓存。

### Added
- **自适应阈值控制器**（F2a，`core/adaptive.py` `AdaptiveThreshold`）：每群一个实例，滚动窗口记录最近 100 次批次决策，每 20 样本评估触发率——rate > 30% 则 `mult *= 1.1`（收紧），rate < 5% 则 `mult *= 0.9`（放宽），mult 钳制 [0.5, 2.0]。无论 embedding 模型余弦尺度如何，控制器把实际触发率收敛到 5%-30% 自然区间，消除调参敏感。状态经 KV 持久化（`adaptive_state`），`BatchDecision` 新增 `adaptive_mult` 字段。
- **每群发送频率硬上限**（F2b，`core/adaptive.py` `SendQuota`）：滑动窗口记录发送时间戳，`max_proactive_per_hour`（默认 5）/ `max_proactive_per_day`（默认 20）超限后 `suppressed_reason="quota"`，省 LLM 开销。调参失误的最终兜底，任何参数调崩都不会导致话痨。
- **LLM 自动诊断调参**（F3）：`/prosocial tune` 指令（ADMIN）+ `POST /prosocial/autotune` API + Dashboard「🤖 LLM 诊断调参」按钮，分析最近 200 条决策数据（触发率/score 分布/五因子均值/疲劳/suppressed 直方图）+ 当前参数子集 → LLM 生成 {analysis, suggested_patch, expected_effect} → 18 键白名单 + ConfigStore 校验器双重过滤 → apply 应用建议。前端弹窗显示分析结果与建议参数表，二次确认后应用。
- **collect_tune_stats()**：scheduler 新方法，汇总最近 200 条决策的统计信息供 LLM 诊断。
- 4 项新配置项（全部非 null 默认）：`reply_via_pipeline` / `adaptive_threshold_enabled` / `max_proactive_per_hour` / `max_proactive_per_day`
- 32 项 v0.2.8 单元测试（AdaptiveThreshold 10 / SendQuota 5 / models+metrics 2 / config_store 4 / interest hash 3 / scheduler 集成 8）

### Changed
- scheduler 三个主动发送路径（`run_batch` / `_send_wait_window_reply` / `glance_once`）统一改走新方法 `_dispatch_proactive`，按 `reply_via_pipeline` + `inject_fn` 是否就绪选择注入路径或旧路径降级
- `run_batch` 融合判定改用 `eff_threshold = fusion.threshold * adaptive.multiplier()`（自适应开关关闭时 multiplier=1.0 等价旧行为）
- `after_message_sent` 钩子按触发消息 message_id 前缀分类：`prosocial:` → `reply_type="active"` + `is_proactive=True`；否则保持 `passive`
- `on_group_message` 检测 `prosocial:` 前缀 message_id 直接 return（避免合成消息双缓冲/双决策），并在跳过自身消息前缓存 `event.get_platform_id()` → `event.get_self_id()` 供 inject_fn 构造合成消息
- Dashboard 概览面板新增「🤖 LLM 诊断调参」按钮与通用 modal 弹窗；配置面板基础设置组新增 4 项控件；版本号 v0.2.6 → v0.2.8
- Web API handler 总数 11 → 12（新增 `POST /prosocial/autotune`）
- 插件版本 v0.2.7 → v0.2.8

### Notes
- 注入仅支持群聊（umo 类型 GroupMessage）；私聊/平台未找到/self_id 未缓存 → 旧路径降级
- 合成消息 sender 为虚拟用户「群聊动态」，非真实发言者；对话历史中显示为此昵称
- LLM 调参建议经白名单 + 校验器双重过滤，不会写入非法/危险键
- 兴趣 interests.npz 因 persona_hash 算法变更，首次启动 v0.2.8 时自动重建（一次性）

## [0.2.7] - 2026-07-23

### Fixed
- **配置持久化彻底修复**（F1）：ConfigStore 从 AstrBot KV 存储迁移到独立 SQLite 数据库（config.db），彻底解决插件重载后配置丢失的问题。KV 存储在插件重载时可能被清空或不可用，改用 aiosqlite 直接管理数据库文件后，配置持久化完全由插件自身掌控。
- **全部 KV 数据迁移到 SQLite**（F2）：metrics / decision_log / fatigue / group_enable / interest_rejected 从 AstrBot KV 迁移到 SQLite，ConfigStore 新增 `get_kv`/`set_kv`/`delete_kv` 通用 KV 方法。修复 `get_kv_data("interest_rejected")` 缺少 `default` 参数的报错，以及重载后 KV 不可用导致不采集消息、不触发主动发言的严重 bug。
- **旧数据自动迁移**（F3）：首次启动 v0.2.7 时自动从旧 AstrBot KV 读取全部数据（config / group_enable / decision_log / metrics / fatigue / interest_rejected）迁移到 SQLite，避免配置回到默认值（whitelist + 空白名单）导致群未启用。迁移完成后标记 `_kv_migrated` 不再重复。
- **配置向导保存卡顿**（F4）：set_config_view 中人设变更触发的 `interest_mgr.regenerate()` 从 `await` 改为 `asyncio.create_task()` 后台执行，API 立即返回，向导保存秒回。
- **_conf_schema.json 排版问题**（F5）：`description` 从长解释改为简短标签（如「聊天模型」），解释性内容移到 `hint` 字段，修复 AstrBot 设置面板中描述被渲染为标题的问题。
- **Embedding 选择器只能选 LLM**（F6）：`embedding_provider_id` 移除 `_special: "select_provider"`（只选 LLM Provider 不选 Embedding Provider），改为普通 string 输入框，用户手动填 ID，留空则自动选第一个可用 Embedding Provider。

### Changed
- ConfigStore 构造函数从 `ConfigStore()` 改为 `ConfigStore(db_path: Path)`，需传入 SQLite 数据库文件路径
- `ConfigStore.load()` 不再需要 `kv_get_fn` 回调参数，直接读 SQLite
- `ConfigStore.set_many(updates)` 不再需要 `kv_set_fn` 回调参数，直接写 SQLite
- 新增 `ConfigStore.close()` 方法，在插件 `terminate()` 中关闭数据库连接
- `main.py` 的 `_kv_get`/`_kv_set` 改为包 `ConfigStore.get_kv`/`set_kv`，彻底脱离 AstrBot KV
- `main.py` 的 `on_loaded` 和 `set_interests_view` 中的 KV 调用改用 `ConfigStore.get_kv`/`set_kv`
- `main.py` 的 `terminate()` 增加 `config_store.close()` 调用

### Added
- `ConfigStore.get_kv(key, default)` / `set_kv(key, value)` / `delete_kv(key)` 通用 KV 方法
- `main.py` 新增 `_migrate_kv_to_sqlite()` 数据迁移方法
- `aiosqlite` 加入 `requirements.txt`
- 新增 `test_f1_config_survives_reload` 测试：模拟插件重载后配置不丢失

## [0.2.6] - 2026-07-23

### Fixed
- **配置持久化**（F1）：ConfigStore.load() 移至 initialize() 中调用（该方案未能根治重载丢配置问题，v0.2.7 改用 SQLite 彻底解决）
- **Embedding 提供商设置**（F3）：embedding_provider_id 从 ConfigStore 迁移至 _conf_schema.json（AstrBot 原生 select_provider），解决保存后置空且无法修改的问题
- **人设描述无法获取兴趣关键词**（F4）：set_config_view 检测 persona_text/persona_knowledge 变更时自动触发 interest_mgr.regenerate
- **空批次摘要仍然求解**（F5）：scheduler.run_batch 对空 batch_text 提前返回，跳过后续嵌入和评分计算
- **切换配置子标签页丢失更改**（F7）：switchCfgTab 前自动调用 flushAutoSave，保存脏字段
- **切换子标签页页面卡死**（F10）：switchCfgTab 仅切换 CSS class，不再重建 DOM（77+ 字段一次性重建导致卡顿）
- **Embedding 欠费仍计算 Score B**（F12）：BatchDecision 新增 embedding_degraded 字段，嵌入失败时标记降级

### Changed
- **参数触发率优化**（F6）：base_threshold 0.65→0.55、w_int 1.0→1.2、w_silence 0.2→0.35、after_reply_probability 0.6→0.7，降低触发门槛提高活跃度
- **窗口上下文注入策略**（F8）：移除 on_llm_request 钩子的长窗口注入，改为仅主动触发回复时才注入（long_window_inject_proactive=True）
- **配置描述完善**（F13）：CONFIG_GROUPS 每组添加 doc 字段和工作原理说明，优化 label/hint 描述

### Added
- **兴趣关键词增删改查**（F2）：InterestManager 新增 add_item/update_item/remove_item 方法，Dashboard 兴趣面板支持添加/编辑/删除操作
- **可配置的示例句子和关键词数量**（F9）：新增 interest_example_count（1-10，默认 3）和 interest_keyword_count（3-30，默认 12）配置项
- **导出完整决策记录 JSON**（F11）：新增 GET /prosocial/export API，导出配置+决策记录+疲劳+兴趣数据，可用于 AI 辅助调参
- **Dashboard 决策表降级标记**（F12）：embedding_degraded 行在通道列显示灰色「降级」标签
- 新增 long_window_inject_proactive 配置项（bool，默认 True）

## [0.2.5] - 2026-07-23

### Added
- **基于回复分词的连续对话匹配**：Bot 每次群聊回复后用 jieba TF-IDF 提取关键词缓存（按目标用户 + TTL），目标用户下次发消息时计算匹配得分，叠加到双通道融合后的 `final_score`，弥补向量通道在超短回复（1-3 字）上的语义缺失
- **个人跟踪模块增强**：向量相似度不足时，转用关键词匹配作为强信号直接触发回复（超过 `reply_keyword_min_score_to_trigger` 阈值），并使用 `track` 档位疲劳消耗（0.6，低于 active 1.2）
- **关键词缓存生命周期**：覆盖式更新（新回复立即失效旧缓存）/ TTL 过期（默认 60s）/ 回复后清除（防重复触发）/ 连续低分清除（默认 2 次 <0.1 清除）
- **6 项配置项**（`reply_keyword_enabled` / `reply_keyword_top_n` / `reply_keyword_boost_factor` / `reply_keyword_ttl_seconds` / `reply_keyword_min_score_to_trigger` / `reply_keyword_early_clear_low_score`），Dashboard 配置面板新增「回复关键词匹配」分组
- **`BatchDecision` 新增 2 字段**：`keyword_match_score` / `keyword_added_score`（向后兼容默认 0.0）
- **jieba 加入 requirements.txt**（运行时仍 try-import 兜底，缺失时禁用功能并日志警告一次）
- 22 项 v0.2.5 单元测试（11 项 ReplyKeywordManager + 11 项 scheduler 集成 3 集成点 + 生命周期 + jieba 缺失 + 降级路径）

### Changed
- `scheduler.on_bot_sent`：增加关键词提取（防重时跳过；jieba 不可用仅警告一次）
- `scheduler.run_batch`：融合后做关键词加分（集成点 1）+ 个人跟踪增强（集成点 2）+ track 档位疲劳消耗（集成点 3）+ 回复后清除缓存 + 连续低分清除 + dry_run 日志
- 插件版本 v0.2.1 → v0.2.5

### Notes
- **私聊支持留作 v0.2.6**：当前架构完全围绕群聊设计（仅 `on_group_message` handler，scheduler 以 `group_id` 为核心），私聊无 `run_batch` 决策管线。本版本聚焦群聊实现（覆盖 PRD 90% 价值）。

## [0.2.2] - 2026-07-23

### Added
- **Embedding 模型选择器**（F18）：新增 `GET prosocial/providers` API，Dashboard 的 embedding_provider_id 改为动态下拉（拉取已配置 embedding provider）
- **兴趣关键词展示与人工过滤**（F20）：新增 `GET/POST prosocial/interests` API，Dashboard 新增「兴趣关键词」tab，4 级展示 LLM 生成的 topic/examples/keywords，支持删除（reject 持久化 KV）与应用过滤（重算质心）
- **Dashboard 顶层标签页重组**（F19）：单页网格改为 5 顶层 tab（概览/决策记录/群管理/兴趣关键词/配置），记忆选择，按 tab 轮询
- **防抖自动保存**（F21）：配置改动后 3 秒自动提交 dirty 字段（delta），切 tab/关页立即保存
- 32 项 v0.2.2 单元测试（InterestManager export/reject/apply/regenerate + 3 新 Web API）

### Fixed
- **大提交健壮性**（F22）：collectDirty 逐字段 try/catch，合法字段单独保存，非法字段红框不阻塞 + inline 校验（根因：int/float 空值→NaN→throw 连锁阻塞整批）
- **向导误关**（F23）：遮罩点击不再关闭向导，仅退出按钮/Esc 可关，有未保存改动需确认

## [0.2.1] - 2026-07-23

### Changed
- **配置存储迁移**：全部 ~48 项普通配置从 `_conf_schema.json`/AstrBotConfig 迁移到 AstrBot KV 存储 + Web Dashboard（ConfigStore 模块，内存缓存 + 热更新）
- `_conf_schema.json` 瘦身：仅保留 `chat_provider_id`（select_provider 原生下拉），主插件面板不再堆叠普通参数
- `config_getter` 合并两源：ConfigStore 缓存（普通参数）+ AstrBotConfig（特殊选择器），对 scheduler 透明
- 新增 `on_astrbot_loaded` 钩子：启动时从 KV 加载配置覆盖项

### Added
- **ConfigStore 模块**（`core/config_store.py`）：DEFAULT_CONFIG（71 项默认值）+ VALIDATORS + 事务性 set_many + KV 持久化
- **Dashboard 配置面板补全**：全部 71 项普通参数分 9 组 tab，含 schedule 时段增删编辑器、list 控件
- **配置引导向导**（F17）：7 步分步流程（人设→群范围→阈值→作息→双通道→疲劳惯性→完成），首启自动弹出，带说明与进度条
- 18 项 v0.2.1 单元测试（ConfigStore 校验/事务性/load 合并/热更新/合并逻辑）

### Fixed
- Dashboard 配置保存后不再用 POST 响应回填表单（响应改为 `{updated:N}`）

## [0.2.0] - 2026-07-23

### Added
- **双通道唤醒决策**：向量语义通道（score_b）+ 规则模式通道（score_a），融合判定 `final = α·score_a + (1−α)·score_b`，支持通道独立开关与动态权重
- **规则引擎（通道 A）**：隐式回复评分（10 项正则组）+ 疑问信号（6 档强模式）+ 屏蔽短语抑制
- **全局疲劳管理**：bot 级单例，指数衰减，按回复类型消耗（active/passive/track/glance），高疲劳阈值惩罚 + 非强制抑制
- **对话惯性**：回复后阈值倍率（after_reply + proactive 双窗口）+ 主动话题临时提升 + 成功/失败计数
- **等待窗口**：回复后收集同用户连续后续消息，合并为一条连贯回复（超时/收满/@ 强制关闭）
- **27 项配置扩展**：双通道 / 规则 / 疲劳 / 惯性 全部可配置（非 null 默认）
- **`/prosocial fatigue` 指令**：查看全局疲劳值 / 级别 / 阈值倍率 / 抑制状态
- Dashboard `get_status` 新增全局疲劳快照 + 每群惯性快照字段
- **Dashboard 前端重设计**：现代控制台风格卡片网格，新增疲劳仪表、每群惯性面板、决策表 score_a/score_b/α/通道列与迷你条、趋势图三线叠加、27 项配置分 9 组 tab

### Changed
- `run_batch` 决策管线接入双通道融合判定（BatchDecision 新增 score_a/score_b/alpha/fatigue_level/fatigue_value/channel 6 字段，向后兼容 v0.1 数据）
- `on_bot_sent` 增参 `reply_type` / `is_proactive`，内部消耗疲劳 + 触发惯性，防重（同 text <2s 跳过）
- 修复 Dashboard 前端字段名不匹配 BUG（active_hours/current_group/replay → in_active_hours/current_monitoring/replay_active）
- 插件版本 v0.1.0 → v0.2.0

## [0.1.0] - 2026-07-23

### Added
- 人设兴趣管理（LLM 生成 + 内置 EmbeddingProvider 向量化 + npz 持久化）
- 动态批处理与五因子唤醒决策引擎（兴趣/话题/回应期待/冷却/沉默 + 动态阈值 + 反感屏蔽 + 规则降级）
- 双窗口上下文（短窗口常注；长窗口被动 @ 时相关性 Top-N 注入）
- 个人跟踪回复（无 @ 接话，低阈值，超时清理）
- 多群瞥一眼注意力转移（期待回复期随机选群、关键词+向量快判、快速插话）
- 多群轮询与作息调度（时段±抖动、单群专注、监听周期、群冷却、五态状态机）
- 群白名单（whitelist/all 模式）+ 群快捷开关（AND 语义，实时生效）
- DRY_RUN 干运行 + 决策日志环 + 每日指标
- Dashboard 前端页面（状态/决策记录/得分趋势/实时配置/群管理，7 个 Web API）
- 历史回放（JSONL 按时间流速喂入决策管线，强制不发送）
- `/prosocial` 管理员指令组（status/dryrun/enable/disable/persona/scores/replay）
