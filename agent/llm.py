from __future__ import annotations

from rich.markdown import Markdown

from . import ctx
from .core import (
    MODEL,
    MAX_CONTEXT_TOKENS,
    client,
    console,
)
from .tools.registry import TOOLS

# ---------------- 核心循环 ----------------

# 用量**记在当前 agent 的上下文里**(见 ctx.py),不再是进程级的一份 ——
# 否则子 agent 和主 agent 的账会混在一起,连"该压谁的上下文"都判不出来。


def _record_usage(usage) -> None:
    """记录 API 返回的用量(流式要开 include_usage 才有)。记在**发这次请求的** agent 账上。"""
    if usage is None:
        return
    c = ctx.current()
    last, total = c.last_usage, c.total_usage
    # prompt_tokens 就是"这个 agent 现在的上下文有多大"
    last["prompt"] = getattr(usage, "prompt_tokens", 0) or 0
    last["completion"] = getattr(usage, "completion_tokens", 0) or 0
    last["cache_hit"] = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    total["requests"] += 1
    total["completion"] += last["completion"]
    total["prompt"] += last["prompt"]
    total["cache_hit"] += last["cache_hit"]


def context_ratio() -> float:
    """**当前 agent** 最近一次请求的上下文占用比例(0~1),用于判断要不要自动压缩。

    每个 agent 要压的是自己的上下文,所以这个值必须跟着上下文走,不能是全局的一份。
    """
    if MAX_CONTEXT_TOKENS <= 0:
        return 0.0
    return ctx.current().last_usage.get("prompt", 0) / MAX_CONTEXT_TOKENS


def _cache_rate(prompt: int, hit: int) -> float:
    """缓存命中率(0~100)。命中数来自 API 的 prompt_cache_hit_tokens。"""
    return (hit / prompt * 100) if prompt else 0.0


def begin_turn() -> None:
    """记下本轮起点(在 run() 开头调一次),之后 _turn_usage() 就能给出这一轮的真实消耗。

    为什么不能只看最后一次请求:一轮里每调一次工具就多发一次请求,每次都要重发整个
    历史。用户问一句话触发了三次工具调用,那是四次请求 —— 只看最后那次,消耗被少算成
    四分之一,而且上下文越大、工具越多,少算得越离谱。
    """
    c = ctx.current()
    c.turn_start = dict(c.total_usage)


def _turn_usage() -> dict:
    """**当前 agent** 本轮至今的消耗(字段与 total_usage 相同)。"""
    c = ctx.current()
    return {k: c.total_usage.get(k, 0) - c.turn_start.get(k, 0) for k in c.total_usage}


def usage_line() -> str:
    """每轮结尾的一行摘要:当前上下文大小 + **本轮**消耗(还没有数据时返回空串)。

    两个口径刻意分开:
    - **上下文**:取最后一次请求的 prompt —— 那才是"现在的历史有多长",报本轮之和没意义
    - **其余**:一律报本轮 —— 缓存命中率、输出 token 都是"这一轮整体如何",只报最后一次
      会把中间几次工具调用白算进去的那部分漏掉
    """
    used = ctx.current().last_usage.get("prompt")
    if not used:
        return ""
    t = _turn_usage()
    pct = (used / MAX_CONTEXT_TOKENS * 100) if MAX_CONTEXT_TOKENS else 0.0
    return (f"上下文 {used:,}/{MAX_CONTEXT_TOKENS:,}({pct:.1f}%)"
            f" · 本轮 {t['requests']} 次请求"
            f" · 缓存命中 {_cache_rate(t['prompt'], t['cache_hit']):.1f}%"
            f" · 输出 {t['completion']:,} tokens")


