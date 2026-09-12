from __future__ import annotations

from .core import (
    MODEL,
    MAX_CONTEXT_TOKENS,
    client,
    console,
)
from .tools.registry import TOOLS

# ---------------- 核心循环 ----------------

# 最近一次请求的 token 用量(来自 API 的 usage)。prompt_tokens 就是"当前上下文多大"。
_last_usage: dict = {}
# 本会话累计:请求数、总输出 token(输入每轮都要重发,累计没有意义,不计)
_total_usage: dict = {"requests": 0, "completion": 0}


def _record_usage(usage) -> None:
    """记录 API 返回的用量(流式要开 include_usage 才有)。"""
    if usage is None:
        return
    _last_usage["prompt"] = getattr(usage, "prompt_tokens", 0) or 0
    _last_usage["completion"] = getattr(usage, "completion_tokens", 0) or 0
    _last_usage["cache_hit"] = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    _total_usage["requests"] += 1
    _total_usage["completion"] += _last_usage["completion"]


def _context_ratio() -> float:
    """最近一次请求的上下文占用比例(0~1),用于判断要不要自动压缩。"""
    if MAX_CONTEXT_TOKENS <= 0:
        return 0.0
    return _last_usage.get("prompt", 0) / MAX_CONTEXT_TOKENS


def usage_line() -> str:
    """当前上下文用量的一行摘要(还没有数据时返回空串)。"""
    prompt = _last_usage.get("prompt")
    if not prompt:
        return ""
    pct = (prompt / MAX_CONTEXT_TOKENS * 100) if MAX_CONTEXT_TOKENS else 0.0
    text = (f"上下文 {prompt:,}/{MAX_CONTEXT_TOKENS:,} tokens({pct:.1f}%,"
            f"本轮输出 {_last_usage.get('completion', 0):,}")
    hit = _last_usage.get("cache_hit", 0)
    if hit:
        text += f",缓存命中 {hit:,}"
    return text + ")"


def usage_detail() -> str:
    """给 /tokens 用的较详细用量:当前上下文 + 本会话累计。"""
    if not _last_usage.get("prompt"):
        return "(还没有用量数据,先聊一句再看)"
    return (f"{usage_line()}\n"
            f"本会话:共 {_total_usage['requests']} 次请求,累计输出 {_total_usage['completion']:,} tokens")


def _stream_model(messages: list[dict]) -> tuple[str, list[dict], str]:
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
    return "".join(content_parts), tool_calls, "".join(reason_parts)


_COMPACT_INSTRUCTION = (
    "请把以上我们这次对话压缩成一份紧凑的要点摘要,供之后继续对话使用。"
    "务必保留:我的偏好与明确要求、已做出的决定与结论、涉及的文件路径与对文件的改动、"
    "尚未完成的任务/待办、重要的具体数据;可以省略寒暄、重复的试探和被推翻的中间步骤。"
    "用简体中文直接输出摘要本身,不要加任何评论或前后缀,也不要调用任何工具。"
)
