from __future__ import annotations

import atexit
import sys
from datetime import datetime

from rich.markdown import Markdown

from . import prompts
from .core import (
    AUTO_COMPACT_RATIO,
    ROOT,
    SESSION_RESUME_CHARS,
    SESSION_RESUME_MESSAGES,
    TRASH_MAX_AGE_DAYS,
    console,
)
from .commands import Context as CommandContext
from .commands import all_commands, dispatch as dispatch_command
from .commands import system_message as commands_system_message
from . import skills
from . import ctx
from . import tasks
from .llm import usage_line
from .loop import load_system_prompt, run
from .session import (
    claim_owner,
    current_session_id,
    load_session,
    release_owner,
    session_note as session_note_text,
    rewrite_session,
    touch_session,
)
from .tools.container import docker_cleanup_stale, docker_health
from .tools.memory import memory_text
from .tools.trash import purge_trash
from .tools.vm import vm_kickoff, vm_state_get, session_reset_hint, vm_status


def _clip(text: str, limit: int) -> tuple[str, int]:
    """把一段文本压成单行并截断,返回 (要显示的文本, 被藏起来的字数)。

    把"藏了多少"单独返回而不是拼进文本里 —— 调用方要把它放在合适的位置
    (比如工具参数那行要塞在右括号**外面**,不然会变成 `…未显示))`)。
    截断时务必把这个数报出去:光留一个省略号,读的人会以为原文就到这儿。
    """
    one = " ".join(text.split())
    if limit <= 0 or len(one) <= limit:
        return one, 0
    return one[:limit] + "…", len(one) - limit


def _replay_history(history: list[dict], source: str = "上次会话") -> None:
    """把恢复/切换回来的对话**按当时的样子重放一遍** —— 和实时对话用同一套渲染。

    source 只改开头那句说明:启动恢复时是"上次会话",/switch 之后是"刚切到的会话"。

    刻意不做成"摘要式预览":那样和真实对话长得不一样,一眼看去分不清哪些是
    刚才发生的、哪些是上次的。这里逐条重放:用户输入、思考、工具调用、
    AI 回答,顺序与样式都和跑的时候一致(tool 消息在实时里本来就不显示,
    这里同样跳过)。

    唯一做了裁剪的是**思考**和**工具参数** —— 前者动辄几千字、后者可能塞着
    整个文件内容(如 write_file 的 content),照原样打会刷屏。AI 的回答和用户的
    输入完整显示,那才是对话本身。SESSION_RESUME_CHARS=0 表示连这两处也不裁。
    """
    if SESSION_RESUME_MESSAGES <= 0 or not history:
        return
    readable = [m for m in history if m.get("role") in ("user", "assistant")]
    shown = readable[-SESSION_RESUME_MESSAGES:]
    if not shown:
        return

    console.print("─" * 46, style="dim")
    console.print(
        f"↓ 以下是{source}的重放(共 {len(readable)} 条,最近 {len(shown)} 条)"
        f" —— 已接着继续,不用重说",
        style="dim", markup=False,
    )
    for m in shown:
        content = m.get("content")
        if isinstance(content, list):        # 图片等注入消息,不是用户打的字
            continue
        text = (content or "").strip()

        if m.get("role") == "user":
            console.print("你 > ", style="bold cyan", end="")
            console.print(text, markup=False)
            continue

        # ---- assistant:思考 → 工具调用 → 回答,顺序与实时一致 ----
        reasoning = (m.get("reasoning_content") or "").strip()
        if reasoning:
            shown, hidden = _clip(reasoning, SESSION_RESUME_CHARS)
            console.print("* 思考", style="dim", markup=False)
            console.print(shown, style="dim italic", markup=False,
                          highlight=False, soft_wrap=True, end="")
            # 藏了就说清楚藏了多少,别让思考"戛然而止"
            console.print(f"    (完整思考还有 {hidden} 字未显示)" if hidden else "",
                          style="dim", markup=False)
        # **话要说在工具调用前面** —— 同一次响应里 content 和 tool_calls 是一起回来的,
        # 语义上就是"先说一句、再去调"。反过来渲染的话,满屏 • 里夹着几行 AI >,读起来
        # 像是"这轮只有工具调用",而那几句话恰恰是实时界面里看不到的(见下方说明)。
        if text:
            console.print("AI >", style="bold green")
            # 与实时一致:拿到完整文本后交由 Markdown 渲染
            console.print(Markdown(text))
        for call in (m.get("tool_calls") or []):
            fn = call.get("function", {})
            shown, hidden = _clip(fn.get("arguments") or "", SESSION_RESUME_CHARS)
            # 提示放在右括号**外面**,否则读起来像 `…未显示))`
            tail = f"    (参数还有 {hidden} 字未显示)" if hidden else ""
            console.print(f"• {fn.get('name', '?')}({shown}){tail}",
                          style="dim", markup=False, highlight=False)
    console.print("─" * 46, style="dim")


