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

class _Stop(BaseException):
    """让读线程停下来(loop 只 catch Exception,BaseException 会穿出去)。"""


class _FakeStdin:
    """按脚本返回行。脚本用完时:`stop=True` 抛 _Stop(控制台场景,循环不该结束),
    否则返回空串(管道场景 = 真 EOF)。"""

    def __init__(self, script, tty=False, stop=True):
        self.script = list(script)
        self.tty = tty
        self.stop = stop

    def isatty(self):
        return self.tty

    def readline(self):
        if not self.script:
            if self.stop:
                raise _Stop
            return ""
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _drain_input():
    while not cli._INPUT.empty():
        cli._INPUT.get_nowait()


def test_a_console_empty_read_is_not_eof(monkeypatch):
    """**"按 Ctrl+C 程序直接退了"的真凶。**

    Ctrl+C 会让控制台的那次读返回空串,而且**后续的读也会继续返回空串**。
    控制台还开着,所以这不能当 EOF:重开 CONIN$ 继续读,绝不能推 None。
    """
    import sys as _sys
    _drain_input()
    monkeypatch.setattr(_sys, "stdin", _FakeStdin(["", "hello\n"], tty=True))
    reopened = {"n": 0}

    def fake_reopen():
        reopened["n"] += 1
        return True

    monkeypatch.setattr(cli, "_reopen_console_stdin", fake_reopen)
    try:
        cli._stdin_reader_loop()
    except _Stop:
        pass                                     # 脚本读完,正常收工

    got = []
    while not cli._INPUT.empty():
        got.append(cli._INPUT.get_nowait())
    assert got == ["hello\n"], "空读被当成 EOF 了"
    assert reopened["n"] >= 1

def test_a_pipe_empty_read_is_a_real_eof(monkeypatch):
    """非控制台(管道 / 重定向 / 文件)的空读才是真 EOF —— 脚本输入要靠它收尾。"""
    import sys as _sys
    _drain_input()
    monkeypatch.setattr(_sys, "stdin", _FakeStdin([], tty=False, stop=False))
    cli._stdin_reader_loop()
    assert cli._INPUT.get_nowait() is None


# ---------------------------------------------------------------- 一轮的执行

@pytest.fixture
def abort_spy(monkeypatch):
    """把收尾动作换成计数器:不碰真实会话存储,也不打印。"""
    seen = {"repair": 0, "render": 0}

    def repair(messages, note=""):
        seen["repair"] += 1
        seen["note"] = note
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
