# Changelog

## [0.3.10] - 2026-07-24

### Added
- **LLM 调参 Prompt 重构（两段式 + CoT + 范围表）**（T3，`core/plugin/autotune.py` `_build_tune_prompt`）：从原「单段 suggested_patch」重写为 18 段结构——
  - **两段式输出**：`{diagnosis: "...", plan: [{key, original, suggested, delta, reason, expected_effect_quant}, ...], suggested_keywords_patch, persona_revision, expected_effect_overall}`，LLM 先诊断后调参
  - **CoT 五步引导**（第 2 段「# 思考流程」）：强制 LLM 按观察数据→诊断问题→假设原因→设计调整方案→量化预期影响五步展开
  - **全量参数范围表**（第 3 段「# 参数范围参考」）：遍历 `ConfigStore.VALIDATORS` 生成 markdown 表格（约 53 项有规则 + 17 项无规则标注「无范围限制」），避免 LLM 调出 VALIDATORS 范围被静默拒绝
  - **调参约束段**（第 16 段）：显式告知 LLM 幅度上限公式 / 数量上限 / 必填 reason（引用具体数值）/ 必填 expected_effect_quant（必须含数字）/ 范围限制
  - **两轮模式支持**：`_build_tune_prompt` 新增 `phase` 参数（`'full'` 默认 / `'diagnosis'` 移除 plan 输出要求 / `'plan'` 注入已生成 diagnosis 后输出 plan）
- **调参限制逻辑**（T4，`core/plugin/autotune.py` `_validate_plan`）：单次变化幅度上限 `|suggested - original| <= autotune_max_change_ratio * |original|`（默认 0.3 即 ±30%，`|original| < 0.1` 时按 VALIDATORS 范围 1/4）；plan 中参数数 ≤ `autotune_max_params_per_tune`（默认 5）；每项必须含 reason + expected_effect_quant；suggested 值必须在 VALIDATORS 范围内。超限项被剔除并在 diagnosis 末尾注明
- **TuneHistoryStore 扩表 + 新方法**（T2，`core/storage/tune_history.py`）：表新增 8 字段 `original_values` / `pre_apply_values` / `applied_values` / `diagnosis` / `plan` / `status` / `approved_by` / `error_msg`；新增方法 `list_pending()` / `mark_approved(record_id, applied_values, approved_by)` / `mark_rejected(record_id, approved_by)` / `restore_to_pending(record_id)` / `mark_failed(record_id, error_msg)` / `dedupe_pending_params()`（参数级去重，按时间保留最新）
- **批准工作流 API**（T7，`core/plugin/web.py` + `web_bridge.py`）：新增 `POST /prosocial/tune/approve` / `POST /prosocial/tune/reject` / `POST /prosocial/tune/restore` / `POST /prosocial/tune/plan`（两轮模式第二轮）4 个 handler；Web API handler 总数 15 → 19
- **前端历史页扩展**（T8，`pages/prosocial/index.html`）：调参历史每条新增状态徽章（pending/approved/rejected/failed）+ 来源徽章（manual/auto/force）+ diagnosis + plan 分两段展示 + 原始值/当前值/变化量三列表格；新增 status 过滤下拉 + include_archived 开关 + hide_days 输入
- **前端概览页精简卡片**（T9，`pages/prosocial/index.html`）：概览页内嵌待批准建议精简卡片（每条显示来源徽章 + diagnosis 摘要 + plan 参数数 + 批准/拒绝按钮），点击展开跳转历史页详情
- **4 项新配置**（T1，`core/storage/config_store.py`）：`autotune_max_change_ratio`(0.3) / `autotune_max_params_per_tune`(5) / `autotune_two_phase_enabled`(false) / `autotune_history_hide_days`(30)
- **source 字段扩展为三值**：`manual`（手动 analyze）/ `auto`（自动触发）/ `force`（强制触发，原 `manual+force` 合并）