def _restore_notice(restored_count: int) -> dict:
    """会话恢复时追加的那条提示(**追加在历史末尾**,不是开头)。

    两个要点:

    1. **位置在末尾**。插在历史前面会让整段已恢复历史的前缀失配、缓存全 miss;
       追加在末尾则前缀完整命中,只有这条和新提问要重新算 —— 这正是"持久化能
       提高缓存命中"的地方。它自己也会被持久化,下次恢复时同样在匹配的前缀里。
    2. **用 system 角色**。它讲的是程序级状态(本次是恢复、VM 被重启),不是用户
       说的话,也不该被当成助手说过的话。注意 session 落盘时只过滤**开头连续**
       的 system 消息,所以这条中段的 system 能存下来。
    """
    return {
        "role": "system",
        "content": prompts.load(
            "resume_notice",
            count=restored_count,
            now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        ),
    }


def _cleanup_trash_on_start() -> None:
    """启动时删除超过保留期(默认 7 天)的回收站文件。

    必须传 TRASH_MAX_AGE_DAYS —— 若调 purge_trash()(无参),会变成"清空全部",
    把还没过保留期的文件也一起删了,那就违背"只删 7 天以上"的本意了。
    """
    try:
        result = purge_trash(TRASH_MAX_AGE_DAYS)
        if "删除 0 个" not in result:  # 只在确有清理时提示,免得每次启动都罗嗦
            console.print(f"回收站:{result}", style="dim")
    except Exception as exc:  # noqa: BLE001 - 回收站清理失败不应阻止 agent 启动
        console.print(f"回收站清理失败(不影响使用):{exc}", style="dim")


_EXIT_WORDS = {"exit", "quit"}      # 裸敲也等价于 /exit,两处用法见下


def _read_multiline(prompt: str = "你 > ") -> str:
    """读取一段输入,空行提交 —— 支持粘贴多行并保留换行。

    console.input() 只读单行,没法粘贴多行代码/文本。改为逐行读取,
    用户输入完(或粘贴完)按一个空行结束。空行只作提交信号,不会进消息。

    例外:**第一行本身就是终端指令**(以 / 开头)或裸的 exit/quit 时,回车即执行,
    不必再敲空行 —— 指令天生是单行的,逼人多按一次回车只会烦人。
    """
    lines: list[str] = []
    try:
        console.print(prompt, style="bold cyan", end="")
        while True:
            line = sys.stdin.readline()
            if line == "":  # EOF(Ctrl+D / Ctrl+Z),停止
                break
            if line in {"\n", "\r\n"}:  # 空行 = 提交
                break
            lines.append(line.rstrip("\r\n"))
            if len(lines) == 1 and (lines[0].lstrip().startswith("/")
                                    or lines[0].strip().lower() in _EXIT_WORDS):
                break               # 指令:回车即走,连续行提示符都不打
            # 续行提示符用 ASCII 的 "+",别用省略号 —— 那个符号在终端里基本等于
            # "加载中/思考中",看到它会以为程序正忙、在那儿干等一个不会来的回复。
            console.print("+ ", style="dim", end="")
    except KeyboardInterrupt:
        # 空闲时 Ctrl+C = 退出信号(返回 None)。
        # 必须包住整个函数(含提示打印),否则中断落在 console.print 里的
        # os.get_terminal_size() 时(像这次的栈)会从这个函数逃逸,直接崩掉进程。
        # 恢复打印用裸 write(不走 Rich),避免 get_terminal_size 又被中断引发二次异常。
        sys.stdout.write("\n")
        return None
    except EOFError:
        pass
    return "\n".join(lines)


