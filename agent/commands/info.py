"""查看类指令(token 用量、指令清单)与退出。"""
from __future__ import annotations

from . import Context, all_commands, command, lookup


@command(
    "/tokens",
    "查看当前上下文用了多少 token:占模型上限的百分比、本轮输出、缓存命中,以及本会话累计。",
    usage="""
/tokens          看用量。
                三段口径:上下文(现在有多大)、本轮(这句话花了多少)、
                本会话(一共花了多少)。一轮里每调一次工具就多发一次请求,
                所以"本轮"是那几次之和,不是最后那一次。
""",
    hint="用户问「用了多少 token / 上下文满了吗」,或你想确认还有多少空间时。",
)
def cmd_tokens(ctx: Context) -> str:
    from ..llm import usage_detail
    return usage_detail()


@command(
    "/help",
    "列出所有可用的终端指令及一句话作用;带指令名则打出那一条的详细用法。",
    usage="""
/help            列出全部指令(只有名字和一句话作用)。
/help <指令>     看那一条的详细用法,如 /help subtasks。
                带不带前导斜杠都行,别名也认(/help quit 会打 /exit)。
""",
    hint="用户问「有哪些指令 / 能敲什么 / 这个指令怎么用」时。",
)
def cmd_help(ctx: Context) -> str:
    """指令清单 / 单条详情。**用户问「怎么用」时答不上来,代价比多打几行大得多。**"""
    want = (ctx.args or "").strip()
    if want:
        entry = lookup(want)
        if entry is None:
            names = "、".join(n for n, *_ in all_commands())
            return f"没有名为 {want} 的指令。可用:{names}\n(想找哪条就 /help <指令名>)"
        name, desc, _hint, aliases, usage = entry
        lines = [f"{name} —— {desc}"]
        if aliases:
            lines.append(f"别名:{aliases}")
        lines += ["", "用法:", usage]
        return "\n".join(lines)

    lines = ["可用指令(敲 /help <指令> 看详细用法):", ""]
    for name, desc, _hint, aliases, _usage in all_commands():
        # 别名直接跟在名字后面 —— 用户找的往往是"那个 /new 是干嘛的",而不是新名字
        alias_note = f"(别名 {'、'.join(aliases.split('、'))})" if aliases else ""
        lines.append(f"  {name} {alias_note}".rstrip())
        lines.append(f"      {desc}")
    return "\n".join(lines)


@command(
    "/exit",
    "退出程序。",
    usage="""
/exit            退出。等价于裸敲 exit 或 quit。
/quit            同上(旧名,等价)。
退出前会把还在跑的子 agent 收拢掉;正在跑的那一轮不会替你写完。
""",
    hint="用户表示聊完了、想关掉时,可以提醒他敲这个(或它的别名 /quit)。"
         "但别在用户没说要结束时主动建议退出。",
    aliases=("/quit",),
)
def cmd_exit(ctx: Context) -> str:
    """退出程序:只声明「用户要退出」这个事实,真正结束 REPL 是 cli 那边的事。

    和 session_reset 同理 —— 指令的返回值只是要打印的文本,自己管不了循环。
    """
    ctx.events.add("exit")
    return "再见。"