### Changed
- **analyze 流程重构**（T5，`core/plugin/autotune.py` `llm_autotune`）：analyze 后立即调 `dedupe_pending_params()` 参数级去重；analyze 成功后 record 含 `diagnosis`/`plan`/`original_values`/`status="pending"`；`autotune_two_phase_enabled=true` 时分两轮调用 LLM（diagnosis + plan），只计一次速率限制
- **apply 流程重构**（T6，`core/plugin/autotune.py`）：apply 改为基于 `record_id` 操作；apply 时先快照 `pre_apply_values`，set_many 成功后快照 `applied_values`，调 `mark_approved(record_id, applied_values, approved_by="auto"/"manual")`；apply 失败调 `mark_failed(record_id, error_msg)`；`autotune_auto_apply=false` 时 analyze 仅入 pending 队列，apply 必须从历史页/概览页手动触发
- **历史记录默认隐藏 30 天前 non-pending 记录**：`get_tune_history_view` 新增 `hide_days` 参数（默认读 `autotune_history_hide_days` 配置），pending 记录不隐藏
- `core/plugin/web.py` `get_tune_history` handler 新增 query 参数 `status` / `include_archived` / `hide_days`
- 插件版本 v0.3.8 → v0.3.10

### Fixed
- **@ 昵称清洗残留平台 ID 污染 embedding**（`core/scheduler/scheduler.py` `on_message`）：旧正则 `@[\w\u4e00-\u9fa5]{1,20}` 只剥离 `@昵称`，AstrBot At 渲染格式为 `@昵称(平台ID)空格`（如 `@狐白(3693831132) `），残留 `(3693831132)` 数字串仍污染 embedding 导致 s_int 计算失准。修复：改为 `@[^\s()]*\(\d+\)\s*` 完整剥离整个 At token + 兜底 `(?<!\w)@\S{1,30}` 剥离非标准 @ 昵称（不误伤 `user@domain` 邮箱）
- **Unicode Cf 类不可见格式字符扰乱 embedding 分词**（`core/scheduler/scheduler.py` `on_message`）：新增正则剔除零宽字符（U+200B/C/D ZWSP/ZWNJ/ZWJ）、BOM（U+FEFF）、WJ（U+2060）、方向控制符（U+200E/F LRM/RLM、U+202A-E LRE/RLE/PDF/LRO/RLO、U+2066-9 隔离符），这些字符会让 `len()` 统计失真并扰乱正则字符匹配位置。清洗顺序调整为：特殊字符 → emoji → @ 昵称（避免方向控制符让 `@` 不在词首导致漏匹配）

### Notes
- 542 既有测试零回归（v0.3.10 新增测试待 T11 补充）
- LLM 调参建议经范围表提示 + VALIDATORS 校验 + 幅度上限 + 数量上限 + 必填理由五重约束，杜绝超范围/激进调参
- pending 建议参数级去重：多条 pending 记录存在相同参数键时只保留时间最新的那条的建议值
- 两轮模式默认关闭（`autotune_two_phase_enabled=false`），开启后分两次 LLM 调用（先 diagnosis 后 plan），只计一次速率限制配额
- @ 昵称清洗仅作用于入缓冲区文本（影响 embedding 与长度统计），上下文窗口保留原始文本（用于 `short_window_text` 展示）
- 特殊字符剔除无配置开关，强制剔除（这些字符纯粹是格式噪声，无语义价值）

## [0.3.8] - 2026-07-24

### Fixed
- **已过滤项恢复按钮点不动**（`pages/prosocial/index.html`）：根因是 `restoreInterest()` 和 `clearTuneHistory()` 使用了 `window.confirm()`，而 AstrBot 插件页面在 iframe/sandbox 环境中加载时 `confirm()` 被静默阻止（返回 false），导致函数在 `if (!confirm(...)) return` 处提前退出，按钮点击无任何反应。修复：新增 `confirmModal(title, msg, onConfirm)` 自定义模态框函数（复用 `showModal`/`closeModal` 的 DOM 结构，带确认/取消按钮 + Esc 关闭 + 事件清理），替换两处 `confirm()` 调用。与 `rejectInterest`（无需 confirm，正常工作）行为对齐。

### Changed
- 新增 `confirmModal(title, msg, onConfirm)` 函数：基于 `showModal` 的自定义确认弹窗，支持确认/取消按钮 + Esc 关闭 + 事件清理
- `restoreInterest` 中 `confirm()` → `confirmModal()`（回调式，非阻塞）
- `clearTuneHistory` 中 `confirm()` → `confirmModal()`（回调式，非阻塞）
- 插件版本 v0.3.7 → v0.3.8

