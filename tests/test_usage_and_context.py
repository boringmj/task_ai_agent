"""账目与上下文:token 怎么算、上下文多满、流式的分片怎么拼回来。

**为什么要测这些**:它们全是**静默出错**的那一类。

  · 账算少了,用户看到的数字是假的,而没人会去核对;
  · "上下文多大"算错了,自动压缩就要么不触发(**直接把下一次请求撑爆**)、要么过早触发
    (白花一次压缩的钱);
  · 流式的 tool_calls 是**分片到达**的,拼错一个字段,工具收到的是半截 JSON ——
    而它只会表现为"工具有时候报参数错误",离真正的原因很远。

这一组里除了拼接那几条,全都不碰网络(用量是喂进去的)。
"""
from __future__ import annotations

import io
from types import SimpleNamespace as NS

import pytest
from agent import llm, loop
from agent.ctx import AgentCtx, use
from rich.console import Console

# ============================== 账目 ==============================


def _usage(prompt=1000, completion=50, hit=900):
    return NS(prompt_tokens=prompt, completion_tokens=completion,
              prompt_cache_hit_tokens=hit)


def test_the_bill_lands_on_the_agent_that_asked():
    """子 agent 花的钱记在**它自己**账上 —— 混在一起连"该压谁的上下文"都判不出来。"""
    main, sub = AgentCtx(role="main"), AgentCtx(role="sub", task_id="t1")
    with use(main):
        llm._record_usage(_usage(prompt=100))
    with use(sub):
        llm._record_usage(_usage(prompt=900))
        llm._record_usage(_usage(prompt=1200))

    assert main.total_usage["prompt"] == 100 and main.total_usage["requests"] == 1
    assert sub.total_usage["prompt"] == 2100 and sub.total_usage["requests"] == 2
    assert sub.last_usage["prompt"] == 1200, "last 只留最近一次(上下文多大就看它)"


def test_a_turn_counts_every_request_not_just_the_last():
    """一轮里每调一次工具就多发一次请求,每次都重发整个历史。

    只看最后那次,消耗被少算成几分之一 —— 而且上下文越大、工具越多,少算得越离谱。
    """
    c = AgentCtx(role="main")
    with use(c):
        llm._record_usage(_usage(prompt=1000, completion=10))       # 上一轮留下的
        llm.begin_turn()
        for _ in range(3):
            llm._record_usage(_usage(prompt=1000, completion=10))

    turn = c.total_usage["requests"] - c.turn_start["requests"]
    assert turn == 3
    assert c.total_usage["requests"] == 4, "累计照旧包含之前那些"


def test_the_usage_line_reports_the_context_and_the_turn():
    c = AgentCtx(role="main")
    with use(c):
        assert llm.usage_line() == "", "一次请求都还没发,不该硬凑一行出来"
        llm.begin_turn()
        llm._record_usage(_usage(prompt=1000, completion=50, hit=900))
        line = llm.usage_line()

    assert "上下文 1,000" in line, "上下文取最近一次请求的 prompt"
    assert "本轮 1 次请求" in line
    assert "90.0%" in line, f"缓存命中率算错了:{line}"
    assert "输出 50 tokens" in line


def test_usage_detail_gives_three_viewpoints():
    """/tokens 的三段:上下文是"现在多大",本轮是"这句话花了多少",累计是"这个会话一共"。"""
    c = AgentCtx(role="main")
    with use(c):
        assert llm.usage_detail() == "(还没有用量数据)"
        llm._record_usage(_usage(prompt=500, completion=20, hit=100))
        llm.begin_turn()
        llm._record_usage(_usage(prompt=800, completion=30, hit=700))
        out = llm.usage_detail()

    assert "上下文 800" in out
    assert "本轮:1 次请求" in out
    assert "本会话:2 次请求" in out, "累计那段报的该是总额,不是最近一次"


def test_context_ratio_is_what_triggers_compaction(monkeypatch):
    """这个值算错的代价是**静默**的:小了不压缩,下一次请求直接超限;大了白花钱。"""
    monkeypatch.setattr(llm, "MAX_CONTEXT_TOKENS", 1000)
    c = AgentCtx(role="main")
    with use(c):
        assert llm.context_ratio() == 0.0
        llm._record_usage(_usage(prompt=900))
        assert llm.context_ratio() == pytest.approx(0.9)
    monkeypatch.setattr(llm, "MAX_CONTEXT_TOKENS", 0)
    with use(c):
        assert llm.context_ratio() == 0.0, "上限配成 0 时不能除零"


def test_the_cache_rate_survives_a_zero_prompt():
    assert llm._cache_rate(0, 0) == 0.0
    assert llm._cache_rate(200, 50) == 25.0


# ============================== 流式:分片拼回来 ==============================


class _FakeStream:
    """假的那次 HTTP 调用:给它一串 chunk,它就是个可迭代的流。"""

    def __init__(self, frames):
        self._frames = frames
        self.seen = None

    def create(self, **kwargs):
        self.seen = kwargs
        return iter(self._frames)


def _client(frames):
    stream = _FakeStream(frames)
    return NS(chat=NS(completions=stream)), stream


