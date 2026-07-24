"""test_message_filter.py —— MessageFilter 单元测试（v0.3.11）。

测试对象：
- core/decision/message_filter.py → MessageFilter.should_filter

覆盖范围：
- 规则 A 黑名单匹配（命中/不命中 + 短消息包含 vs 长消息不包含边界）
- 规则 B 无意义短语（命中/不命中 + 精确匹配 vs 子串不命中）
- 规则 C 重复刷屏（命中/不命中 + 时间窗口边界 窗口内/窗口外）
- 规则 D 纯表情/单字（命中/不命中）
- 规则 E 超短消息（命中/不命中 + 含问号豁免）
- 总开关关闭时所有规则失效
- 配置自定义词表
- 规则顺序 A→B→C→D→E（任一命中即返回）

测试策略：纯标准库模块直接实例化断言，不依赖 conftest fixtures。
"""

from __future__ import annotations

from core.decision.message_filter import MessageFilter

# ======================================================================
# 规则 A：黑名单匹配
# ======================================================================


class TestRuleABlacklist:
    """规则 A：黑名单匹配（精确匹配 OR 短消息包含）。"""

    def test_exact_match_hit(self):
        """精确匹配黑名单词 → 命中 A。"""
        f = MessageFilter()
        assert f.should_filter("打卡", "u1", 0.0) == (True, "A")

    def test_exact_match_hit_plus1(self):
        """精确匹配 '+1' → 命中 A。"""
        f = MessageFilter()
        assert f.should_filter("+1", "u1", 0.0) == (True, "A")

    def test_long_message_not_filtered(self):
        """长消息包含黑名单词但长度 > short_msg_len → 不命中 A。

        '我打卡了' 长度 5 > 默认 short_msg_len=2，虽包含 '打卡' 但不过滤。
        """
        f = MessageFilter()
        filtered, rule = f.should_filter("我打卡了", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_short_message_contains_hit(self):
        """短消息（长度 ≤ short_msg_len）包含黑名单词但非精确匹配 → 命中 A。

        配置 short_msg_len=3，blacklist=['ab']，text='xab'（长度 3 ≤ 3，包含 'ab'）。
        """
        f = MessageFilter({"filter_short_msg_len": 3, "filter_blacklist": ["ab"]})
        filtered, rule = f.should_filter("xab", "u1", 0.0)
        assert filtered is True
        assert rule == "A"

    def test_short_message_not_containing_blacklist_word(self):
        """短消息不含任何黑名单词 → 不命中 A。

        '赞' 长度 1 ≤ 2，但 '赞我' 不在 '赞' 中 → 不命中 A。
        （但会被规则 D 命中，因长度==1）
        """
        f = MessageFilter()
        filtered, rule = f.should_filter("赞", "u1", 0.0)
        assert filtered is True
        assert rule == "D"

    def test_boundary_short_vs_long(self):
        """边界：short_msg_len=3，长度 3 包含 → 命中 A；长度 4 包含 → 不命中 A。"""
        f = MessageFilter({"filter_short_msg_len": 3, "filter_blacklist": ["ab"]})
        # 长度 3 ≤ 3，包含 'ab' → 命中 A
        assert f.should_filter("xab", "u1", 0.0) == (True, "A")
        # 长度 4 > 3，包含 'ab' 但不命中 A（精确匹配也失败）
        filtered, rule = f.should_filter("xxab", "u1", 0.0)
        assert filtered is False
        assert rule == ""


# ======================================================================
# 规则 B：纯拟声/无意义短语
# ======================================================================


class TestRuleBMeaninglessPhrases:
    """规则 B：纯拟声/无意义短语（精确匹配）。"""

    def test_exact_match_hit(self):
        """精确匹配无意义短语 → 命中 B。"""
        f = MessageFilter()
        assert f.should_filter("啊啊啊", "u1", 0.0) == (True, "B")

    def test_exact_match_hahaha(self):
        """精确匹配 '哈哈哈哈' → 命中 B。"""
        f = MessageFilter()
        assert f.should_filter("哈哈哈哈", "u1", 0.0) == (True, "B")

    def test_substring_not_matched(self):
        """包含短语但非精确匹配 → 不命中 B。

        '啊啊啊!' 不是 '啊啊啊' 的精确匹配 → 不命中 B。
        """
        f = MessageFilter()
        filtered, rule = f.should_filter("啊啊啊!", "u1", 0.0)
        # 不命中 B，但可能命中其他规则
        assert rule != "B"

    def test_normal_message_not_filtered(self):
        """正常消息不命中 B。"""
        f = MessageFilter()
        assert f.should_filter("你好啊", "u1", 0.0) == (False, "")


# ======================================================================
# 规则 C：重复刷屏
# ======================================================================


class TestRuleCBurst:
    """规则 C：重复刷屏（同一 user_id 在窗口内发送 ≥ burst_count 条相同消息）。"""

    def test_burst_hit(self):
        """3 条相同消息在窗口内 → 第 3 条命中 C。"""
        f = MessageFilter()  # burst_count=3, burst_window=10
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u1", 1.0) == (False, "")
        filtered, rule = f.should_filter("spam", "u1", 2.0)
        assert filtered is True
        assert rule == "C"

    def test_burst_not_hit_different_messages(self):
        """不同消息不触发刷屏。"""
        f = MessageFilter()
        assert f.should_filter("aaa", "u1", 0.0) == (False, "")
        assert f.should_filter("bbb", "u1", 1.0) == (False, "")
        assert f.should_filter("ccc", "u1", 2.0) == (False, "")

    def test_burst_not_hit_below_threshold(self):
        """窗口内仅 2 条相同（< burst_count=3）→ 不命中 C。"""
        f = MessageFilter()
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u1", 1.0) == (False, "")
        # 第 3 条是不同消息，不触发
        assert f.should_filter("other", "u1", 2.0) == (False, "")

    def test_window_boundary_within(self):
        """窗口边界（窗口内）：burst_window=10，ts=0/5/10 均在窗口内 → 命中 C。

        ts=10 时 cutoff=0，ts=0 的记录（0 不 < 0）保留 → count=3。
        """
        f = MessageFilter({"filter_burst_window": 10, "filter_burst_count": 3})
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u1", 5.0) == (False, "")
        filtered, rule = f.should_filter("spam", "u1", 10.0)
        assert filtered is True
        assert rule == "C"

    def test_window_boundary_outside(self):
        """窗口边界（窗口外）：burst_window=10，ts=0/11/15 → ts=0 被清理 → 不命中 C。

        ts=15 时 cutoff=5，ts=0 (<5) 被清理，ts=11 (≥5) 保留 → count=2 < 3。
        """
        f = MessageFilter({"filter_burst_window": 10, "filter_burst_count": 3})
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u1", 11.0) == (False, "")
        filtered, rule = f.should_filter("spam", "u1", 15.0)
        assert filtered is False
        assert rule == ""

    def test_burst_whitespace_normalized(self):
        """去空白后相同的消息视为相同。"""
        f = MessageFilter({"filter_burst_count": 3, "filter_burst_window": 10})
        # 使用不在黑名单/短语词表中的文本，去空白后均为 'hello'
        assert f.should_filter("hello", "u1", 0.0) == (False, "")
        assert f.should_filter("he llo", "u1", 1.0) == (False, "")
        # 去空白后均为 'hello'，count=3 → 命中 C
        filtered, rule = f.should_filter("h e l l o", "u1", 2.0)
        assert filtered is True
        assert rule == "C"

    def test_burst_isolated_per_user(self):
        """不同用户的刷屏计数独立。"""
        f = MessageFilter({"filter_burst_count": 3, "filter_burst_window": 10})
        # u1 和 u2 各发 2 条相同消息，均不触发（各自 count=2 < 3）
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u2", 1.0) == (False, "")
        assert f.should_filter("spam", "u1", 2.0) == (False, "")
        assert f.should_filter("spam", "u2", 3.0) == (False, "")
        # u1 第 3 条触发（u1 count=3），证明 u2 的 2 条未计入 u1
        assert f.should_filter("spam", "u1", 4.0) == (True, "C")


# ======================================================================
# 规则 D：纯表情/单字
# ======================================================================


class TestRuleDPureSymbol:
    """规则 D：纯表情/单字（正则 ^[\\s\\W\\d_]+$ 或长度 == 1）。"""

    def test_pure_punctuation_hit(self):
        """纯标点 → 命中 D。"""
        f = MessageFilter()
        assert f.should_filter("？？？", "u1", 0.0) == (True, "D")

    def test_single_char_hit(self):
        """单字符 → 命中 D。"""
        f = MessageFilter()
        assert f.should_filter("哈", "u1", 0.0) == (True, "D")

    def test_digits_only_hit(self):
        """纯数字 → 命中 D。"""
        f = MessageFilter()
        assert f.should_filter("123", "u1", 0.0) == (True, "D")

    def test_normal_text_not_filtered(self):
        """正常文本（含字母） → 不命中 D。"""
        f = MessageFilter()
        filtered, rule = f.should_filter("hello", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_emoji_hit(self):
        """emoji → 命中 D（长度==1 或正则匹配）。"""
        f = MessageFilter()
        filtered, rule = f.should_filter("💀", "u1", 0.0)
        assert filtered is True
        assert rule == "D"


# ======================================================================
# 规则 E：超短消息
# ======================================================================


class TestRuleEShortMessage:
    """规则 E：超短消息（长度 ≤ short_msg_len 且不含问号）。"""

    def test_short_without_question_hit(self):
        """短消息不含问号 → 命中 E。

        'ab' 长度 2 ≤ 2，不含 ?，且为字母（不命中 D 的正则，长度!=1）。
        """
        f = MessageFilter()
        assert f.should_filter("ab", "u1", 0.0) == (True, "E")

    def test_short_chinese_letters_hit(self):
        """短中文不含问号 → 命中 E。

        '你好' 长度 2 ≤ 2，不含 ?，中文是字母（不命中 D 的正则）。
        """
        f = MessageFilter()
        assert f.should_filter("你好", "u1", 0.0) == (True, "E")

    def test_short_with_question_not_filtered(self):
        """短消息含问号 → 不命中 E（避免误伤短问句）。"""
        f = MessageFilter()
        filtered, rule = f.should_filter("好？", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_long_message_not_filtered(self):
        """长消息 → 不命中 E。"""
        f = MessageFilter()
        filtered, rule = f.should_filter("hello world", "u1", 0.0)
        assert filtered is False
        assert rule == ""


# ======================================================================
# 总开关关闭时所有规则失效
# ======================================================================


class TestMasterSwitch:
    """filter_enabled=False 时所有规则失效。"""

    def test_disabled_blacklist_not_filtered(self):
        """关闭后黑名单不生效。"""
        f = MessageFilter({"filter_enabled": False})
        assert f.should_filter("打卡", "u1", 0.0) == (False, "")

    def test_disabled_meaningless_not_filtered(self):
        """关闭后无意义短语不生效。"""
        f = MessageFilter({"filter_enabled": False})
        assert f.should_filter("啊啊啊", "u1", 0.0) == (False, "")

    def test_disabled_burst_not_filtered(self):
        """关闭后刷屏检测不生效。"""
        f = MessageFilter({"filter_enabled": False})
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        assert f.should_filter("spam", "u1", 1.0) == (False, "")
        assert f.should_filter("spam", "u1", 2.0) == (False, "")

    def test_disabled_pure_symbol_not_filtered(self):
        """关闭后纯表情不生效。"""
        f = MessageFilter({"filter_enabled": False})
        assert f.should_filter("？？？", "u1", 0.0) == (False, "")

    def test_disabled_short_not_filtered(self):
        """关闭后超短消息不生效。"""
        f = MessageFilter({"filter_enabled": False})
        assert f.should_filter("ab", "u1", 0.0) == (False, "")


# ======================================================================
# 配置自定义词表
# ======================================================================


class TestCustomConfig:
    """自定义黑名单/短语词表测试。"""

    def test_custom_blacklist_hit(self):
        """自定义黑名单词命中。"""
        f = MessageFilter({"filter_blacklist": ["custom_word"]})
        assert f.should_filter("custom_word", "u1", 0.0) == (True, "A")

    def test_custom_blacklist_default_not_hit(self):
        """默认黑名单词不在自定义词表中 → 不命中 A。

        '打卡' 不在自定义 blacklist ['custom_word'] 中 → 不命中 A。
        """
        f = MessageFilter({"filter_blacklist": ["custom_word"]})
        filtered, rule = f.should_filter("打卡", "u1", 0.0)
        # 不命中 A（但可能命中 E，因 len=2 ≤ 2 无问号）
        assert rule != "A"

    def test_custom_meaningless_hit(self):
        """自定义无意义短语命中。"""
        f = MessageFilter({"filter_meaningless_phrases": ["blahblah"]})
        assert f.should_filter("blahblah", "u1", 0.0) == (True, "B")

    def test_custom_meaningless_default_not_hit(self):
        """默认短语不在自定义词表中 → 不命中 B。

        '啊啊啊' 不在自定义 phrases ['blahblah'] 中 → 不命中 B。
        """
        f = MessageFilter({"filter_meaningless_phrases": ["blahblah"]})
        filtered, rule = f.should_filter("啊啊啊", "u1", 0.0)
        assert rule != "B"

    def test_custom_short_msg_len(self):
        """自定义 short_msg_len 影响 E 规则。"""
        f = MessageFilter({"filter_short_msg_len": 5})
        # 长度 4 ≤ 5，不含 ? → 命中 E
        assert f.should_filter("abcd", "u1", 0.0) == (True, "E")
        # 长度 6 > 5 → 不命中 E
        filtered, rule = f.should_filter("abcdef", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_custom_burst_threshold(self):
        """自定义 burst_count=2 → 2 条相同即触发。"""
        f = MessageFilter({"filter_burst_count": 2, "filter_burst_window": 10})
        assert f.should_filter("spam", "u1", 0.0) == (False, "")
        filtered, rule = f.should_filter("spam", "u1", 1.0)
        assert filtered is True
        assert rule == "C"


# ======================================================================
# 规则顺序 A→B→C→D→E
# ======================================================================


class TestRuleOrder:
    """验证规则顺序：任一命中即返回不再继续。"""

    def test_blacklist_before_burst(self):
        """黑名单词连发 → 命中 A 而非 C（A 优先于 C）。"""
        f = MessageFilter()
        # '打卡' 在黑名单中，每次都命中 A，不会进入 C 的 deque
        assert f.should_filter("打卡", "u1", 0.0) == (True, "A")
        assert f.should_filter("打卡", "u1", 1.0) == (True, "A")
        assert f.should_filter("打卡", "u1", 2.0) == (True, "A")

    def test_meaningless_before_short(self):
        """无意义短语 '嗯嗯' 命中 B 而非 E（B 优先于 E）。"""
        f = MessageFilter()
        # '嗯嗯' 长度 2 ≤ 2 无问号，本可命中 E，但 B 先命中
        filtered, rule = f.should_filter("嗯嗯", "u1", 0.0)
        assert filtered is True
        assert rule == "B"

    def test_burst_before_symbol(self):
        """刷屏消息（同时满足 D）→ 前 2 次命中 D，第 3 次命中 C（C 优先于 D）。

        '12345' 是纯数字 → D 会命中。但前 2 次 D 命中时消息已入 C 的 deque，
        第 3 次 C count=3 >= 3 先命中，D 不再检查。
        """
        # 配置不含 '12345' 的黑名单，burst_count=3
        f = MessageFilter({"filter_blacklist": [], "filter_burst_count": 3})
        # '12345' 是纯数字 → D 会命中，但每次 C 都先检查并加入 deque
        assert f.should_filter("12345", "u1", 0.0) == (True, "D")
        assert f.should_filter("12345", "u1", 1.0) == (True, "D")
        # 第 3 条：C 先命中（count=3 >= 3），D 不再检查
        filtered, rule = f.should_filter("12345", "u1", 2.0)
        assert filtered is True
        assert rule == "C"

    def test_symbol_before_short(self):
        """单字符同时满足 D 和 E → 命中 D（D 优先于 E）。"""
        f = MessageFilter()
        # '哦' 长度 1 → D 命中（长度==1）
        filtered, rule = f.should_filter("哦", "u1", 0.0)
        assert filtered is True
        assert rule == "D"


# ======================================================================
# 边界与回归
# ======================================================================


class TestEdgeCases:
    """边界情况与回归测试。"""

    def test_empty_string_filtered_by_e(self):
        """空字符串 → 命中 E（长度 0 ≤ 2，不含问号）。"""
        f = MessageFilter()
        filtered, rule = f.should_filter("", "u1", 0.0)
        assert filtered is True
        assert rule == "E"

    def test_question_mark_fullwidth_exemption(self):
        """全角问号豁免 E 规则。"""
        f = MessageFilter()
        # '好？' 长度 2 ≤ 2，含 ？ → 不命中 E
        filtered, rule = f.should_filter("好？", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_halfwidth_question_exemption(self):
        """半角问号豁免 E 规则。"""
        f = MessageFilter()
        # 'ab?' 长度 3 > 2，但用 short_msg_len=3 测试
        f = MessageFilter({"filter_short_msg_len": 3})
        filtered, rule = f.should_filter("ab?", "u1", 0.0)
        assert filtered is False
        assert rule == ""

    def test_instance_reusable(self):
        """MessageFilter 实例可被多次调用。"""
        f = MessageFilter()
        # 多次调用不应出错
        for i in range(10):
            f.should_filter(f"msg{i}", "u1", float(i))
        # 验证状态正常
        assert f.should_filter("打卡", "u1", 100.0) == (True, "A")

    def test_default_config_values(self):
        """默认配置值正确。"""
        f = MessageFilter()
        assert f.enabled is True
        assert "打卡" in f.blacklist
        assert "啊啊啊" in f.meaningless_phrases
        assert f.short_msg_len == 2
        assert f.burst_count == 3
        assert f.burst_window == 10

    def test_burst_deque_maxlen(self):
        """deque maxlen=20，超过后旧记录被丢弃。"""
        f = MessageFilter({"filter_burst_count": 100, "filter_burst_window": 1000})
        # 发送 25 条不同消息，deque 应限长 20
        for i in range(25):
            f.should_filter(f"msg{i}", "u1", float(i))
        # deque 应只有 20 条
        assert len(f._burst_history["u1"]) == 20
