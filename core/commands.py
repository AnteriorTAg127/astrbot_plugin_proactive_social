"""模块 E：CommandsMixin — ``/prosocial`` 指令组的处理逻辑实现。

v0.3.0 调整：指令注册（``@filter.command_group`` / ``@prosocial.command`` 装饰器）
已搬回 ``main.py``，本模块仅保留 9 个 ADMIN 子指令的处理逻辑（无装饰器）。
方法重命名为 ``_handle_*``，由 ``main.py`` 中的装饰器方法委托调用。

依赖实例属性（``ProSocialPlugin`` 主类提供，mixin 不定义 ``__init__``）：
``self.scheduler`` / ``self.interest_mgr`` / ``self._llm_fn`` / ``self._embed_fn`` /
``self._config_getter()`` / ``self._format_tune_status()`` / ``self.llm_autotune()``。
格式化函数 ``format_status`` / ``format_persona`` / ``format_scores`` 来自
``core/formatting.py``，作为模块级函数直接调用。
"""

from __future__ import annotations

import asyncio

from .formatting import format_persona, format_scores, format_status


class CommandsMixin:
    """``/prosocial`` 指令组处理逻辑 mixin（模块 E）。

    本 mixin 仅含指令的处理逻辑（``_handle_*`` 方法），指令注册装饰器在
    ``main.py`` 的 ``ProSocialPlugin`` 类体中声明，由装饰方法委托到本 mixin。
    mixin 不定义 ``__init__``，方法经 MRO 在 ``ProSocialPlugin`` 实例上生效。
    """

    async def _handle_status(self, event):
        """查看调度器/状态机/跟踪列表/今日指标/回放进度。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            status = self.scheduler.get_status()
            yield event.plain_result(format_status(status))
        except Exception as e:
            yield event.plain_result(f"获取状态失败: {e}")

    async def _handle_dryrun(self, event, arg: str = ""):
        """运行时切换 DRY_RUN：/prosocial dryrun on|off。"""
        try:
            arg = (arg or "").strip().lower()
            if arg not in ("on", "off"):
                yield event.plain_result("用法: /prosocial dryrun on|off")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            # 受控访问 scheduler._dry_run_override（运行时覆盖，不污染配置文件）
            self.scheduler._dry_run_override = arg == "on"
            yield event.plain_result(
                f"DRY_RUN 已{'开启' if arg == 'on' else '关闭'}（运行时覆盖）"
            )
        except Exception as e:
            yield event.plain_result(f"切换 dryrun 失败: {e}")

    async def _handle_enable(self, event):
        """在当前群启用主动唤醒（快捷开关，需群在白名单范围内或 mode=all）。"""
        try:
            group_id = event.get_group_id() or ""
            if not group_id:
                yield event.plain_result("仅在群内可用")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            await self.scheduler.set_group_enabled(group_id, True)
            yield event.plain_result("已在本群启用主动唤醒")
        except Exception as e:
            yield event.plain_result(f"启用失败: {e}")

    async def _handle_disable(self, event):
        """在当前群停用主动唤醒（快捷开关）。"""
        try:
            group_id = event.get_group_id() or ""
            if not group_id:
                yield event.plain_result("仅在群内可用")
                return
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            await self.scheduler.set_group_enabled(group_id, False)
            yield event.plain_result("已在本群停用主动唤醒")
        except Exception as e:
            yield event.plain_result(f"停用失败: {e}")

    async def _handle_persona(self, event, arg: str = ""):
        """查看兴趣分级摘要或重新生成：/prosocial persona show|reload。"""
        try:
            arg = (arg or "").strip().lower()
            if arg == "show":
                summary = self.interest_mgr.summary()
                yield event.plain_result(format_persona(summary))
            elif arg == "reload":
                if self._llm_fn is None or self._embed_fn is None:
                    yield event.plain_result("调度器未启动，无法重载")
                    return
                yield event.plain_result(
                    "开始重新生成兴趣语料（1 次 LLM + 批量嵌入），请稍候..."
                )
                try:
                    cfg = self._config_getter()
                    persona_text = str(cfg.get("persona_text", ""))
                    persona_knowledge = str(cfg.get("persona_knowledge", ""))
                    # v0.2.8 F4：从 cfg 读兴趣生成数量（_compute_persona_hash 纳入数量，需用配置值）
                    example_count = int(cfg.get("interest_example_count", 3))
                    keyword_count = int(cfg.get("interest_keyword_count", 12))
                    await self.interest_mgr.regenerate(
                        persona_text,
                        persona_knowledge,
                        self._llm_fn,
                        self._embed_fn,
                        example_count=example_count,
                        keyword_count=keyword_count,
                    )
                    yield event.plain_result("兴趣语料已重新生成")
                except Exception as e:
                    yield event.plain_result(f"重新生成失败: {e}")
            else:
                yield event.plain_result("用法: /prosocial persona show|reload")
        except Exception as e:
            yield event.plain_result(f"persona 指令失败: {e}")

    async def _handle_scores(self, event, n: str = "10"):
        """查看最近 N（默认 10）条批次决策得分。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            try:
                limit = int(n)
            except (TypeError, ValueError):
                limit = 10
            if limit <= 0:
                limit = 10
            # 受控访问 scheduler._decision_log.recent（决策日志读取）
            decisions = self.scheduler._decision_log.recent(limit)
            yield event.plain_result(format_scores(decisions))
        except Exception as e:
            yield event.plain_result(f"获取决策记录失败: {e}")

    async def _handle_replay(self, event, name: str = "", speed: str = "1.0"):
        """历史回放：/prosocial replay <名称> [倍速] 或 /prosocial replay stop。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            name = (name or "").strip()
            if not name:
                files = self.scheduler._replay_engine.list_files()
                if not files:
                    yield event.plain_result(
                        "无可用回放文件（放于 "
                        "data/plugin_data/astrbot_plugin_proactive_social/replay/*.jsonl）"
                    )
                else:
                    yield event.plain_result("可用回放文件:\n" + "\n".join(files))
                return
            if name == "stop":
                self.scheduler.stop_replay()
                yield event.plain_result("已请求停止回放")
                return
            try:
                sp = float(speed)
            except (TypeError, ValueError):
                sp = float(self._config_getter().get("replay_speed", 1.0))
            # 回放是长任务，后台执行，立即回复
            asyncio.create_task(self.scheduler.replay(name, sp))
            yield event.plain_result(f"开始回放 {name}（倍速 {sp}，强制不发送）")
        except Exception as e:
            yield event.plain_result(f"replay 指令失败: {e}")

    async def _handle_fatigue(self, event):
        """查看全局疲劳值/级别/影响因子（v0.2）。"""
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            snap = self.scheduler._fatigue.snapshot()
            mod = self.scheduler._fatigue.threshold_modifier()
            suppress = self.scheduler._fatigue.should_suppress(False)
            lines = [
                f"疲劳值: {snap.get('value', 0)} / {snap.get('limit', 0)}",
                f"比率: {snap.get('ratio', 0):.2f} | 级别: {snap.get('level', 'none')}",
                f"阈值倍率 A_modifier: {mod:.2f}",
                f"高疲劳抑制非强制唤醒: {'是' if suppress else '否'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"fatigue 指令失败: {e}")

    async def _handle_tune(self, event, arg: str = ""):
        """v0.2.9 F3/F5：LLM 诊断调参（全视野 + 速率限制）。

        用法：``/prosocial tune [proactive|passive|balanced]``（默认 balanced）；
        加 ``force`` 前缀跳过速率限制；``status`` 查看速率+缓存摘要；``apply`` 应用缓存建议。
        均需 ADMIN 权限。LLM 调用慢，直接 await 等待回复。
        """
        try:
            if self.scheduler is None:
                yield event.plain_result("调度器未启动")
                return
            raw_arg = (arg or "").strip()
            # v0.2.9 F5：status 子命令——显示速率限制状态 + 上次建议摘要
            if raw_arg == "status":
                yield event.plain_result(self._format_tune_status())
                return
            if raw_arg == "apply":
                result = await self.llm_autotune("apply")
                if result.get("ok"):
                    yield event.plain_result(
                        f"✅ 已应用 {result.get('updated', 0)} 项参数"
                    )
                else:
                    yield event.plain_result(
                        f"❌ 应用失败：{result.get('error', '未知')}"
                    )
                return
            # v0.2.9 F4：force 子命令——跳过速率限制（仍 record 计数）
            force = False
            style_arg = raw_arg
            if raw_arg == "force" or raw_arg.startswith("force "):
                force = True
                style_arg = raw_arg[5:].strip()  # 去掉 "force" 前缀
            # 解析风格参数（proactive/passive/balanced），无效值回退 balanced
            style = (
                style_arg
                if style_arg in ("proactive", "passive", "balanced")
                else "balanced"
            )
            result = await self.llm_autotune("analyze", style=style, force=force)
            if not result.get("ok"):
                # v0.2.9 F4：被速率限制时回显 retry_after / 已用配额
                if result.get("error") == "rate_limited":
                    rate = result.get("rate_limit", {}) or {}
                    used = rate.get("used", 0)
                    limit = rate.get("limit", 0)
                    next_avail = int(rate.get("next_available", 0))
                    hours = next_avail // 3600
                    minutes = (next_avail % 3600) // 60
                    yield event.plain_result(
                        f"⏳ 触发速率限制（{result.get('reason', '')}）："
                        f"今日已用 {used}/{limit}，"
                        f"下次可用约 {hours}小时{minutes}分钟后"
                        f"\n（ADMIN 可用 /prosocial tune force 强制分析）"
                    )
                else:
                    yield event.plain_result(
                        f"❌ 分析失败：{result.get('error', '未知')}"
                    )
                return
            analysis = result.get("analysis", "") or ""
            patch = result.get("suggested_patch", {}) or {}
            keywords_patch = result.get("suggested_keywords_patch") or None
            persona_rev = result.get("persona_revision") or None
            expected = result.get("expected_effect", "") or ""
            patch_str = (
                "\n".join(f"  {k}: {v}" for k, v in patch.items())
                if patch
                else "  （无建议）"
            )
            extra = ""
            if keywords_patch:
                extra += "\n\n（含关键词增删建议）"
            if persona_rev:
                extra += "\n（含人设改写建议）"
            yield event.plain_result(
                f"📊 诊断结果\n\n分析：\n{analysis}\n\n建议参数：\n{patch_str}"
                f"\n\n预期效果：{expected}{extra}"
                f"\n\n应用建议：/prosocial tune apply"
            )
        except Exception as e:
            yield event.plain_result(f"tune 指令失败: {e}")
