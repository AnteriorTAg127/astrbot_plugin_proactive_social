# astrbot_plugin_proactive_social

基于向量决策驱动的多群主动社交插件 —— 让机器人在群聊中自然地主动插话、接话、瞥一眼。

## 功能

- **主动回复走消息管线（v0.2.8）**：主动回复注入 AstrBot 标准消息管线（合成 AstrBotMessage → handle_msg），追踪页自动显示完整事件与 LLM 调用记录，对话历史自动保存；on_llm_request 钩子注入接话风格提示（不污染历史）；关闭或异常时降级回直连 LLM 旧路径
- **自适应阈值控制器（v0.2.8）**：每群按近期触发率自动收敛阈值倍率（5%-30% 触发率带），消除 embedding 尺度差异导致的调参敏感；mult 钳制 [0.5, 2.0]，状态持久化
- **每群发送频率硬上限（v0.2.8）**：max_proactive_per_hour / max_proactive_per_day 超限后 suppressed_reason="quota"，调参失误的最终兜底，任何参数调崩都不会话痨
- **LLM 自动诊断调参（v0.2.8）**：`/prosocial tune` 指令 + Dashboard 按钮，分析最近 200 条决策数据生成参数建议（18 键白名单 + 校验器双重过滤后应用），无需手动试错
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
- **Dashboard 前端**：状态/决策记录/得分趋势/实时配置/群管理/兴趣管理/JSON 导出/LLM 调参，12 个 Web API
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
| `/prosocial tune [apply]` | ADMIN | LLM 诊断调参：分析最近 200 条决策生成参数建议；`apply` 应用缓存建议（v0.2.8） |

## 配置

参考插件 WebUI 配置面板（`_conf_schema.json`）。主要配置项：

- **基础**：人设、活跃时段、群模式、base_threshold、作息调度、示例句子数量、关键词数量
- **管线与自适应（v0.2.8）**：reply_via_pipeline / adaptive_threshold_enabled / max_proactive_per_hour / max_proactive_per_day
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
