from __future__ import annotations

from . import ctx, prompts
from .core import (
    AUTO_COMPACT_RATIO,
    MAX_STEPS,
    MODEL,
    client,
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

    # 以 user 身份进对话,但**打上系统前缀** —— 否则重放时屏幕上会冒出一行
    # "你 > (以上对话已压缩…",像是用户自己说了这句话(见 session.SYSTEM_TAG)。
    messages[head_start:] = [{
        "role": "user",
        "content": session_tag() + " " + prompts.load("compact_summary", summary=summary),
    }] + tail
    return f"已压缩上下文: {len(head)} 条消息 → 1 条摘要({len(summary)} 字)"


def session_tag() -> str:
    """程序插进对话的消息统一带的那个前缀(定义在 session 层,别各写各的)。"""
    from . import session
    return session.SYSTEM_TAG


def _persist(msg: dict) -> None:
    """**产生一条就落一条。** 主 agent 和子 agent 走的是同一个循环,落盘位置按当前上下文分。

    为什么要逐条而不是"这一轮完了整体写一次":
      · 一轮里可能有几十次工具调用、跑好几分钟。整体写意味着**中途崩溃/被打断就全丢**,
        而中途被打断是常态(用户按 Ctrl+C、进程被杀、网络断)。
      · 用户按 Ctrl+C 想留下的是"已经发生过的",不是"什么都没有"。整体写把这两者绑死在一起了。

    落盘失败一律吞掉 —— 存不下不该打断对话(session 那边也是这个口径)。
    """
    c = ctx.current()
    try:
        if c.is_sub:
            from . import tasks
            tasks.append_message(c.task_id, msg)
        else:
            from . import session
            session.append_messages([msg], session.current_session_id())
    except Exception:  # noqa: BLE001 - 持久化失败不该影响对话
        pass


def _persist_rewrite(messages: list[dict]) -> None:
    """整体重写。**只在历史被改写过时用**(如自动压缩把前半段换成了摘要)——
    那种情况下追加会把"压缩前"和"压缩后"的消息混在一个文件里。"""
    c = ctx.current()
    try:
        if c.is_sub:
            from . import tasks
            tasks.rewrite_messages(c.task_id, messages)
        else:
            from . import session
            session.rewrite_session(messages, session.current_session_id())
    except Exception:  # noqa: BLE001
        pass


def _inject_notices(messages: list[dict]) -> None:
    """把后台子 agent 跑完的通报插进主 agent 的对话。

    **插在每一步的开头,不能随便什么时候插。** 主 agent 的历史里,
    `assistant(带 tool_calls)` 后面必须**紧跟**它的 tool 结果 —— 中间插一条 user
    进去,那个历史就是非法的,下一次请求直接 400。子 agent 是在**另一个线程**里跑完的,
    它随时可能来插一脚;所以不是"来了就写",而是**排进队列、由主 agent 在安全点自取**。
    代价是最多等一步(那一步通常就是一次工具调用),换来的是历史永远是合法的。

    **以 user 身份发,但要写明这不是用户打的字。** 用 user 是因为它确实是给主 agent 的
    一条新输入(该被当成待办去处理,而不是一段参考信息);写明来源是因为不写的话,
    主 agent 会以为用户刚说了句话,可能去回应一个根本不在场的人。
    """
    if ctx.current().is_sub:
        return                      # 子 agent 不该收别的子 agent 的通报
    from . import tasks
    for t in tasks.needs_attention():
        # 已经交给主 agent 过的就不再重复打扰(比如它 wait=true 干等拿到的那份)
        t.delivered = True
        messages.append({
            "role": "user",
            "content": session_tag() +
                       " 你之前派出去的后台子 agent 有结果了(**这不是用户打的字**)。\n\n"
                       + tasks.report(t)["message"] +
                       "\n\n**你自己判断怎么接**:手头这件事正做到一半、或者现在处理它会打断"
                       "你的思路 —— 那就先不管它,把手上的做完再说。手头正好告一段落、"
                       "或者它的结果恰好是你下一步要用的,就现在处理。别为了「及时」"
                       "硬把正在做的事切断。",
        })
        _persist(messages[-1])


def _forget_announced(messages: list[dict]) -> None:
    """压缩之后重算"哪些通报还在对话里"。

    **不能无脑清标记然后重报** —— 压缩保留尾巴,那条通报如果正好在尾巴里,重报就变成
    重复了(主 agent 收到同一份报告两遍)。所以这里**扫一遍幸存的消息**:谁的通知还在,
    就仍旧算"已通报";被摘要掉的,才重新报一次。

    这个扫描只在压缩之后做一次,平时不跑 —— 代价可以忽略。
    """
    try:
        from . import tasks
        wanting = tasks.needs_attention()
        if not wanting:
            return
        marks = {t.id: f"[子 agent {t.id}" for t in wanting}
        alive = set()
        for m in messages:
            c = m.get("content")
            if not isinstance(c, str):
                continue
            for tid, mark in marks.items():
                if mark in c:
                    alive.add(tid)
        for t in wanting:
            t.delivered = t.id in alive
    except Exception:  # noqa: BLE001
        pass


def run_pending(messages: list[dict], max_steps: int | None = None) -> str | None:
    """**没有用户输入、但有子 agent 的事要处理时,替主 agent 起一轮。**

    这是"主 agent 闲置时被叫醒"的实现:用户没说话,但后台子 agent 出结果了 ——
    那就直接让它处理,不用干等用户下次敲键盘。

    和 `run()` 的唯一区别:**不追加 user 消息**。触发这一轮的是系统插进来的通报
    (由 `_inject_notices` 在第一步开头放进对话),不是谁说了句话 —— 凭空追加一条空的
    用户消息,模型会以为用户说了什么。

    没有待办就返回 None(什么都不做,调用方继续等输入)。
    """
    if not _has_notices():
        return None
    return run(None, messages, max_steps=max_steps)


def _has_notices() -> bool:
    """有没有等着主 agent 处理的子 agent。"""
    try:
        from . import tasks
        return bool(tasks.needs_attention())
    except Exception:  # noqa: BLE001
        return False


def run(user_input: str | None, messages: list[dict], max_steps: int | None = None) -> str:
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

    if user_input is not None:
        # None = 这一轮不是用户发起的(见 run_pending),别凭空塞一条空的用户消息 ——
        # 那会让模型以为用户说了句空话。
        messages.append({"role": "user", "content": user_input})
        _persist(messages[-1])

    auto_compressed = False
    for _ in range(steps):
        # 每一步的开头先收通报 —— 这是"安全点"(见 _inject_notices:插在 tool 配对中间
        # 会让历史非法)。子 agent 干完了就在这儿被吸收进对话,**不用等用户再说话**。
        _inject_notices(messages)

        # 上下文快满了就先压缩(工具调用过程中也照做),免得下一次请求超限;每轮最多压一次
        if not auto_compressed and context_ratio() >= AUTO_COMPACT_RATIO:
            auto_compressed = True
            note = compact(messages, keep_recent=2)
            _persist_rewrite(messages)      # 历史被换掉了,只能整体重写
            # **压缩会把通报吃掉。** 那条"某个子 agent 干完了等你处理"如果正好在被
            # 摘要掉的那一段里,主 agent 就再也不知道这件事了 —— 而它是**待办**,不是
            # 背景信息,不该跟着消息一起消失。所以把"已通报"的标记清掉,让
            # _inject_notices 在下一步把还没处理的重报一遍(finish_task 关掉的不会重报,
            # 它已经不在 needs_attention 里了)。
            _forget_announced(messages)
            ctx.out().print(f"[自动压缩上下文] {note}", style="dim")
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
        _persist(assistant_msg)

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
            _persist(messages[-1])

        # 这一轮若调用了 img,把登记好的图片作为 image_url 注入,给下一轮模型看
        n_before = len(messages)
        inject_pending_images(messages)
        for m in messages[n_before:]:       # 注入的那条也要落盘
            _persist(m)

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

    # **别让这个字符串自己承担语义**。它会被当成 body 交给调用方,而调用方看正文
    # 猜不出这是"干完了"还是"被砍了" —— 所以另外立个旗子说清楚。
    ctx.current().truncated = True
    return f"[强制终止] 已达到最大步数 {steps},对话强制中止"


def load_system_prompt() -> str:
    """读主系统提示词(prompts/system.md),并把代码里的常量注入进去。

    文案都在 prompts/ 下按用途分文件放着,改措辞不用动代码(见 agent/prompts.py)。

    注意:**终端指令清单不在这里**。它由 commands.system_message() 读
    prompts/commands_list.md 生成成另一条 system 消息(见 cli.main)—— 那份清单是
    程序自动生成的"数据",不该混进系统提示词正文,免得其中的描述被当成系统指令照做。
    """
    return prompts.load("system", max_steps=MAX_STEPS)
