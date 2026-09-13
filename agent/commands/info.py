"""查看类指令(token 用量、指令清单)与退出。"""
from __future__ import annotations

from . import Context, all_commands, command


@command(
    "/tokens",
    "查看当前上下文用了多少 token:占模型上限的百分比、本轮输出、缓存命中,以及本会话累计。",
    hint="用户问「用了多少 token / 上下文满了吗」,或你想确认还有多少空间时。",
)
def cmd_tokens(ctx: Context) -> str:
    from ..llm import usage_detail
    return usage_detail()


@command(
    "/help",
    "列出所有可用的终端指令及其作用。",
    hint="用户问「有哪些指令 / 能敲什么」时。",
)
def cmd_help(ctx: Context) -> str:
    lines = ["可用指令:", ""]
    for name, desc, _hint, aliases in all_commands():
        alias_note = f" (别名 {'、'.join(aliases.split('、'))})" if aliases else ""
        lines.append(f"  {name}{alias_note}")
        lines.append(f"      {desc}")
    return "\n".join(lines)


@command(
    "/exit",
    "退出程序。",
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