### Notes
- 542 既有测试零回归（纯前端修复，无 Python 代码变更）
- `rejectInterest` 不受影响（原本就没有 `confirm()` 调用，用户反馈"删除关键词正常"印证了这一点）
- `confirmModal` 复用既有 `showModal`/`closeModal` 机制，无需新增 DOM 结构

## [0.3.7] - 2026-07-24

### Added
- **主动消息最小间隔冷却**（`core/scheduler/batch_pipeline.py` + `core/storage/config_store.py`）：新增 `proactive_min_interval` 配置项（默认 180 秒），距上次主动消息不足此秒数则抑制触发（`suppressed_reason="min_interval"`），防止短时间内反复触发感兴趣话题导致话痨。群状态新增 `last_proactive_ts` 字段，在 `_dispatch_proactive` 发送成功后更新。配置为 0 表示禁用此冷却。测试环境默认禁用（conftest `proactive_min_interval=0`，与 `batch_min_text_length=0` 同模式）。
- **LLM 调参 prompt 扩展引导参数**（`core/plugin/autotune.py`）：分析要求从 8 点扩展到 12 点，新增：①惯性强度（after_reply_probability/probability_duration/proactive_temp_boost/proactive_boost_duration）；②瞥一眼机制（glance_enable/glance_group_count/glance_min_score）；③规则通道（rule_question_threshold/rule_context_threshold/fusion_weight_rule）；④冷却与间隔（cooldown_messages/proactive_min_interval/group_cooldown）。疲劳维度补充引导 fatigue_cost_track/glance/recovery_rate/high/medium_modifier/suppress_enabled。抑制分布维度新增 min_interval 占比分析。
- 12 项 v0.3.7 单元测试（Bug A 去重 4 / Bug B mark_applied 3 / get_stats 1 / proactive_min_interval 2 / cooldown_ratio 2）

### Fixed
- **Bug A LLM 关键词 object + 去重**（`core/plugin/autotune.py` `_apply_keywords_patch`）：LLM 输出的 `keywords_patch.add`/`remove` 项结构不规范导致问题。修复：①text 字段强制 str 转换（dict/list 转 repr，防止 [object Object]）；②add/remove 交叉去重（同一 (kind, text) 同时出现时优先 remove）；③add 内部按 (kind, text) 去重；④非 dict 项静默跳过。新增 `_normalize()` 内部函数统一处理结构校验与去重。
- **Bug B 手动强制调用重复显示**（`core/storage/tune_history.py` + `core/plugin/autotune.py`）：analyze 和 apply 都调 `tune_history.record()` 导致同一建议显示两次（一次未应用 + 一次应用）。修复：新增 `TuneHistoryStore.mark_applied(source)` 方法，apply 时查找最近一条 `action="analyze" AND applied=0 AND source=相同` 的记录更新为 `applied=1`，不再新增记录。若找不到对应 analyze 记录（跨重启/手动 apply），才新增一条 apply 记录。`get_stats` 的 `apply_count` 改为统计 `applied=1` 的记录数（不再依赖 `action="apply"`）。
- **群冷热统计不准**（`core/scheduler/scheduler.py`）：①`msg_timestamps` deque maxlen 从 100 增到 500（覆盖高频群 5 分钟消息量，避免 maxlen 不足导致 60 秒窗口统计少算）；②`cooldown_window` deque maxlen 从 200 增到 500；③`_cooldown_ratio` 从"最后 N 条消息"改为"时间窗口内消息"（`_COOLDOWN_TIME_WINDOW=300` 秒），旧逻辑在冷群中会跨越数小时导致误判。时间窗口内无消息时退化为取最后 N 条兜底。