def _make_stdio_forgiving() -> None:
    """把标准输入输出的编码错误策略改成"替换",消掉一整类编码崩溃。

    **stdout/stderr**:真实控制台上 rich 走 WriteConsoleW,什么字符都能显示;
    但输出被重定向到文件或管道时,它退回用系统区域编码(中文 Windows 是 GBK),
    而 `•`(U+2022)、`✻`(U+273B) 这类字符 GBK 表示不了 —— 会抛
    UnicodeEncodeError 把整个进程带走。宁可显示成 "?",也不该让会话崩掉。

    **stdin** 更要紧:Python 给它的默认策略是 surrogateescape —— 解不出的字节
    会变成**孤立代理字符**(如 \\udca8)留在消息里,之后每次请求发给 API 时
    utf-8 都编不出来,整个会话会持续报 "surrogates not allowed"。换成 replace
    后最多丢一个字符,不会把会话搞废。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 老解释器或被替换过的流,失败就算了
            pass


def _on_exit() -> None:
    """退出收尾:把 last 指向当前会话,再摘掉自己的占用标记。

    两个都要现取会话 id,**别用启动时那个 session_id** —— /switch 会换掉它,而局部变量
    切完就过期了:拿旧的会把 last 指回上一个会话,还会摘错占用标记(新会话的摘不掉,
    下次启动就以为它还被占着)。这和 rewrite_session 那处是同一个坑。

    last 只在**正常退出**时这样更新;被强杀、崩溃时 atexit 不跑,那种情况靠下次启动时
    的 register_session 兜底 —— 两处都要有,不能只留一个。
    """
    sid = current_session_id()
    touch_session(sid)
    release_owner(sid)


def main() -> None:
    """入口:**建主 agent 的上下文,然后在它里面跑整个会话**。

    为什么要包这一层:待注入的图片、搜索配额、用量这些状态现在都挂在上下文上
    (见 agent/ctx.py)—— 整个会话——包括工具调用——必须跑在**主 agent 那个上下文里**,
    否则它们会落到 ctx 的兜底上下文上,用量统计和搜索配额就都不生效了。
    """
    with ctx.use(ctx.AgentCtx(role="main")):
        _session_loop()


def _session_loop() -> None:
    _make_stdio_forgiving()
    messages: list[dict] = [{"role": "system", "content": load_system_prompt()}]
    memory = memory_text().strip()  # 跨会话记住的关键事实最先注入,始终在场
    if memory:
        messages.append(
            {"role": "system", "content": prompts.load("memory_injection", memory=memory)}
        )
    # 终端指令清单单独成一条 system 消息,而不是并进主提示词 —— 它是程序自动生成的
    # "数据",里面写明信任边界,免得描述文字被当成系统指令(详见 commands.system_message)
    messages.append({"role": "system", "content": commands_system_message()})
    # 技能清单同理:只放"名字 + 什么时候用",正文留在磁盘上按需读(见 agent/skills.py)
    # 清单按角色渲染:主 agent 看得见**全部**技能(包括它自己加载不了的,否则没法
    # 合理分配任务),子 agent 只看得见它能加载的
    messages.append({"role": "system", "content": skills.prompt_section(role="main")})
    # 定下本次的活跃会话。规则(见 session._resolve_session):接回本工作区最后跑过的
    # 那一个,但**如果它正被另一个活着的 agent 用着,就另开一个新的**,不去抢 ——
    # 抢的话两边会共用一个对话历史和一块虚拟机磁盘,互相覆盖。
    session_id = current_session_id()
    claim_owner(session_id)          # 立刻登记占用,让别人知道这个会话正在被用
    session_note = session_note_text()
    history, resume_note = load_session(session_id)
    messages.extend(history)
    if history:
        # 接回历史后追加一条提示(追加在末尾以保住前缀缓存,详见 _restore_notice)
        messages.append(_restore_notice(len(history)))
    atexit.register(_on_exit)
    _cleanup_trash_on_start()
    # 预处理 Docker 健康状态(非阻断):可用则做残留清理,不可用仅警告,agent 照常启动
    ok, msg = docker_health()
    if ok:
        n = docker_cleanup_stale()
        if n:
            console.print(f"已清理 {n} 个上次残留的容器", style="dim")
    console.print(f"Docker:{msg if ok else '! 不可用 —— ' + msg}", style="dim" if ok else "yellow")
    # 后台拉起虚拟机(非阻断,失败仅提示,agent 照常启动)
    try:
        vm_kickoff()  # 后台线程启动/配置虚拟机,不阻塞
        ready = vm_state_get()["status"] == "ready"
        console.print(f"VM:{vm_status()}", style="dim" if ready else "yellow")
    except Exception as exc:  # noqa: BLE001
        console.print(f"VM:启动失败(不影响 agent)—— {exc}", style="yellow")
    console.print("多行输入用空行提交;执行中 Ctrl+C 取消本轮,空闲时退出。", style="dim")
    console.print("指令:" + "、".join(n for n, *_ in all_commands())
                  + f"(/help 有说明);上下文到 {AUTO_COMPACT_RATIO:.0%} 自动压缩。",
                  style="dim")
    console.print(f"工作区:{ROOT}", style="dim")
    console.print(f"会话:{session_id} — {session_note};{resume_note}", style="dim")
    # 上次进程退出时没跑完的子 agent:标成「中断」并告诉主 agent —— **不自动续跑**,
    # 中断那一步的副作用是未知的,由它决定接着干还是重派。
    for note in tasks.recover():
        messages.append({"role": "system", "content": note})
    _replay_history(history)   # 把上次对话按原样重放一遍，接着聊
    console.print()

    while True:
        try:
            # 接管着某个子 agent 时,提示符换成它 —— 一眼能看出"现在敲的话是说给谁听的"
            watched = tasks.attached()
            user_input = _read_multiline(f"[{watched}] > " if watched else "你 > ")
            if user_input is None:  # 空闲时 Ctrl+C = 退出
                console.print("再见。", style="dim")
                break
            text = user_input.rstrip()  # 去掉粘贴时多带的结尾空行,保留行内缩进
            if not text.strip():
                continue
            # 接管状态下,**不带头斜杠的话就是说给那个子 agent 的**(而不是主 agent)。
            # 带斜杠仍然是指令 —— 否则用户被困在里面,连 /subtasks off 都敲不出来。
            if watched and not text.lstrip().startswith("/"):
                console.print(tasks.tell(watched, text), style="dim")
                continue
            # 裸敲的 exit / quit 当成 /exit:老习惯要接住,但退出只留一个入口 ——
            # 否则"纯字符退出"和"指令退出"两套逻辑各走各的,早晚对不上。
            if text.strip().lower() in _EXIT_WORDS:
                text = "/exit"

            # 后台子 agent 干完了 → 在这儿把通报塞进对话,主 agent 下一句话就带着它。
            # 放在用户输入**之前**追加:子 agent 是在用户上一句话的上下文里干完的,
            # 通报属于那件事的后续,不该排在新问题后面。
            for note in tasks.pending_notifications():
                messages.append({
                    "role": "system",
                    "content": "（后台子 agent 的通报,你没在等它,它自己干完了）\n\n" + note,
                })
            # 终端指令(/compact、/reset、/tokens…)。注册在 agent/commands/ 里,
            # 系统提示词中那段说明也由同一份注册表生成,不用两头各维护一遍。
            cmd_ctx = CommandContext(messages)
            cmd_result = dispatch_command(text, cmd_ctx)
            if cmd_result is not None:
                # 会话被重置 → 顺带说清虚拟机没跟着重置。为什么补在这层:只有装配层
                # 同时认识"会话"和"VM"两边;命令层只声明事实,不去 import VM 模块。
                if "session_reset" in cmd_ctx.events:
                    cmd_result += session_reset_hint()
                if cmd_result:
                    # 用终端默认亮度,别加 dim —— dim 是留给背景信息的(Docker/VM 状态、
                    # 工作区路径那些)。命令结果是用户主动敲的、正要读的内容,/help 一次
                    # 列八条指令,暗色下几乎看不清。
                    console.print(cmd_result)
                if "session_switched" in cmd_ctx.events:
                    # 切完会话,把新会话的历史按原样重放一遍 —— 只说一句"已切到 X",
                    # 用户看不到里面聊过什么。和启动时的重放共用同一套渲染,长得一样。
                    _replay_history(messages, source="刚切到的会话")
                if "exit" in cmd_ctx.events:      # /exit、/quit —— 收尾动作由这层做
                    break
                continue

            # 快照这一轮开始前的整份历史。存"内容"而不是长度 —— 本轮里可能发生
            # 自动压缩(把历史改短),那时按长度回滚会算错位置:短了删不掉、长了会
            # 把压缩后的新历史也削掉一截。整份快照才能精确还原。
            # 浅拷贝即可,消息字典本身不再改动;还原用切片赋值,list 对象身份不变。
            snapshot = list(messages)
            try:
                reply = run(text, messages)
            except KeyboardInterrupt:
                # 执行中 Ctrl+C:回滚半截对话,回到提示,会话不退出
                messages[:] = snapshot
                console.print("\n[已取消]", style="bold red")
                continue
            except Exception as exc:  # noqa: BLE001
                # 未预期的错误(渲染、网络、工具内部崩了……)绝不能掀翻整个会话 ——
                # 丢掉这一轮的对话会让人白等,还要从头把上下文喂一遍。
                # 这里 **同样要回滚**:半截历史里很可能留着一个没有 tool 结果配对的
                # tool_calls,那个发给 API 会直接 400,回滚才能保证历史始终合法。
                messages[:] = snapshot
                console.print(f"\n[出错,本轮已回滚]{type(exc).__name__}: {exc}", style="bold red")
                continue

            # 这一轮成功了才落盘(回滚的两个分支上面都 continue 了,不会被写进去)。
            # 整份重写而不是追加:本轮里可能发生过自动压缩,历史已被整体换掉,
            # 追加会把"压缩后"和"压缩前"的消息混在一个文件里。文件本身有界
            # (受上下文上限与压缩约束,通常几百 KB),整体重写的开销可忽略,
            # 换来的是怎么都不会错。
            # 会话 id 每次现取 —— /switch 会换掉它,而启动时那个 session_id 是局部变量、
            # 切完就过期了。取错的那个会把**新会话的历史写进旧会话的文件**,而且落盘是
            # 整份重写,旧会话原有内容会被直接覆盖掉(实测踩过:切一次会话丢一份记录)。
            rewrite_session(messages, current_session_id())

            console.print("AI >", style="bold green")
            # Markdown 要拿到完整文本才能正确解析,所以是等模型说完再一次性渲染
            console.print(Markdown(reply) if reply.strip() else "(模型没有返回内容)")
            line = usage_line()
            if line:
                console.print(line, style="dim")
            console.print()
        except KeyboardInterrupt:
            # 兜底:任何没被上面捕获的 Ctrl+C(例如渲染 Markdown 那一下),
            # 一律干净退出,而不是抛栈崩掉。
            console.print("\n再见。", style="dim")
            break


if __name__ == "__main__":
    main()
