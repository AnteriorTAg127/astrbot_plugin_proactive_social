# astrbot_plugin_proactive_social

基于向量决策驱动的多群主动社交插件 —— 让机器人在群聊中自然地主动插话、接话、瞥一眼。

## 功能

- **兴趣关键词即时反映 + 恢复（v0.3.6）**：删除/修改兴趣关键词后表格立即反映（reject 改为同步内存操作）；已过滤项在「已过滤选项」区可一键「恢复」回到 active 列表，人类操作与 LLM 自主操作产生的过滤项统一可恢复；reject/restore 后台触发质心重算兜底，前端无需手动「应用过滤」
- **LLM 调参历史页面（v0.3.6）**：Dashboard 新增「调参历史」tab，展示每次 LLM 调参的完整记录（时间/动作/来源/应用状态/分析全文/参数补丁/关键词增删/人设改写/预期效果），含统计卡片（总次数/analyze/apply/最近时间）与清空按钮；独立 SQLite `tune_history.db` 持久化，区分手动触发与自动触发（source=manual/auto）
- **对话状态模块（v0.3.5）**：轻量级纯启发式对话状态评估器（零 LLM/embedding 调用），从群聊情绪氛围与对话角色多维度判断当前是否适合插话：has_question（有人抛问）/ bot_turn（轮到机器人说话）/ is_casual_chat（闲聊）/ is_monologue（自言自语）/ is_argument（激烈争论），输出 modifier 修正 eff_threshold（0.7 放宽 ~ 1.3 收紧），将"期待度"从单纯的文本匹配扩展为多维信号，降低机械感
- **短批次合并（v0.3.5）**：批次文本过短（< batch_min_text_length）且消息 ≤ 1 时回填缓冲区等待下一次合并，最多合并 batch_short_merge_max_attempts 次后强制评估，减少短消息噪声决策
- **Emoji 过滤（v0.3.5）**：入缓冲区前移除 Unicode emoji 字符，纯 emoji 消息不入缓冲，净化 embedding 向量质量
- **LLM 强制触发机制（v0.3.5）**：窗口触发率 > autotune_force_rate_threshold（默认 0.50）时无视冷却期强制触发 LLM 调参，受 autotune_force_cooldown_hours（默认 1.0h）独立冷却防抖；修复自动触发路径被限流拒绝的 bug（force=True 跳过 allow 仍 record）
- **LLM apply 异步化 + 批量重算（v0.3.5）**：InterestManager.batch_update 批量增删 + 单次重算质心（N 次嵌入 API → 1 次）；apply 响应立即返回（set_many 同步生效，关键词 patch + 人设 regenerate 后台执行），不再阻塞
- **主动回复走消息管线（v0.2.8）**：主动回复注入 AstrBot 标准消息管线（合成 AstrBotMessage → handle_msg），追踪页自动显示完整事件与 LLM 调用记录，对话历史自动保存；on_llm_request 钩子注入接话风格提示（不污染历史）；关闭或异常时降级回直连 LLM 旧路径
- **自适应阈值控制器（v0.2.8）**：每群按近期触发率自动收敛阈值倍率（5%-30% 触发率带），消除 embedding 尺度差异导致的调参敏感；mult 钳制 [0.5, 2.0]，状态持久化
- **每群发送频率硬上限（v0.2.8）**：max_proactive_per_hour / max_proactive_per_day 超限后 suppressed_reason="quota"，调参失误的最终兜底，任何参数调崩都不会话痨
- **LLM 全视野调参（v0.2.9）**：`_build_tune_prompt` 重写为注入全量配置（减 6 项 `TUNE_DENYLIST` 安全敏感键）+ 兴趣关键词（items/hate/high_interest/rejected）+ 人设文本+补充知识 + 作息 schedule + 群白名单 + AdaptiveThreshold 状态（mult/window_rate）；LLM 输出三段建议：`suggested_patch`（标量配置）/ `suggested_keywords_patch`（关键词增删）/ `persona_revision`（人设改写）
- **触发率越界自动调参（v0.2.9）**：复用 AdaptiveThreshold 评估周期，窗口触发率 >`autotune_safe_rate_hi`（默认 0.30）/<`autotune_safe_rate_lo`（默认 0.05）且样本≥`autotune_min_decisions`（默认 30）时后台 `asyncio.create_task` 调 LLM 重写参数；`autotune_auto_apply=true` 时自动应用建议，否则仅缓存
- **LLM 调参速率限制（v0.2.9）**：`core/tune_controller.py` `TuneRateLimiter` 对所有调参调用（手动 + 自动）施加冷却（`autotune_cooldown_hours` 默认 3h）+ 日上限（`autotune_max_per_day` 默认 4 次），ADMIN 可 `force` 跳过但仍 `record()` 计入配额；状态持久化到 SQLite KV `tune_rate_state`
- **LLM 扩展可写键 denylist 模式（v0.2.9）**：用 6 项 `TUNE_DENYLIST`（enable/dry_run/group_whitelist/group_mode/chat_provider_id/embedding_provider_id）替换 v0.2.8 的 18 项白名单，可写键扩到约 70 项 + 关键词增删 + 人设改写；DENYLIST 键被 LLM 输出时丢弃并在 analysis 末尾注明 `[已过滤安全敏感键: ...]`；apply 分流四类：标量走 ConfigStore.set_many / persona 变更触发后台 regenerate / keywords_patch 走 interest_mgr.add_item+remove_item+apply_rejected / persona_revision 合并入 persona_text
- **LLM 自动诊断调参（v0.2.8）**：`/prosocial tune` 指令 + Dashboard 按钮，分析最近 200 条决策数据生成参数建议，无需手动试错
- **兴趣生成数量配置生效（v0.2.8）**：persona_hash 纳入 example_count/keyword_count，改数量触发缓存失效与后台重建；重启不再回退
- **兴趣关键词增删改查（v0.2.6）**：支持在 Dashboard 中添加/编辑/删除兴趣关键词和示例句子，自动重算向量质心
- **可配置的示例句子和关键词数量（v0.2.6）**：interest_example_count / interest_keyword_count 控制生成数量
- **决策记录 JSON 导出（v0.2.6）**：一键导出完整配置+决策+疲劳+兴趣数据，支持 AI 辅助调参
- **Embedding 降级标记（v0.2.6）**：嵌入失败时决策表显示降级标签，Score B 不再伪计算
- **基于回复分词的连续对话匹配（v0.2.5）**：Bot 回复后用 jieba 提取关键词缓存，目标用户下次发消息时计算匹配得分叠加到融合 final_score；个人跟踪在向量相似度不足时以关键词作为强信号直接触发；零额外 LLM/嵌入调用
- **双通道唤醒决策（v0.2）**：向量语义通道（score_b）+ 规则模式通道（score_a），融合判定 `final = α·score_a + (1−α)·score_b`，支持通道开关与动态权重
- **全局疲劳管理（v0.2）**：bot 级单例，指数衰减，按回复类型消耗（active/passive/track/glance），高疲劳阈值惩罚 + 非强制抑制
- **对话惯性（v0.2）**：回复后阈值倍率 + 主动话题临时提升 + 成功/失败计数
- **等待窗口（v0.2）**：回复后收集同用户连续后续消息，合并为一条连贯回复
- **人设兴趣管理**：LLM 生成兴趣语料 + 内置 Embedding 向量化 + npz 持久化
- **动态批处理与五因子唤醒决策**：兴趣/话题/回应期待/冷却/沉默 + 动态阈值 + 反感屏蔽 + 规则降级
- **双窗口上下文**：短窗口常注；长窗口仅主动触发时注入相关性 Top-N
- **个人跟踪回复**：无 @ 接话，低阈值，超时清理
- **多群瞥一眼**：期待回复期随机选群、关键词+向量快判、快速插话
- **多群轮询与作息调度**：时段±抖动、单群专注、群冷却、五态状态机
- **群白名单 + 快捷开关**：whitelist/all 模式，AND 语义实时生效
- **DRY_RUN + 决策日志环 + 每日指标**
- **Dashboard 前端**：状态/决策记录/得分趋势/实时配置/群管理/兴趣管理/JSON 导出/LLM 调参/调参历史，15 个 Web API
- **历史回放**：JSONL 按时间流速喂入决策管线，强制不发送

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_proactive_social` 安装，或手动克隆到 `data/plugins/`。

## 指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/prosocial status` | ADMIN | 查看运行状态、群列表、今日指标、全局疲劳 |
| `/prosocial dryrun [on\|off]` | ADMIN | 切换 DRY_RUN 干运行 |
| `/prosocial enable <group_id>` | ADMIN | 启用某群 |
| `/prosocial disable <group_id>` | ADMIN | 停用某群 |
| `/prosocial persona <text>` | ADMIN | 设置人设文本 |
| `/prosocial scores [n]` | ADMIN | 查看最近 n 条决策记录（含 score_a/score_b/α/channel） |
| `/prosocial replay <file>` | ADMIN | 回放历史消息 JSONL |
| `/prosocial fatigue` | ADMIN | 查看全局疲劳值/级别/阈值倍率/抑制状态（v0.2） |
| `/prosocial tune [style\|apply\|force\|status]` | ADMIN | LLM 全视野调参：`[style]` 分析（受速率限制，可选 proactive/passive/balanced）；`apply` 应用缓存建议；`force [style]` 强制跳过速率限制；`status` 查看速率限制状态 + 上次建议摘要 + 自动触发开关（v0.2.9） |

