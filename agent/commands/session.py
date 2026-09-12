"""与会话历史有关的指令:重置、压缩。"""
from __future__ import annotations

from . import Context, command


@command(
    "/reset",
    "丢弃当前会话的对话历史,重新开始。工作区文件和长期记忆都不受影响。"
    "用户想彻底换个话题、不想被前面的对话干扰,或会话被搞乱时建议用它。",
    aliases=("/new",),
)
def cmd_reset(ctx: Context) -> str:
    """清空会话:只留下开头的 system 消息(提示词与长期记忆),并删掉磁盘上的存档。"""
    # 延迟导入,避免 commands ↔ session / loop 之间成环
    from ..session import clear_session, current_session_id

    kept = 0
    while kept < len(ctx.messages) and ctx.messages[kept].get("role") == "system":
        kept += 1
    del ctx.messages[kept:]
    clear_session(current_session_id())
    return "已重置会话:之前的对话不再带入(文件与长期记忆未动)。"


@command(
    "/sessions",
    "列出这个工作区里用过的会话(每个会话有自己独立的对话历史与虚拟机),标明哪个是当前活跃的。"
    "用户问「以前聊过哪些 / 有几段会话」时建议用它。",
)
def cmd_sessions(ctx: Context) -> str:
    from ..session import current_session_id, list_sessions

    rows = list_sessions()
    if not rows:
        return "(这个工作区还没有会话记录)"
    active = current_session_id()
    lines = [f"当前活跃会话:{active}", "", "这个工作区里的会话(最近用的在前):"]
    for s in rows:
        mark = "← 当前" if s["id"] == active else ""
        note = "(目录已丢失)" if s["missing"] else ""
        lines.append(f"  {s['id']}  最后使用 {s['last_used']} {mark}{note}")
    return "\n".join(lines)


@command(
    "/compact",
    "把已有的对话历史压缩成一份摘要,释放上下文(会保留用户的偏好、已做的决定、"
    "文件改动和待办)。上下文占用偏高、对话很长,或用户问「怎么省 token / 怎么清一下上下文」时建议用它。",
)
def cmd_compact(ctx: Context) -> str:
    """压缩历史:摘要替换掉较早的对话,并同步把磁盘上的存档改成压缩后的样子。"""
    # loop 依赖 commands(要在提示词里注入指令清单),这里再反向导入就成环了,
    # 所以放到函数里延迟导入 —— 调用时两边模块早已加载完毕。
    from ..loop import compact
    from ..session import current_session_id, rewrite_session

    result = compact(ctx.messages)
    rewrite_session(ctx.messages, current_session_id())
    return result
