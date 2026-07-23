# Changelog

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
