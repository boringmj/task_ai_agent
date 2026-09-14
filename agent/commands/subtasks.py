"""`/subtasks`:看子 agent 在干什么,以及接管它。

**为什么要有它**:子 agent 的输出默认**一行都不打到终端**(主终端是用户跟主 agent 对话的
地方,被几百行分析日志刷了就没法用了)。但"默认不吵"不等于"看不见" —— 用户得有个办法
把某个子 agent 拎出来看个仔细,甚至直接跟它说话。

这一整套不需要什么终端控制能力:**不做全屏切换,只做"把输出转过来"和"把输入送过去"**。
"""
from __future__ import annotations

from . import Context, command

_USAGE = ("用法:/subtasks 看全部;/subtasks t1 看一个;"
          "/subtasks t1 enter 接管(它的输出实时打出来,你敲的字进它的对话);"
          "/subtasks off 退出接管;/subtasks t1 kill 叫停。")


@command(
    "/subtasks",
    "看子 agent 们的进展,或者接管其中一个(实时看它的输出、直接跟它说话)。"
    "不带参数列出全部;带任务号看详情。",
    hint="用户问「子 agent 在干什么」「那个后台任务怎么样了」时告诉他这个指令;"
         "他想盯着某个子 agent 看、或者想直接跟它说话时,用 /subtasks <任务号> enter。",
)
def cmd_subtasks(ctx: Context) -> str:
    from .. import tasks

    args = (ctx.args or "").split()
    if not args:
        head = "已接管:" + (tasks.attached() or "无") if tasks.attached() else ""
        return "\n".join(x for x in (tasks.listing(), head) if x)

    tid = args[0]
    if tid in ("off", "all"):
        if tid == "all":
            return tasks.listing(show_closed=True)
        return tasks.detach()

    rest = args[1:]
    if not rest:
        return tasks.show(tid)

    action = rest[0].lower()
    if action in ("enter", "attach"):
        text = tasks.attach(tid)
        # 接管时先把已经说过的补上 —— 否则用户盯着一个空白,不知道它刚才在忙什么
        hist = tasks.buffer_text(tid)
        if hist.strip():
            return text + "\n\n(以下是它到刚才为止的输出)\n" + hist
        return text
    if action in ("exit", "detach"):
        return tasks.detach()
    if action in ("kill", "stop"):
        return tasks.kill(tid)
    if action in ("log", "history"):
        return tasks.log(tid)
    return f"不认识的子命令 {action!r}。\n{_USAGE}"
