"""test_replay.py —— E 历史回放引擎。

测试对象：core/replay.py → ReplayEngine
覆盖点：
- parse_line：合法 JSON、非法 JSON、缺字段、空行、ts 浮点转换
- list_files：列文件、空目录
- run：基本回放、倍速缩放、stop_flag 中断、非法 speed、feed_fn 异常计入 skipped、文件不存在

对应 PRD §8.11（回放产生决策日志且零发送——零发送部分在 test_scheduler 验证）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.scheduler.replay import ReplayEngine, _check_stop


def _valid_line(ts: float = 1.0, group_id: str = "g1",
                user_id: str = "u1", nickname: str = "Alice",
                text: str = "hello") -> str:
    return json.dumps({
        "ts": ts, "group_id": group_id, "user_id": user_id,
        "nickname": nickname, "text": text,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------- #
# parse_line
# ---------------------------------------------------------------------- #

def test_replay_parse_line_valid():
    msg = ReplayEngine.parse_line(_valid_line(ts=1.5))
    assert msg is not None
    assert msg["ts"] == 1.5
    assert msg["group_id"] == "g1"
    assert msg["text"] == "hello"


def test_replay_parse_line_invalid_json():
    assert ReplayEngine.parse_line("not json") is None


def test_replay_parse_line_missing_field():
    """缺必要字段 → None。"""
    line = json.dumps({"ts": 1.0, "group_id": "g1"})  # 缺 user_id/nickname/text
    assert ReplayEngine.parse_line(line) is None


def test_replay_parse_line_none_field_value():
    """字段值为 None → None。"""
    line = json.dumps({"ts": 1.0, "group_id": None, "user_id": "u",
                       "nickname": "n", "text": "t"})
    assert ReplayEngine.parse_line(line) is None


def test_replay_parse_line_empty_string():
    assert ReplayEngine.parse_line("") is None


def test_replay_parse_line_whitespace_only():
    assert ReplayEngine.parse_line("   \n  ") is None


def test_replay_parse_line_ts_as_string():
    """ts 为字符串数字也能转浮点。"""
    line = json.dumps({"ts": "2.5", "group_id": "g", "user_id": "u",
                       "nickname": "n", "text": "t"})
    msg = ReplayEngine.parse_line(line)
    assert msg is not None
    assert msg["ts"] == 2.5


def test_replay_parse_line_ts_invalid():
    """ts 非数字 → None。"""
    line = json.dumps({"ts": "abc", "group_id": "g", "user_id": "u",
                       "nickname": "n", "text": "t"})
    assert ReplayEngine.parse_line(line) is None


def test_replay_parse_line_not_dict():
    """顶层非 dict（如数组）→ None。"""
    assert ReplayEngine.parse_line("[1,2,3]") is None


# ---------------------------------------------------------------------- #
# list_files
# ---------------------------------------------------------------------- #

def test_replay_list_files(tmp_data_dir):
    """列出 replay/ 下 .jsonl 文件（仅文件名）。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "a.jsonl").write_text("{}", encoding="utf-8")
    (replay_dir / "b.jsonl").write_text("{}", encoding="utf-8")
    (replay_dir / "c.txt").write_text("x", encoding="utf-8")  # 非jsonl
    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    files = eng.list_files()
    assert files == ["a.jsonl", "b.jsonl"]  # 排序


def test_replay_list_files_no_dir(tmp_data_dir):
    """目录不存在 → 空列表。"""
    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    assert eng.list_files() == []


# ---------------------------------------------------------------------- #
# run
# ---------------------------------------------------------------------- #

def test_replay_run_basic(tmp_data_dir):
    """基本回放：逐条 feed_fn，返回统计。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    lines = [_valid_line(ts=1.0), _valid_line(ts=2.0), _valid_line(ts=3.0)]
    path.write_text("\n".join(lines), encoding="utf-8")

    fed: list[dict] = []

    async def feed(msg):
        fed.append(msg)

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    stats = asyncio.run(eng.run(path, speed=100.0, feed_fn=feed, stop_flag=lambda: False))
    assert stats["total"] == 3
    assert stats["fed"] == 3
    assert stats["skipped"] == 0
    assert len(fed) == 3


def test_replay_run_skips_invalid_lines(tmp_data_dir):
    """非法行计入 skipped，不中断。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    path.write_text(
        _valid_line(ts=1.0) + "\n" + "bad line\n" + _valid_line(ts=2.0),
        encoding="utf-8",
    )
    fed: list[dict] = []

    async def feed(msg):
        fed.append(msg)

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    stats = asyncio.run(eng.run(path, speed=100.0, feed_fn=feed, stop_flag=lambda: False))
    assert stats["total"] == 3
    assert stats["fed"] == 2
    assert stats["skipped"] == 1


