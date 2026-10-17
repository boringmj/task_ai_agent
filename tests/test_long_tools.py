"""长任务(容器命令)的非阻塞执行与取消。

要点:
  · 阻塞的工具放工作线程,主线程轮询 —— 等待期间 Ctrl+C 才能落地;
  · **ContextVar 必须复制过去**,否则权限/身份在工具里会变成默认值;
  · 中断时调用工具挂的取消钩子(容器 = docker kill),然后把中断重新抛出;
  · 取消说明要进 tool 结果(模型要在下一轮看得到),不能只打在终端上。
"""
from __future__ import annotations

import _thread
import threading
import time

import pytest

from agent import ctx
from agent.tools import registry as reg


def test_a_long_tool_runs_in_the_agents_context(monkeypatch):
    """ContextVar 不会跟着新线程走 —— 不复制上下文,子 agent 的权限就丢了。"""
    seen = {}

    def fake_dispatch(name, arguments):
        seen["role"] = ctx.current().role
        seen["task"] = ctx.current().task_id
        return "ok"

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    with ctx.use(ctx.AgentCtx(role="sub", task_id="t1")):
        assert reg.dispatch_interruptibly("x", "{}") == "ok"
    assert seen == {"role": "sub", "task": "t1"}


def test_interrupt_calls_the_cancel_hook_and_reraises(monkeypatch):
    started = threading.Event()
    calls = []

    def slow_dispatch(name, arguments):
        started.set()
        time.sleep(10)
        return "late"

    monkeypatch.setattr(reg, "dispatch", slow_dispatch)
    me = ctx.AgentCtx(role="main")
    me.cancel = lambda: calls.append("cancel")

    def fire():
        started.wait(2)
        time.sleep(0.3)
        _thread.interrupt_main()      # 打到测试主线程 —— 它正在轮询等待

    threading.Thread(target=fire, daemon=True).start()
    with ctx.use(me):
        with pytest.raises(KeyboardInterrupt):
            reg.dispatch_interruptibly("x", "{}")
    assert calls == ["cancel"]


def test_repair_dangling_uses_the_specific_cancel_note():
    from agent import session
    msgs = [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "x", "arguments": "{}"}}],
    }]
    n = session.repair_dangling(msgs, note="（已取消:容器已终止。)")
    assert n == 1
    assert msgs[-1]["content"] == "（已取消:容器已终止。)"


def test_abort_turn_writes_the_cancel_note_and_clears_it(monkeypatch):
    from agent import cli
    msgs = [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "x", "arguments": "{}"}}],
    }]
    monkeypatch.setattr(cli.session, "append_messages", lambda *a, **k: None)
    monkeypatch.setattr(cli, "current_session_id", lambda: "test-session")

    me = ctx.AgentCtx(role="main")
    me.cancel_note = "（已取消:容器已终止,这条命令没有结果。)"
    with ctx.use(me):
        cli._abort_turn(msgs)

    assert msgs[-1]["content"] == "（已取消:容器已终止,这条命令没有结果。)"
    assert me.cancel_note == "", "说明用过就该清掉,别漏到下一次"


def test_container_tools_are_marked_long_running_with_a_timeout_param():
    assert reg.is_long_running("run_command")
    assert reg.is_long_running("run_python")
    assert not reg.is_long_running("read_file"), "快工具不该每个都起线程"

    for name in ("run_command", "run_python"):
        fn = [t for t in reg.TOOLS if t["function"]["name"] == name][0]["function"]
        assert "timeout" in fn["parameters"]["properties"]
