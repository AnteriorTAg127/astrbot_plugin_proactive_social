"""指令输出格式化函数（main.py 静态方法提取，模块 A 产出）。

将 main.py 中 3 个 @staticmethod 改为模块级独立函数：
- format_status：运行状态摘要
- format_persona：人设/兴趣摘要
- format_scores：决策记录列表

仅纯字符串格式化，无 astrbot / 依赖注入，保证离线可测。
"""

from __future__ import annotations


def format_status(status: dict) -> str:
    lines = [
        f"运行: {status.get('running', False)} | "
        f"活跃时段: {status.get('in_active_hours', False)} | "
        f"DRY_RUN: {status.get('dry_run', False)}",
        f"回放中: {status.get('replay_active', False)} | "
        f"兴趣已加载: {status.get('interest_loaded', False)} | "
        f"决策记录数: {status.get('decision_count', 0)}",
    ]
    m = status.get("metrics", {}) or {}
    lines.append(
        f"今日指标: LLM={m.get('llm_calls', 0)} "
        f"嵌入={m.get('embedding_calls', 0)} "
        f"主动发送={m.get('proactive_sends', 0)} "
        f"触发={m.get('proactive_triggered', 0)}"
    )
    # v0.2 全局疲劳摘要（紧跟今日指标行，便于一眼看到 bot 疲劳状态）
    f = status.get("fatigue", {}) or {}
    lines.append(
        f"全局疲劳: {f.get('value', 0)}/{f.get('limit', 0)} ({f.get('level', 'none')})"
    )
    cm = status.get("current_monitoring", []) or []
    lines.append(f"当前监听群: {', '.join(cm) if cm else '无'}")
    lines.append("各群状态:")
    for g in status.get("groups", []) or []:
        lines.append(
            f"  {g.get('id')}: {g.get('state')} 启用={g.get('enabled')} "
            f"跟踪={g.get('tracker_count', 0)} msg/min={g.get('msg_per_min', 0)}"
        )
    return "\n".join(lines)


def format_persona(summary: dict) -> str:
    if not summary.get("loaded", False):
        return "兴趣数据未加载"
    lines = [
        f"人设哈希: {summary.get('persona_hash', '')} | 维度: {summary.get('dim', 0)}"
    ]
    levels = summary.get("levels", {}) or {}
    for lv in ("core", "general", "marginal", "hate"):
        info = levels.get(lv, {}) or {}
        topics = info.get("topics", []) or []
        lines.append(
            f"[{lv}] 权重={info.get('weight', 0)} 数量={info.get('count', 0)} "
            f"主题: {', '.join(topics) if topics else '无'}"
        )
    hk = summary.get("hate_keywords", []) or []
    hik = summary.get("high_interest_keywords", []) or []
    lines.append(f"高唤醒关键词: {', '.join(hik) if hik else '无'}")
    lines.append(f"反感关键词: {', '.join(hk) if hk else '无'}")
    return "\n".join(lines)


def format_scores(decisions: list[dict]) -> str:
    if not decisions:
        return "无决策记录"
    lines = [f"最近 {len(decisions)} 条决策（新→旧）:"]
    for i, d in enumerate(decisions, 1):
        f = d.get("factors", {}) or {}
        lines.append(
            f"{i}. [{d.get('ts', 0):.0f}] 群={d.get('group_id', '')} "
            f"score={d.get('score', 0):.3f} thr={d.get('threshold', 0):.3f} "
            f"hit={d.get('hit_level', 'none')} "
            f"int={f.get('s_int', 0):.2f} topic={f.get('s_topic', 0):.2f} "
            f"resp={f.get('s_resp', 0):.2f} cd={f.get('c_cooldown', 0):.2f} "
            f"sil={f.get('p_silence', 0):.2f} "
            f"触发={d.get('triggered', False)} "
            f"原因={d.get('suppressed_reason', '') or 'below_threshold'} "
            f"DRY={d.get('dry_run', False)} "
            f"a={d.get('score_a', 0):.2f} b={d.get('score_b', 0):.2f} "
            f"α={d.get('alpha', 0):.2f} ch={d.get('channel', '')}"
        )
    return "\n".join(lines)
