"""每个模型请求都要带 x-opencode-session(OpenCode Go 的路由/缓存凭证)。

**为什么值得单独测**:这玩意儿漏了是**硬错**——服务端直接 400,而不是变慢或者降级;
可它又只在新加一个请求调用点时漏(现有的三处都带了)。用假 client 抓参数,零成本。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from agent import llm, loop, session
from agent.ctx import AgentCtx, use


class _Recorder:
    """假 client:记下每次请求的 kwargs,不碰网络。"""

    def __init__(self, reply: str = ""):
        self.calls: list[dict] = []
        self.reply = reply
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return []                       # 流式:空迭代即可,拼装逻辑不需要数据
        return NS(choices=[NS(message=NS(content=self.reply))])


def test_stream_request_carries_current_session(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(llm, "client", rec)

    llm.stream_model([{"role": "user", "content": "hi"}])

    sid = session.current_session_id()
    assert rec.calls[0]["extra_headers"] == {"x-opencode-session": sid}


def test_sub_agent_gets_its_own_session_id(monkeypatch):
    """子 agent 是另一段独立对话 —— 共用一个 id 会让路由和缓存互相干扰。"""
    main = _Recorder()
    monkeypatch.setattr(llm, "client", main)
    llm.stream_model([{"role": "user", "content": "hi"}])
    main_sid = main.calls[0]["extra_headers"]["x-opencode-session"]

    sub = _Recorder()
    monkeypatch.setattr(llm, "client", sub)
    with use(AgentCtx(role="sub", task_id="t7")):
        llm.stream_model([{"role": "user", "content": "hi"}])
    sub_sid = sub.calls[0]["extra_headers"]["x-opencode-session"]

    assert sub_sid == f"{session.current_session_id()}-t7"
    assert sub_sid != main_sid


def test_compact_requests_carry_session_too(monkeypatch):
    rec = _Recorder(reply="摘要")
    monkeypatch.setattr(loop, "client", rec)

    loop.compact([
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ])

    assert rec.calls, "压缩应当发出一次请求"
    assert rec.calls[0]["extra_headers"] == llm.session_headers()
