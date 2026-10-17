"""Ctrl+C 的语义:**打断当前这一轮,不等于退出整个程序。**

背景(实测报障):主线程卡在长阻塞调用里时,中断会挂起,等调用返回才落地 —— 而落点
常常正好在提示符上。原来"提示符下 Ctrl+C = 退出",于是用户只是想把刚才那一轮停下来,
整个程序却退了。这里把新的语义钉住:

    · 提示符下第一次 Ctrl+C 只清空输入(并提示),第二次才退出;
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

def test_first_ctrl_c_only_clears_the_line(monkeypatch):
    """第一次只清空 + 提示,继续读 —— 不返回 None(那是退出信号)。"""
    monkeypatch.setattr(cli, "_take_line", _Take([KeyboardInterrupt, "\n"]))
    assert cli._read_multiline("> ") == ""


def test_second_consecutive_ctrl_c_exits(monkeypatch):
    monkeypatch.setattr(cli, "_take_line",
                        _Take([KeyboardInterrupt, KeyboardInterrupt]))
    assert cli._read_multiline("> ") is None


def test_typing_resets_the_double_press(monkeypatch):
    """中间输入过东西,下一次 Ctrl+C 就重新算"第一次",不能算连按。"""
    monkeypatch.setattr(
        cli, "_take_line",
        _Take([KeyboardInterrupt, "hello\n", KeyboardInterrupt, "\n"]),
    )
    # 第二次中断把已输入的内容清掉,然后正常提交 → "";关键是不是 None(没退出)
    assert cli._read_multiline("> ") == ""


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