def usage_detail() -> str:
    """给 /tokens 用的用量:当前上下文 + **本轮**消耗 + 本会话累计。

    分三段是因为口径不同,混在一起最容易误读 —— 上下文是"现在有多大",本轮是"这句话
    花了多少",累计是"这个会话一共花了多少"。只报最后那次请求是没意义的:一轮里调了几次
    工具就有几次请求,每次都重发了整个历史。
    """
    c = ctx.current()
    if not c.last_usage.get("prompt"):
        return "(还没有用量数据)"
    used = c.last_usage["prompt"]
    pct = (used / MAX_CONTEXT_TOKENS * 100) if MAX_CONTEXT_TOKENS else 0.0
    t = _turn_usage()
    tp, th, tc, tr = t["prompt"], t["cache_hit"], t["completion"], t["requests"]
    ap, ah, ac, ar = (c.total_usage["prompt"], c.total_usage["cache_hit"],
                      c.total_usage["completion"], c.total_usage["requests"])
    return (
        f"上下文 {used:,}/{MAX_CONTEXT_TOKENS:,} tokens({pct:.1f}%)\n"
        f"本轮:{tr} 次请求,输出 {tc:,} tokens,缓存命中 "
        f"{_cache_rate(tp, th):.1f}%({th:,}/{tp:,})\n"
        f"本会话:{ar} 次请求,累计输出 {ac:,} tokens,整体缓存命中 "
        f"{_cache_rate(ap, ah):.1f}%({ah:,}/{ap:,})"
    )


def stream_model(messages: list[dict]) -> tuple[str, list[dict], str]:
    """流式调用模型:实时显示思考(reasoning_content),返回 (正文, tool_calls, 思考文本)。

    - 模型有思考就逐字显示(暗色斜体),没有就自然跳过 —— 自适应,无需开关。
    - 思考文本会**随 assistant 消息回传**(见 run())。DeepSeek V4 规则:请求携带 tools 时,
      历史所有轮的 reasoning_content 必须完整回传,否则 API 返回 400;
      (旧模型如 deepseek-reasoner 规则相反、不该回传 —— 将来若换非 DeepSeek 提供方需注意区分。)
    - 正文不做流式渲染(攒完整段后交给调用方 Markdown 渲染),只有思考是实时刷出的。
    - tool_calls 在流式下是分片到达的,按 index 合并 id / name / arguments。
    """
    content_parts: list[str] = []
    reason_parts: list[str] = []
    tool_slots: dict[int, dict] = {}   # index -> {"id","type","function":{"name","arguments"}}
    reasoning_open = False             # 思考区已开始且尚未闭合(还没换行)

    def _close_reasoning() -> None:
        nonlocal reasoning_open
        if reasoning_open:
            console.print()            # 思考结束,换行
            reasoning_open = False

    stream = client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, stream=True,
        stream_options={"include_usage": True},  # 让末尾那一帧带上 token 用量,供上下文统计
    )
    for chunk in stream:
        _record_usage(getattr(chunk, "usage", None))
        if not getattr(chunk, "choices", None):  # 纯用量帧等没有 choices 的 chunk
            continue
        delta = chunk.choices[0].delta
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reason_parts.append(rc)
            if not reasoning_open:
                console.print("* 思考", style="dim", markup=False)
                reasoning_open = True
            # 思考文本可能含 [ ] 之类的字符,关掉 markup/highlight,原样输出
            console.print(rc, style="dim italic", end="", markup=False,
                          highlight=False, soft_wrap=True)
        if delta.content:
            _close_reasoning()
            content_parts.append(delta.content)
        for tcd in (delta.tool_calls or []):
            idx = tcd.index
            if idx is None:
                # 提供方没给 index 时不能一律并到 0 —— 那会把并行的多个工具调用
                # 合成一个。改用"带 id 视为新的一次调用、否则续写最后一个"来定位。
                if getattr(tcd, "id", None):
                    idx = len(tool_slots)
                elif tool_slots:
                    idx = max(tool_slots)
                else:
                    idx = 0
            slot = tool_slots.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if getattr(tcd, "id", None):
                slot["id"] = tcd.id
            fn = getattr(tcd, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["function"]["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["function"]["arguments"] += fn.arguments
    _close_reasoning()

    tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
    content = "".join(content_parts)

    # 这一轮**还要继续调工具**时,它说的那几句话也当场打出来 —— 通常是"我先看看这个文件"
    # 这类过程说明。原来这些话是隐形的:run() 只在最后一轮 return content,中间轮只把
    # content 存进 messages,而 cli 只渲染最终 reply —— 于是用户只能靠事后重放才看得到。
    # 既然就在上下文里,没道理不显示。(最后一轮不打,那个由 cli 用它自己的样式渲染。)
    if tool_calls and content.strip():
        console.print("AI >", style="bold green", markup=False)
        console.print(Markdown(content.strip()))

    return content, tool_calls, "".join(reason_parts)