### Changed
- `config_store.py DEFAULT_CONFIG` 新增 `proactive_min_interval`(180) + 校验器 `(int, 0, 86400)`
- `scheduler.py _get_group` 群状态新增 `last_proactive_ts` 字段（默认 0.0）
- `batch_pipeline.py run_batch` 在配额检查后新增 proactive_min_interval 冷却检查
- `batch_pipeline.py _dispatch_proactive` 注入路径和旧路径发送成功后更新 `g["last_proactive_ts"] = now`
- `tune_history.py get_stats` apply_count 统计逻辑从 `action="apply"` 改为 `applied=1`
- `tune_history.py` 新增 `mark_applied(source)` 方法
- `autotune.py llm_autotune` apply 路径：先调 `mark_applied(source)`，返回 False 时才新增 apply 记录
- `autotune.py _build_tune_prompt` 分析要求从 8 点扩展到 12 点
- `conftest.py default_config` 新增 `proactive_min_interval: 0`（测试环境禁用）
- 前端 `pages/prosocial/index.html` 配置面板「调度与轮询」分组新增 proactive_min_interval 控件 + 版本号 v0.3.6 → v0.3.7
- 插件版本 v0.3.6 → v0.3.7

### Notes
- 530 既有测试零回归（新增 12 项 v0.3.7 测试，全量 542/542 通过）
- proactive_min_interval 在测试环境默认禁用（=0），生产默认 180 秒由 ConfigStore.DEFAULT_CONFIG 提供
- mark_applied 按 source 匹配，手动触发（source="manual"）和自动触发（source="auto"）互不干扰
- _cooldown_ratio 时间窗口 300 秒与 _COOLDOWN_TIME_WINDOW 常量一致，冷群退化为最后 N 条兜底

## [0.3.6] - 2026-07-24

### Added
- **兴趣关键词即时反映（F1，`core/decision/interest.py`）**：`reject(kind, label, text)` 改为同步内存操作——立即从 active items/keywords 移除并加入 `_rejected` 列表，前端表格刷新后即时显示删除效果，不再需要手动「应用过滤」。新增 `_remove_from_active()` 内部方法统一处理 active 列表的移除逻辑（example 按 label+text 精确匹配，keyword 检测在 high 还是 hate 列表自动归类 kind）。`remove_item()` 与 `batch_update(removes)` 统一改走 reject 路径，所有删除（人类手动 + LLM 自动）都进入 `_rejected` 可恢复。
- **已过滤项恢复机制（F2，`core/decision/interest.py`）**：新增 `restore(kind, label, text)` 方法，从 `_rejected` 移除并调 `_add_back_to_active()` 加回 active items/keywords（与 reject 互逆）。keyword 类恢复时按存储的 `kind` 字段自动加回到 high_interest_keywords 或 hate_keywords。`_rejected.keywords` 格式从 `[str]` 迁移为 `[{"text": str, "kind": "high_keyword"|"hate_keyword"|""}]`，旧格式字符串自动迁移。Web API `POST /prosocial/interests` 新增 `action="restore"` 分支，reject/restore 后台触发 `apply_rejected` 重算质心兜底。
- **LLM 调参历史持久化（F3，`core/storage/tune_history.py` 新建）**：`TuneHistoryStore` 类独立 SQLite 数据库 `tune_history.db`（与 config.db 分离），表 `tune_history(id, timestamp, action, source, patch_json, keywords_patch_json, persona_revision, analysis, expected_effect, applied)` + 时间降序索引。`record()` 插入记录，`list(limit, offset)` 分页查询，`clear()` 清空，`get_stats()` 返回 total/analyze_count/apply_count/last_timestamp。`autotune.py llm_autotune` 在 analyze/apply 成功后调用 `record()` 持久化，新增 `source` 参数（"manual"/"auto"）区分手动触发与自动触发。
- **调参历史展示页面（F3，`pages/prosocial/index.html`）**：Dashboard 新增「调参历史」tab：顶部 4 个统计卡片（总次数 / analyze / apply / 最近时间）+ 可展开历史列表（每条显示 [时间][动作标签][来源][应用状态] + 分析摘要，展开后显示 analysis 全文、参数补丁 key-value 表、关键词增删列表、人设改写、预期效果）+ 清空历史按钮（二次确认）。Web API 新增 `GET /prosocial/tune_history?limit=50&offset=0` 与 `DELETE /prosocial/tune_history` + `POST` 别名（bridge 无 apiDelete）。
- 16 项 v0.3.6 单元测试（F1 reject 即时移除 3 / F2 restore 恢复 4 / F3 调参历史持久化 6 / API 透传 3）

