"""查看类指令:用了多少 token、有哪些指令。"""
from __future__ import annotations

from . import Context, all_commands, command


@command(
    "/tokens",
    "查看当前上下文用了多少 token(占模型上限的百分比、本轮输出、缓存命中)以及本会话累计。"
    "用户问「用了多少 token / 上下文满了吗」,或你想确认还有多少空间时建议用它。",
)
def cmd_tokens(ctx: Context) -> str:
    from ..llm import usage_detail
    return usage_detail()


@command(
    "/help",
    "列出所有可用的终端指令及其作用。用户问「有哪些指令 / 能敲什么」时建议用它。",
)
def cmd_help(ctx: Context) -> str:
    lines = ["可用指令:", ""]
    for name, desc, aliases in all_commands():
        alias_note = f" (别名 {'、'.join(aliases.split('、'))})" if aliases else ""
        lines.append(f"  {name}{alias_note}")
        lines.append(f"      {desc}")
    lines += ["", "  还有 exit / quit —— 退出。"]
    return "\n".join(lines)