## 配置

参考插件 WebUI 配置面板（`_conf_schema.json`）。主要配置项：

- **基础**：人设、活跃时段、群模式、base_threshold、作息调度、示例句子数量、关键词数量
- **管线与自适应（v0.2.8）**：reply_via_pipeline / adaptive_threshold_enabled / max_proactive_per_hour / max_proactive_per_day
- **LLM 调参（v0.2.9 + v0.3.5）**：autotune_safe_rate_hi / autotune_safe_rate_lo / autotune_auto_trigger_enabled / autotune_auto_apply / autotune_min_decisions / autotune_cooldown_hours / autotune_max_per_day / autotune_force_rate_threshold / autotune_force_cooldown_hours
- **批次与输入过滤（v0.3.5）**：batch_min_text_length / batch_short_merge_max_attempts / emoji_filter_enabled
- **对话状态（v0.3.5）**：conversation_state_enabled / conversation_state_window / conversation_state_monologue_ratio / conversation_state_argument_msg_len
- **双通道融合（v0.2）**：enable_rule_channel / enable_vector_channel / fusion_weight_rule / dynamic_fusion_enabled
- **规则引擎（v0.2）**：强唤醒词 / 上下文唤醒词 / 疑问信号 / 屏蔽短语
- **疲劳（v0.2）**：衰减率 / 上限 / 各类型消耗成本 / 高中疲劳阈值修正 / 抑制开关
- **惯性（v0.2）**：回复后概率 / 持续时长 / 等待窗口时长 / 最大追加条数 / 主动话题提升
- **上下文（v0.2.6）**：long_window_inject_proactive 控制主动回复是否注入长窗口
- **Embedding（v0.2.6）**：embedding_provider_id 使用 AstrBot 原生 provider 选择器

## 开发

详见 `开发/` 目录下的设计文档（PRD、分工、测试报告）。

## 依赖

见 `requirements.txt`。核心依赖：`numpy`（向量计算）、`aiosqlite`（配置持久化）。