### Changed
- `interest.py reject()` 由「仅加入 _rejected 等待 apply_rejected 移除」改为「立即 _remove_from_active + 加入 _rejected」，`apply_rejected()` 语义弱化为质心重算兜底
- `interest.py remove_item()` 与 `batch_update(removes)` 统一调 reject 逻辑，所有删除路径汇聚到单一可恢复入口
- `interest.py _filter_rejected()` 适配新 `[{"text", "kind"}]` 格式
- `web_bridge.py set_interests_view` reject/restore 后台触发 `_bg_apply_rejected()` 重算质心
- `web_bridge.py` 新增 `get_tune_history_view()` / `clear_tune_history_view()` 方法
- `web.py build_handlers` 返回 12 → 15 个 handler（新增 GET/DELETE/POST `/prosocial/tune_history`）
- `main.py __init__` 注入 `self._tune_history = TuneHistoryStore(data_dir / "tune_history.db")`；`terminate()` 关闭连接
- `autotune.py llm_autotune` 签名新增 `source: str = "manual"` 关键字参数；`_autotune_trigger` 调用时传 `source="auto"`
- Web API handler 总数 12 → 15（新增 tune_history GET/DELETE/POST 三个）
- 插件版本 v0.3.5 → v0.3.6

### Notes
- 509 既有测试零回归（新增 16 项 v0.3.6 测试，全量 525/525 通过）
- 调参历史独立 SQLite 文件，与 config.db 分离避免影响配置表性能
- reject/restore 内存同步操作 + 后台质心重算，前端表格立即反映
- 旧格式 `_rejected.keywords = [str]` 自动迁移为新格式 `[{"text", "kind"}]`，向后兼容

### Fixed (hotfix)
- **Bug A 关键词删除报错**（`core/plugin/web_bridge.py` + `core/decision/interest.py`）：前端关键词删除按钮 `data-kind="high_keyword"`/`"hate_keyword"` 被 `set_interests_view` action="reject" 拒绝（仅接受 `"example"`/`"keyword"`），返回 `{"ok":false,"error":"kind 必须是 example 或 keyword"}`。修复：reject/restore 校验放宽到 4 种 kind（`example`/`keyword`/`high_keyword`/`hate_keyword`），与底层 `interest_mgr.reject()` 已支持的 4 种 kind 对齐。`restore()` 同步支持 `high_keyword`/`hate_keyword`（行为与 `keyword` 一致，实际路由由 `_rejected` 中存储的 kind 决定）。
- **Bug B 已过滤关键词恢复按钮失效**（`pages/prosocial/index.html`）：`_rejected.keywords` 在 v0.3.6 改为 `[{"text": str, "kind": str}]` 字典格式，但前端 `renderInterests` 仍按字符串渲染，导致显示 `[object Object]` 且 `data-text="[object Object]"`，恢复请求带错误 text 后端找不到匹配项。修复：`rejKw.forEach` 提取 `r.text` 字段（兼容旧字符串格式），`data-text` 使用提取后的纯文本。
- 5 项回归测试（reject high_keyword/hate_keyword 各 1 / restore high_keyword/hate_keyword 各 1 / 未知 kind 仍被拒 1），全量 530/530 通过

## [0.3.5] - 2026-07-24

