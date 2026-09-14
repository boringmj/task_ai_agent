"""`/subtasks`:看子 agent 在干什么,以及接管它。

**为什么要有它**:子 agent 的输出默认**一行都不打到终端**(主终端是用户跟主 agent 对话的
地方,被几百行分析日志刷了就没法用了)。但"默认不吵"不等于"看不见" —— 用户得有个办法
把某个子 agent 拎出来看个仔细,甚至直接跟它说话。

这一整套不需要什么终端控制能力:**不做全屏切换,只做"把输出转过来"和"把输入送过去"**。
"""
from __future__ import annotations

from . import Context, command

_USAGE = """
/subtasks            列出全部子 agent 的状态。已验收关闭的默认不列,用 all 看。
/subtasks <任务号>   看那一个的详情:任务是什么、什么状态、卡在等什么、
                     在它之前干到哪、对话和产出在哪。
                     任务号写 t2 或 2 都行。
/subtasks <任务号> enter
                     接管它:它说的话实时打到你终端上(带 [任务号] 前缀),
                     你敲的字进它的对话。再敲一次 /subtasks off 退出来。
/subtasks off        退出接管(写 exit 或 detach 也一样)。
                     它的输出重新回到缓冲区,不再刷屏。
/subtasks <任务号> kill
                     叫停它。是商量式的:它会在当前这一步做完之后停,
                     正在跑的那次工具调用不打断。停完自动关闭。
/subtasks <任务号> log
                     打它的完整对话(给人和排错看的,不进模型上下文)。
/subtasks all        连已验收关闭的一起列出来。
""".strip()


@command(
    "/subtasks",
    "看子 agent 们在干什么、卡在哪、干完没有,以及接管其中一个(实时看它的输出、直接跟它说话)。"
    "不带参数列出全部;带任务号看详情;带 enter 接管;带 kill 叫停。",
    usage=_USAGE,
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
    if tid == "all":
        return tasks.listing(show_closed=True)
    # 退出接管的几种写法。**不带任务号也要认** —— 用户正卡在接管里,最先想到的就是敲
    # `/subtasks exit`(他不会去翻 /subtasks t2 exit 那种写法,因为"退出"这件事跟
    # 哪个任务无关)。实测:这两个词原来只认带任务号的形式,于是敲了没反应。
    if tid in ("off", "exit", "detach", "leave"):
        return tasks.detach()

    rest = args[1:]
    if not rest:
        return tasks.show(tid)

    action = rest[0].lower()
    if action in ("enter", "attach"):
        text = tasks.attach(tid)
        # 接管时先把它的历史补上 —— 否则用户盯着一个空白,不知道它干过什么、干到哪了。
        # **这份历史是从它的对话重建的**(不是终端输出缓冲),所以你给它的任务、工具返回的
        # 结果、它交的结论都在里面;从盘上读回来的任务(重启之后再接管)同样有。
        hist = tasks.replay_text(tid)
        parts = [text]
        if hist.strip():
            parts.append("(以下是它到刚才为止的记录)")
            parts.append(hist)
        # **"怎么退出"必须排在最后。** 排在前面的话,上面这段补出来的历史会当场把它
        # 顶出屏幕 —— 实测用户就是这么卡住的:他接管了、看到了输出、然后不知道该怎么出去,
        # 试了 /exit,结果把整个程序关掉了。
        hint = tasks.attach_hint()
        if hint:
            parts.append("")
            parts.append(hint)
        return "\n".join(parts)
    if action in ("exit", "detach", "leave"):
        return tasks.detach()
    if action in ("kill", "stop"):
        return tasks.kill(tid)
    if action in ("log", "history"):
        return tasks.log(tid)
    return f"不认识的子命令 {action!r}。用法:\n{_USAGE}"
