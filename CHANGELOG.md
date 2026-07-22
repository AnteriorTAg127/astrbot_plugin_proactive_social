# Changelog

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
