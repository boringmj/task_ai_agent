from __future__ import annotations

from .core import (
    AUTO_COMPACT_RATIO,
    MAX_STEPS,
    MODEL,
    PROMPT_FILE,
    client,
    console,
    _pending_images,
)
from .llm import _stream_model, _context_ratio, _COMPACT_INSTRUCTION
from .tools.media import _inject_pending_images
from .tools.net import reset_turn_searches
from .tools.registry import TOOLS, dispatch

def compact(messages: list[dict], keep_recent: int = 0) -> str:
    """把较早的对话压成一段摘要、替换掉那段历史 —— 释放上下文。

    省 token 的关键:压缩请求**直接在原对话后面追一条指令**(而不是另起一个新请求),
    并带上同一套 tools —— 这样请求前缀与刚才的对话一致,能命中 DeepSeek 的前缀缓存;
    否则整段历史都要当新输入重新计费(实测:带 tools 命中 6912 tokens,不带则 0)。
    keep_recent > 0 时尾部保留若干条消息不动(自动压缩用,免得把刚拿到的工具结果压掉)。
    """
    head_start = 0
    while head_start < len(messages) and messages[head_start].get("role") == "system":
        head_start += 1                      # 开头的 system prompt 与长期记忆始终保留
    cut = len(messages)
    if keep_recent > 0:
        cut = max(head_start, len(messages) - keep_recent)
        while cut > head_start and messages[cut].get("role") != "user":
            cut -= 1                         # 往前挪到 user 消息,别把 assistant/tool 配对拆开
    head, tail = messages[head_start:cut], messages[cut:]
    if len(head) < 2:
        return "对话还很短,不需要压缩。"

    req = head + [{"role": "user", "content": _COMPACT_INSTRUCTION}]
    try:
        resp = client.chat.completions.create(model=MODEL, messages=req, tools=TOOLS)
        summary = (resp.choices[0].message.content or "").strip()
        if not summary:                      # 模型跑去调工具了 —— 退一步,不带 tools 再试一次
            resp = client.chat.completions.create(model=MODEL, messages=req)
            summary = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"压缩失败:{type(exc).__name__}: {exc}"
    if not summary:
        return "压缩失败:模型没有返回摘要。"

    messages[head_start:] = [{
        "role": "user",
        "content": f"(以上对话已压缩以节省上下文。以下是此前对话的摘要,请据此继续:\n{summary})",
    }] + tail
    return f"已压缩上下文:{len(head)} 条消息 → 1 条摘要({len(summary)} 字)"


def run(user_input: str, messages: list[dict]) -> str:
    reset_turn_searches()  # 搜索配额按轮重置,而不是整个会话共用一份
    _pending_images.clear()  # 图像也按轮清空,避免上一轮的图带到下一轮

    messages.append({"role": "user", "content": user_input})

    auto_compressed = False
    for _ in range(MAX_STEPS):
        # 上下文快满了就先压缩(工具调用过程中也照做),免得下一次请求超限;每轮最多压一次
        if not auto_compressed and _context_ratio() >= AUTO_COMPACT_RATIO:
            auto_compressed = True
            console.print(f"[自动压缩上下文] {compact(messages, keep_recent=2)}", style="dim")
        try:
            content, tool_calls, reasoning = _stream_model(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/流中断,给提示而不是崩掉
            return f"错误:调用模型失败({type(exc).__name__}: {exc})"

        # 把模型这一轮的回复放回对话历史,messages 就是 agent 的全部记忆。
        # reasoning_content 必须一并带着:DeepSeek V4 在带 tools 的请求里要求历史轮
        # 完整回传思维链,漏了会 400 —— 即使该轮没有实际调用工具也一样。
        assistant_msg: dict = {
            "role": "assistant",
            "content": content,
            "reasoning_content": reasoning,
        }
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # 模型不再要求调用工具 —— 说明它已经能回答了,循环结束
        if not tool_calls:
            return content

        for call in tool_calls:
            fname = call["function"]["name"]
            fargs = call["function"]["arguments"]
            # markup=False:工具参数里的 [ ] 不该被 rich 当成样式标记解析
            # 打印只是一行提示,渲染失败(如老终端编码不支持某个字符)不该中断整个回合,
            # 更不能让这一步之后的历史缺 tool 结果 —— 那会直接让下一次请求 400。
            try:
                console.print(
                    f"• {fname}({fargs})",
                    style="dim",
                    markup=False,
                    highlight=False,
                )
            except Exception:  # noqa: BLE001
                pass
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": dispatch(fname, fargs),
                }
            )

        # 这一轮若调用了 img,把登记好的图片作为 image_url 注入,给下一轮模型看
        _inject_pending_images(messages)

    return f"(已达到最大步数 {MAX_STEPS},中止)"


def load_system_prompt() -> str:
    """从 system_prompt.md 读取系统提示词。改提示词只需要编辑那个文件。

    会把占位符替换成代码里的实际值,免得提示词和代码对不上:
    - `{max_steps}`   → MAX_STEPS
    - `{commands}`    → agent/commands/ 里注册的终端指令清单(加指令不用再改提示词)
    """
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SystemExit(f"找不到系统提示词文件:{PROMPT_FILE}") from None
    from .commands import prompt_section      # 延迟导入:commands 里可能反向用到 loop
    return (text.replace("{max_steps}", str(MAX_STEPS))
                .replace("{commands}", prompt_section()))
