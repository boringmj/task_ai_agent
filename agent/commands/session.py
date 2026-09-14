"""与会话历史有关的指令:重置、压缩。"""
from __future__ import annotations

from . import Context, command


from .. import prompts


@command(
    "/reset",
    "丢弃当前会话的对话历史,重新开始。工作区文件和长期记忆都不受影响。",
    hint="用户想彻底换个话题、不想被前面的对话干扰,或会话被搞乱时。",
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

    # 只声明"会话被重置了"这个事实。虚拟机没跟着重置、要不要提醒用户 —— 那是 VM 那边
    # 的事,由 cli 接上(见 commands/__init__.py 里 events 的说明)。
    ctx.events.add("session_reset")
    return "会话已重置(文件与长期记忆未动)。"


@command(
    "/sessions",
    "列出这个工作区里用过的会话(每个会话有自己独立的对话历史与虚拟机),标明哪个是当前活跃的。",
    hint="用户问「以前聊过哪些 / 有几段会话」时。",
)
def cmd_sessions(ctx: Context) -> str:
    from ..session import current_session_id, list_sessions

    rows = list_sessions()
    if not rows:
        return "(这个工作区还没有会话记录)"
    active = current_session_id()
    lines = [f"当前活跃会话:{active}", "", "全部会话(最近在前):"]
    for s in rows:
        # 状态要一眼看出来:哪个是当前、哪个被别的 agent 占着(切不过去)、
        # 哪个是空的、哪个目录已经没了
        if s["id"] == active:
            state = "← 当前"
        elif s["busy"]:
            state = "! 被另一个 agent 占用"
        elif s["missing"]:
            state = "(目录已丢失)"
        else:
            state = "空闲"
        lines.append(f"  {s['id']}  最后使用 {s['last_used']}  {state}")
    return "\n".join(lines)


@command(
    "/switch",
    "切换活跃会话。/switch <会话id> 切到那个会话,/switch new 新开一个会话。"
    "只能切到没被别的 agent 占用的会话上;切换会连带把虚拟机换成该会话自己的磁盘"
    "(所以 VM 会重启用)。",
    hint="用户在几段不同主题的会话之间来回切时。",
)
def cmd_switch(ctx: Context) -> str:
    """切到指定会话或新建一个,并把历史与虚拟机一并换过去。"""
    from ..session import (current_session_id, list_sessions, load_session,
                           new_session_id, release_owner, session_dir,
                           set_current_session, try_claim)
    from ..tools.vm import vm_switch_session

    target = ctx.args.strip()
    cur = current_session_id()

    if not target:
        # 不带参数就当"列个清单帮我选",不用让用户先去敲 /sessions 看一眼
        lines = [f"当前会话:{cur}", "", "用 `/switch <id>` 切换,或 `/switch new` 新开一个。", ""]
        for s in list_sessions():
            if s["active"]:
                state = "← 当前"
            elif s["busy"]:
                state = "! 被另一个 agent 占用"
            elif s["missing"]:
                state = "(目录已丢失)"
            else:
                state = "空闲"
            lines.append(f"  {s['id']}  最后使用 {s['last_used']}  {state}")
        return "\n".join(lines)

    creating = target.lower() in ("new", "新")
    if creating:
        target = new_session_id()
    else:
        if not session_dir(target).exists():
            return f"没有会话 {target}。用 /sessions 查询 id。"

    if target == cur:
        return f"已经在会话 {target} 上了。"

    # 原子认领目标:抢不到说明别人正在用,就不切
    if not try_claim(target):
        return f"会话 {target} 正被另一个 agent 占用(换一个,或 /switch new)。"

    release_owner(cur)          # 先拿到新的,再放旧的 —— 反过来的话认领失败就没主了
    set_current_session(target)

    hist, note = load_session(target)
    kept = 0
    while kept < len(ctx.messages) and ctx.messages[kept].get("role") == "system":
        kept += 1
    del ctx.messages[kept:]
    ctx.messages.extend(hist)
    ctx.messages.append({
        "role": "system",
        "content": prompts.load("switch_notice", session=target,
                                note="(新建的)" if creating else ""),
    })

    vm_switch_session()          # 每会话一块盘,换会话就得换 VM
    # 请终端把刚切到的这段历史重放出来。指令的返回值只是一行文本,打不了对话,
    # 所以照旧只声明事实、由 cli 那层去重放(和 session_reset 一个套路)。
    ctx.events.add("session_switched")
    kind = "已新建并切到" if creating else "已切到"
    return f"{kind}会话 {target}({note}),虚拟机正在按该会话的磁盘重启。"


@command(
    "/compact",
    "把已有的对话历史压缩成一份摘要,释放上下文(会保留用户的偏好、已做的决定、"
    "文件改动和待办)。",
    hint="上下文占用偏高、对话很长,或用户问「怎么省 token / 怎么清一下上下文」时。",
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