def test_replay_run_speed_scales_sleep(tmp_data_dir):
    """speed 越高 sleep 越短。验证 high speed 下快速完成。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    # ts 间隔 10 秒，speed=100 → sleep 0.1 秒/条
    lines = [_valid_line(ts=0.0), _valid_line(ts=10.0), _valid_line(ts=20.0)]
    path.write_text("\n".join(lines), encoding="utf-8")

    async def feed(msg):
        pass

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    import time
    start = time.monotonic()
    asyncio.run(eng.run(path, speed=100.0, feed_fn=feed, stop_flag=lambda: False))
    elapsed = time.monotonic() - start
    # 2 次 sleep × 0.1 秒 = 0.2 秒（首条不 sleep）
    assert elapsed < 1.0  # 远小于真实 20 秒


def test_replay_run_invalid_speed_defaults_to_1(tmp_data_dir):
    """speed<=0 当 1.0 处理。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    path.write_text(_valid_line(ts=1.0), encoding="utf-8")

    async def feed(msg):
        pass

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    stats = asyncio.run(eng.run(path, speed=0, feed_fn=feed, stop_flag=lambda: False))
    assert stats["fed"] == 1


def test_replay_run_stop_flag_interrupts(tmp_data_dir):
    """stop_flag 返回 True 中断回放。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    lines = [_valid_line(ts=1.0), _valid_line(ts=2.0), _valid_line(ts=3.0)]
    path.write_text("\n".join(lines), encoding="utf-8")

    fed: list[dict] = []
    call_count = [0]

    async def feed(msg):
        fed.append(msg)
        call_count[0] += 1

    # stop_flag 在第 1 条后返回 True
    def stop_flag():
        return call_count[0] >= 1

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    stats = asyncio.run(eng.run(path, speed=100.0, feed_fn=feed, stop_flag=stop_flag))
    # 至少喂入 1 条后中断
    assert stats["fed"] >= 1
    assert stats["fed"] < 3  # 未喂完全部


def test_replay_run_feed_exception_counts_skipped(tmp_data_dir):
    """feed_fn 抛异常计入 skipped，不中断。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    lines = [_valid_line(ts=1.0), _valid_line(ts=2.0), _valid_line(ts=3.0)]
    path.write_text("\n".join(lines), encoding="utf-8")

    call_count = [0]

    async def feed(msg):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("feed broken")

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    stats = asyncio.run(eng.run(path, speed=100.0, feed_fn=feed, stop_flag=lambda: False))
    assert stats["fed"] == 2
    assert stats["skipped"] == 1


def test_replay_run_missing_file(tmp_data_dir):
    """文件不存在 → 返回 {0,0,0}。"""
    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)

    async def feed(msg):
        pass

    path = tmp_data_dir / "nonexistent.jsonl"
    stats = asyncio.run(eng.run(path, speed=1.0, feed_fn=feed, stop_flag=lambda: False))
    assert stats == {"total": 0, "fed": 0, "skipped": 0}


def test_replay_run_first_message_no_sleep(tmp_data_dir):
    """首条不 sleep（无论 ts 多大）。"""
    replay_dir = tmp_data_dir / "replay"
    replay_dir.mkdir(parents=True)
    path = replay_dir / "test.jsonl"
    path.write_text(_valid_line(ts=1000.0), encoding="utf-8")

    async def feed(msg):
        pass

    eng = ReplayEngine(tmp_data_dir, lambda lv, m: None)
    import time
    start = time.monotonic()
    asyncio.run(eng.run(path, speed=1.0, feed_fn=feed, stop_flag=lambda: False))
    elapsed = time.monotonic() - start
    assert elapsed < 0.5  # 首条不 sleep


# ---------------------------------------------------------------------- #
# _check_stop
# ---------------------------------------------------------------------- #

def test_check_stop_callable():
    assert _check_stop(lambda: True) is True
    assert _check_stop(lambda: False) is False


def test_check_stop_object_with_is_set():
    class Flag:
        def __init__(self, v):
            self._v = v
        def is_set(self):
            return self._v
    assert _check_stop(Flag(True)) is True
    assert _check_stop(Flag(False)) is False


def test_check_stop_exception_returns_false():
    """异常视为未停止。"""
    def bad():
        raise RuntimeError()
    assert _check_stop(bad) is False
