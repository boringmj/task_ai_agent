from __future__ import annotations

from . import ctx, prompts
from .core import (
    AUTO_COMPACT_RATIO,
    MAX_STEPS,
    MODEL,
    client,
    console,
)
from .llm import stream_model, context_ratio, begin_turn
from .tools.media import inject_pending_images
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
        return "对话过短, 无法压缩。"

    req = head + [{"role": "user", "content": prompts.load("compact_instruction")}]
    try:
        resp = client.chat.completions.create(model=MODEL, messages=req, tools=TOOLS)
        summary = (resp.choices[0].message.content or "").strip()
        if not summary:                      # 模型跑去调工具了 —— 退一步,不带 tools 再试一次
            resp = client.chat.completions.create(model=MODEL, messages=req)
            summary = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"压缩失败:{type(exc).__name__}: {exc}"
    if not summary:
        return "压缩失败: 模型没有返回摘要。"

    messages[head_start:] = [{
        "role": "user",
        "content": prompts.load("compact_summary", summary=summary),
    }] + tail
    return f"已压缩上下文: {len(head)} 条消息 → 1 条摘要({len(summary)} 字)"


def run(user_input: str, messages: list[dict], max_steps: int | None = None) -> str:
    """跑一轮:把 user_input 加进对话,循环"模型 → 工具 → 模型"直到它不再要工具。

    主 agent 和子 agent 用的是**同一个循环** —— 差别只在步数上限和各自的上下文
    (子 agent 的上下文是它自己那份,见 ctx.py)。同一套逻辑跑两种角色,才不会出现
    "主 agent 会做的事子 agent 不会"这种莫名其妙的不一致。
    """
    steps = max_steps or MAX_STEPS
    # 搜索配额和待注入图片都按轮重置 —— 而且重置的是**当前 agent** 的那一份
    reset_turn_searches()  # 子 agent 跑自己的循环时,重置的是它自己的配额
    ctx.current().pending_images.clear()  # 避免上一轮的图带到下一轮
    begin_turn()           # 记下用量起点 —— 一轮里每调一次工具就多发一次请求,
                           # 结尾那行统计要把这一轮的全部算进来,不能只看最后一次

    messages.append({"role": "user", "content": user_input})

    auto_compressed = False
    for _ in range(steps):
        # 上下文快满了就先压缩(工具调用过程中也照做),免得下一次请求超限;每轮最多压一次
        if not auto_compressed and context_ratio() >= AUTO_COMPACT_RATIO:
            auto_compressed = True
            ctx.out().print(f"[自动压缩上下文] {compact(messages, keep_recent=2)}", style="dim")
        try:
            content, tool_calls, reasoning = stream_model(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/流中断,给提示而不是崩掉
            return f"错误: 调用模型失败({type(exc).__name__}: {exc})"

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
                ctx.out().print(
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
        inject_pending_images(messages)

        # 被叫停了(见 tasks.kill)。**放在工具跑完之后 check** —— 正在执行的那次
        # 工具调用不打断,否则可能把它做到一半的文件留在那儿;但不会再走下一步。
        if ctx.current().cancelled:
            return "（已按用户要求中止。之前做完的部分都在对话里,没有被撤销。）"

        # 子 agent 交了问题上来(申请权限、要问用户)→ 停在这儿,别接着往下跑。
        # 接手的是 tasks.py:它把状态记成"等回话",把问题交给主 agent。
        # **停在这儿而不是抛出异常**:对话历史是完整的(最后一条是 tool 结果),
        # 后面 resume 时直接往下走就行。
        if ctx.current().suspend:
            return content

    return f"[强制终止] 已达到最大步数 {steps},对话强制中止"


def load_system_prompt() -> str:
    """读主系统提示词(prompts/system.md),并把代码里的常量注入进去。

    文案都在 prompts/ 下按用途分文件放着,改措辞不用动代码(见 agent/prompts.py)。

    注意:**终端指令清单不在这里**。它由 commands.system_message() 读
    prompts/commands_list.md 生成成另一条 system 消息(见 cli.main)—— 那份清单是
    程序自动生成的"数据",不该混进系统提示词正文,免得其中的描述被当成系统指令照做。
    """
    return prompts.load("system", max_steps=MAX_STEPS)