### Added
- **短批次合并**（F1，`core/scheduler/batch_pipeline.py`）：当批次文本过短（`< batch_min_text_length`，默认 12）且消息 ≤ 1 时，回填缓冲区等待下一次 `_schedule_batch` 触发时合并，最多合并 `batch_short_merge_max_attempts`（默认 2）次后强制评估，减少短消息噪声决策。成功评估后重置 `short_batch_attempts` 计数。
- **Emoji 过滤**（F2，`core/common/emoji_filter.py` 新建）：`strip_emoji(text)` 纯函数按 Unicode 范围移除 emoji 字符；`is_pure_emoji(text)` 判定纯 emoji。`GroupBuffer.append` 与 `scheduler.on_message` 入缓冲前过滤，纯 emoji 消息不入缓冲，净化 embedding 向量质量。
- **LLM 强制触发机制**（F4，`core/storage/tune_controller.py`）：`TuneRateLimiter` 新增 `_force_history` 独立冷却队列 + `allow_force(now, cooldown_hours)` / `record_force(now)` 方法。当窗口触发率 > `autotune_force_rate_threshold`（默认 0.50）时无视冷却期强制触发 LLM 调参，受 `autotune_force_cooldown_hours`（默认 1.0h）独立冷却防抖，避免短时间内反复强制触发。
- **对话状态模块**（F6，`core/decision/conversation_state.py` 新建）：轻量级纯启发式对话状态评估器，零 LLM/embedding 调用。`ConversationStateEvaluator.evaluate` 从最近 N 条消息判定 5 维度状态：`has_question`（有人抛问）/ `is_monologue`（自言自语）/ `is_argument`（激烈争论）/ `is_casual_chat`（闲聊）/ `bot_turn`（轮到机器人说话），输出 `appropriateness` 综合适宜度与 `modifier` 阈值修正倍率（0.7 放宽 ~ 1.3 收紧），应用到 `eff_threshold`，将"期待度"从单纯的文本匹配扩展为多维信号，降低机械感。
- **9 项新配置项**（全部非 null 默认）：`batch_min_text_length`(12) / `batch_short_merge_max_attempts`(2) / `emoji_filter_enabled`(true) / `autotune_force_rate_threshold`(0.50) / `autotune_force_cooldown_hours`(1.0) / `conversation_state_enabled`(true) / `conversation_state_window`(10) / `conversation_state_monologue_ratio`(0.6) / `conversation_state_argument_msg_len`(20)
- 32 项 v0.3.5 单元测试（F1 短批次合并 3 / F2 emoji 过滤 4 / F4 限流修复+强制触发 5 / F5 apply 异步化+批量重算 3 / F6 对话状态 17）

### Fixed
- **兴趣关键词 CRUD kind 错误**（F3，`pages/prosocial/index.html`）：`renderInterests` 中关键词按钮 `data-kind` 由固定 `"keyword"` 改为按 level 渲染（core/general → `high_keyword`，hate → `hate_keyword`），修复 `{"ok":false,"error":"kind 必须是 example、high_keyword 或 hate_keyword"}` 报错。
- **LLM 自动触发限流 bug**（F4，`core/scheduler/autotune_collector.py` + `core/plugin/autotune.py`）：原自动触发路径调 `llm_autotune("analyze", force=False)` 被 `TuneRateLimiter.allow()` 拒绝，导致"最需要修正时反而被限流"的设计反模式。改为 `force=True` 跳过 allow（仍 record 计入配额），强制触发额外受 `force_history` 独立冷却防抖。

### Changed
- **LLM apply 异步化 + 批量重算**（F5，`core/plugin/autotune.py` + `core/decision/interest.py`）：`InterestManager` 新增 `batch_update(adds, removes, embed_fn)` 方法，批量内存增删 + 单次 `_recompute_centroids` + 单次 `_save_npz`，从 N 次嵌入 API 调用降到 1 次。`_apply_keywords_patch` 重写调用 `batch_update`。`llm_autotune("apply")` 改造：`set_many` 同步生效后，关键词 patch + 人设 regenerate 放到 `asyncio.create_task` 后台执行，API 立即返回 `background:true`，不再阻塞响应。前端 `autotuneApply` 按钮点击立即禁用 + 文案"应用中…"，2 秒后恢复。
- `collect_tune_stats` 新增 `conversation_state_summary` 字段：遍历每群最近 N 条消息统计平均 `appropriateness` 与各状态占比，供 LLM 诊断时参考。
- `_build_tune_prompt` 注入 `conversation_state_summary`，分析要求新增第 7 点「对话状态」维度。
- `BatchDecision` 新增 `conversation_state_mod` 字段（默认 1.0 向后兼容），`_deserialize_decision` 同步反序列化。
- Dashboard 配置面板新增 2 个分组：「批次与输入过滤」（3 项）+「对话状态」（4 项），LLM 调参分组新增 2 项强制触发配置。
- 插件版本 v0.3.1 → v0.3.5

### Notes
- 短批次合并在测试默认配置下禁用（`batch_min_text_length=0`），生产默认 12 由 `ConfigStore.DEFAULT_CONFIG` 提供，避免破坏既有测试的短消息批次断言
- 对话状态模块评估异常时退化为 `modifier=1.0`，不影响主流程
- F4 强制触发受 1h 独立冷却防抖，避免触发率持续高位时反复调用 LLM
- 509 既有测试零回归（477 旧 + 32 新）

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
