"""Ctrl+C 的语义:**打断当前这一轮,不等于退出整个程序。**

背景(实测报障):主线程卡在长阻塞调用里时,中断会挂起,等调用返回才落地 —— 而落点
常常正好在提示符上。原来"提示符下 Ctrl+C = 退出",于是用户只是想把刚才那一轮停下来,
整个程序却退了。这里把新的语义钉住:

    · **任何 Ctrl+C 都不退出程序**:提示符下只清空输入,按几次都一样;
    · 退出的入口只有 /exit 与真正的 EOF(Ctrl+D / Ctrl+Z);
    · 一轮执行中被打断 → 本轮收尾,程序继续;
    · 后台子 agent 叫醒的那条入口和用户入口**走同一个受保护函数**(原来那条是裸的)。
"""
from __future__ import annotations

import inspect

import pytest

from agent import cli


class _Take:
    """替代真实的 stdin 等待:按脚本返回一行,或抛 KeyboardInterrupt。"""

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, timeout):
        action = self.script.pop(0) if self.script else "\n"
        if action is KeyboardInterrupt:
            raise KeyboardInterrupt
        return action


# ---------------------------------------------------------------- 提示符

def test_ctrl_c_only_clears_the_line(monkeypatch):
    """只清空 + 提示,继续读 —— 不返回 None(那是退出信号)。"""
    monkeypatch.setattr(cli, "_take_line", _Take([KeyboardInterrupt, "\n"]))
    assert cli._read_multiline("> ") == ""


def test_ctrl_c_never_exits_no_matter_how_many_times(monkeypatch):
    """按几次都一样 —— 连按本来就是人着急时的习惯动作,不能拿次数当退出条件。"""
    monkeypatch.setattr(
        cli, "_take_line",
        _Take([KeyboardInterrupt, KeyboardInterrupt, KeyboardInterrupt, "hello\n"]),
    )
    assert cli._read_multiline("> ") == "hello"


def test_ctrl_c_clears_what_was_already_typed(monkeypatch):
    """被打断前的半截输入不提交(否则会悄悄发出去一段没写完的话)。"""
    monkeypatch.setattr(
        cli, "_take_line", _Take(["half typed\n", KeyboardInterrupt, "\n"]),
    )
    assert cli._read_multiline("> ") == ""


def test_a_real_eof_still_exits(monkeypatch):
    """退出入口不能被堵死:Ctrl+D / Ctrl+Z 的 EOF 仍然返回 None。"""
    monkeypatch.setattr(cli, "_take_line", _Take([None]))
    assert cli._read_multiline("> ") is None


# ---------------------------------------------------------------- stdin 读线程

class _FakeStdin:
    """按脚本返回行;脚本用完 = 真 EOF。"""

    def __init__(self, script):
        self.script = list(script)

    def readline(self):
        if not self.script:
            return ""
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _drain_input():
    while not cli._INPUT.empty():
        cli._INPUT.get_nowait()


def test_an_interrupted_console_read_is_not_treated_as_eof(monkeypatch):
    """**这就是"按 Ctrl+C 程序直接退了"的真凶。**

    Ctrl+C 会让控制台的那次读提前返回空串;如果把它当 EOF,读线程会推一个 None
    然后退出,主循环立刻走 `再见。break` —— 而且之后再按都没用(线程已经没了)。
    """
    import sys as _sys
    _drain_input()
    monkeypatch.setattr(_sys, "stdin", _FakeStdin(["", "hello\n"]))
    cli._stdin_reader_loop()                     # 脚本用完后才遇到真 EOF

    got = []
    while not cli._INPUT.empty():
        got.append(cli._INPUT.get_nowait())
    # 第一项必须是读到的那一行,不能是 None(假 EOF 被吞掉)
    assert got[0] == "hello\n"
    assert got[-1] is None                       # 真正的 EOF 仍然送到


def test_two_consecutive_empty_reads_are_a_real_eof(monkeypatch):
    import sys as _sys
    _drain_input()
    monkeypatch.setattr(_sys, "stdin", _FakeStdin([]))
    cli._stdin_reader_loop()
    assert cli._INPUT.get_nowait() is None


# ---------------------------------------------------------------- 一轮的执行

@pytest.fixture
def abort_spy(monkeypatch):
    """把收尾动作换成计数器:不碰真实会话存储,也不打印。"""
    seen = {"repair": 0, "render": 0}

    def repair(messages):
        seen["repair"] += 1
        return 0                     # 没有悬空的 tool_calls

    monkeypatch.setattr(cli.session, "repair_dangling", repair)
    monkeypatch.setattr(cli.session, "append_messages", lambda *a, **k: None)
    monkeypatch.setattr(cli, "current_session_id", lambda: "test-session")

    def render(reply):
        seen["render"] += 1

    monkeypatch.setattr(cli, "_render_reply", render)
    return seen


def test_an_interrupted_turn_is_aborted_not_fatal(monkeypatch, abort_spy):
    def boom(text, messages):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", boom)
    cli._run_turn([], "hi")          # 不抛出去 = 不会把整个会话掀翻
    assert abort_spy["repair"] == 1
    assert abort_spy["render"] == 0  # 被打断的那轮没有答复可渲染


def test_a_failed_turn_is_aborted_not_fatal(monkeypatch, abort_spy):
    def boom(text, messages):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run", boom)
    cli._run_turn([], "hi")
    assert abort_spy["repair"] == 1


def test_the_background_wakeup_turn_passes_none(monkeypatch, abort_spy):
    """后台通报那条路是 `run(None, …)`:不能凭空追加一条用户消息。"""
    seen = []
    monkeypatch.setattr(cli, "run", lambda text, messages: seen.append(text) or "")
    cli._run_turn([], None)
    assert seen == [None]


def test_both_turn_entries_go_through_the_protected_helper(monkeypatch):
    """**回归护栏**:两条入口都必须走 `_run_turn`,不许再出现裸的 run 调用。

    原来的 bug 就是"用户入口包了 try,后台叫醒入口忘了包" —— 那种遗漏在动态测试里
    很难触发(要真跑起整个 REPL),所以在这里直接盯住源码结构。
    """
    src = inspect.getsource(cli._session_loop)
    assert src.count("_run_turn(") == 2, "用户入口 + 后台叫醒入口,一个都不能少"
    assert "run(text, messages)" not in src
    assert "run(None, messages)" not in src
