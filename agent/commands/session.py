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
    from ..session import clear_session        # 延迟导入,避免 commands ↔ session 成环

    kept = 0
    while kept < len(ctx.messages) and ctx.messages[kept].get("role") == "system":
        kept += 1
    del ctx.messages[kept:]
    clear_session()
    return "已重置会话:之前的对话不再带入(文件与长期记忆未动)。"


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
    from ..session import rewrite_session

    result = compact(ctx.messages)
    rewrite_session(ctx.messages)
    return result