def _chunk(content=None, reasoning=None, tool_calls=None, usage=None, no_choices=False):
    choices = [] if no_choices else [
        NS(delta=NS(reasoning_content=reasoning, content=content, tool_calls=tool_calls))]
    return NS(usage=usage, choices=choices)


def _call(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


@pytest.fixture
def spoken():
    """接住流式输出的缓冲区(它中间说的话打在这儿)。

    **不能去改 `ctx._DEFAULT`** —— ContextVar 的默认值是创建时绑定的,改模块属性没用
    (实测:那儿打出来的东西照样进了真终端)。
    """
    return io.StringIO()


def _stream(frames, monkeypatch, buf):
    """跑一遍 stream_model:假 client + 把当前 agent 的输出口引到 buf。"""
    fake, stream = _client(frames)
    monkeypatch.setattr(llm, "client", fake)
    with use(AgentCtx(role="main", console=Console(file=buf, width=110))):
        out = llm.stream_model([])
    return out, stream


def test_the_stream_is_stitched_back_into_one_call(monkeypatch, spoken):
    """名字一片、参数三片 —— 拼不回一个完整调用的话,工具收到的是半截 JSON。"""
    (content, calls, reasoning), stream = _stream([
        _chunk(reasoning="我先看"),
        _chunk(reasoning="一下。"),
        _chunk(content="看完了。"),
        _chunk(tool_calls=[_call(0, id="c1", name="read_file", args='{"path":')]),
        _chunk(tool_calls=[_call(0, args='"a.txt"}')]),
        _chunk(usage=_usage(), no_choices=True),          # 末尾那一帧只有用量
    ], monkeypatch, spoken)

    assert content == "看完了。"
    assert reasoning == "我先看一下。"
    assert calls == [{"id": "c1", "type": "function",
                      "function": {"name": "read_file",
                                   "arguments": '{"path":"a.txt"}'}}]
    assert stream.seen["stream"] is True
    assert stream.seen["stream_options"] == {"include_usage": True}, \
        "不开这个就没有用量帧,账目全是 0"


def test_two_parallel_calls_do_not_get_merged_into_one(monkeypatch, spoken):
    (_content, calls, _r), _ = _stream([
        _chunk(tool_calls=[_call(0, id="c1", name="read_file", args='{"path":"a"}')]),
        _chunk(tool_calls=[_call(1, id="c2", name="write_file", args='{"path":"b"}')]),
    ], monkeypatch, spoken)

    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert [c["function"]["name"] for c in calls] == ["read_file", "write_file"]


def test_without_an_index_a_new_id_starts_a_new_call(monkeypatch, spoken):
    """提供方不给 index 时**不能一律并到 0** —— 那会把并行的多个调用合成一个。"""
    (_content, calls, _r), _ = _stream([
        _chunk(tool_calls=[_call(None, id="c1", name="read_file", args='{"path":"a"}')]),
        _chunk(tool_calls=[_call(None, id="c2", name="write_file", args='{"path":"b"}')]),
    ], monkeypatch, spoken)

    assert len(calls) == 2, f"被并成一个了:{calls}"
    assert [c["function"]["name"] for c in calls] == ["read_file", "write_file"]


def test_without_an_index_a_plain_delta_continues_the_last_call(monkeypatch, spoken):
    (_content, calls, _r), _ = _stream([
        _chunk(tool_calls=[_call(None, id="c1", name="read_file", args='{"pa')]),
        _chunk(tool_calls=[_call(None, args='th":"a"}')]),
    ], monkeypatch, spoken)

    assert len(calls) == 1
    assert calls[0]["function"]["arguments"] == '{"path":"a"}'


def test_the_remarks_between_tool_calls_are_shown(monkeypatch, spoken):
    """它中间说的话("我先看看这个文件")原来是**隐形**的 —— 只存进历史,不打出来。

    只在**还要继续调工具**时打:最后那一轮由 cli 用它自己的样式渲染,这里再打就重复了。
    """
    _stream([_chunk(content="我先看看。",
                    tool_calls=[_call(0, id="c1", name="x", args="{}")])],
            monkeypatch, spoken)
    assert "AI >" in spoken.getvalue() and "我先看看。" in spoken.getvalue()

    spoken.truncate(0), spoken.seek(0)
    _stream([_chunk(content="就这样,完了。")], monkeypatch, spoken)
    assert spoken.getvalue() == "", "最后一轮不打(交给 cli),不然屏幕上会出现两遍"


def test_the_reasoning_is_carried_back_with_the_history(model):
    """**漏了它,下一次带 tools 的请求会被 API 直接拒(400)。**

    DeepSeek 的规矩:请求带 tools 时,历史里每一轮的 reasoning_content 都要完整回传。
    这是"能跑"和"跑两步就报错"的区别,而且报错信息离原因很远。
    """
    model.set(("看一下。", [model.call("get_current_time")], "我得先看看"),
              ("好了。", [], "想完了"))
    messages: list[dict] = [{"role": "system", "content": "x"}]

    loop.run("干点活", messages, max_steps=3)

    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert [m.get("reasoning_content") for m in assistant] == ["我得先看看", "想完了"]
