from __future__ import annotations

import atexit
import sys
from datetime import datetime

from rich.markdown import Markdown

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
from .llm import usage_line
from .loop import load_system_prompt, run
from .session import (
    claim_owner,
    load_session,
    release_owner,
    rewrite_session,
)
from .tools.container import _docker_cleanup_stale, _docker_health
from .tools.memory import _read_memory
from .tools.trash import purge_trash
from .tools.vm import _vm_kickoff, _vm_state_get, vm_status


def _clip(text: str, limit: int) -> str:
    """把一段文本压成单行:折叠所有空白,超长则截断。limit<=0 表示不截断。"""
    one = " ".join(text.split())
    if limit <= 0 or len(one) <= limit:
        return one
    return one[:limit] + "…"


def _replay_history(history: list[dict]) -> None:
    """把恢复回来的对话**按当时的样子重放一遍** —— 和实时对话用同一套渲染。

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
        f"↓ 以下是上次会话的重放(共 {len(readable)} 条对话,重放最近 {len(shown)} 条)"
        f" —— 已经接着这段继续,不用重新说一遍",
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
            console.print("* 思考", style="dim", markup=False)
            console.print(_clip(reasoning, SESSION_RESUME_CHARS), style="dim italic",
                          markup=False, highlight=False, soft_wrap=True)
        for call in (m.get("tool_calls") or []):
            fn = call.get("function", {})
            console.print(
                f"• {fn.get('name', '?')}({_clip(fn.get('arguments') or '', SESSION_RESUME_CHARS)})",
                style="dim", markup=False, highlight=False,
            )
        if text:
            console.print("AI >", style="bold green")
            # 与实时一致:拿到完整文本后交由 Markdown 渲染
            console.print(Markdown(text))
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
        "content": (
            f"【程序提示】本次对话是从上次会话恢复的(接回了 {restored_count} 条历史),"
            f"不是新会话,发生时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}。\n"
            "几点需要你知道:\n"
            "- 上面的内容是**上次运行时**留下的,不要重复做已经做过的事。\n"
            "- 工作区文件、长期记忆都还在。\n"
            "- **虚拟机也被一起恢复了**:它用的是本会话自己的磁盘,上次装过的软件、"
            "写过的文件都还在。\n"
            "- 但虚拟机是**这次重新启动**的,所以上次在里面**跑着的服务/进程已经没了**,"
            "要接着用就得重新拉起来。用之前先 vm_status 确认它起来了。"
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


def _read_multiline(prompt: str = "你 > ") -> str:
    """读取一段多行输入,空行提交 —— 支持粘贴多行并保留换行。

    console.input() 只读单行,没法粘贴多行代码/文本。改为逐行读取,
    用户输入完(或粘贴完)按一个空行结束。空行只作提交信号,不会进消息。
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
            console.print("… ", style="dim", end="")
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


def main() -> None:
    _make_stdio_forgiving()
    messages: list[dict] = [{"role": "system", "content": load_system_prompt()}]
    memory = _read_memory().strip()  # 跨会话记住的关键事实最先注入,始终在场
    if memory:
        messages.append(
            {
                "role": "system",
                "content": f"以下是跨会话保留的长期记忆,和你的对话无关,仅供参考:\n{memory}",
            }
        )
    # 终端指令清单单独成一条 system 消息,而不是并进主提示词 —— 它是程序自动生成的
    # "数据",里面写明信任边界,免得描述文字被当成系统指令(详见 commands.system_message)
    messages.append({"role": "system", "content": commands_system_message()})
    # 会话恢复要赶在别的事情前面:先登记占用者(发现别的实例仍在用就提醒),
    # 再把上次的对话读回来接在最新的 system 消息之后。
    conflict = claim_owner()
    history, resume_note = load_session()
    messages.extend(history)
    if history:
        # 接回历史后追加一条提示(追加在末尾以保住前缀缓存,详见 _restore_notice)
        messages.append(_restore_notice(len(history)))
    atexit.register(release_owner)   # 正常退出时摘掉占用者标记
    _cleanup_trash_on_start()
    # 预处理 Docker 健康状态(非阻断):可用则做残留清理,不可用仅警告,agent 照常启动
    ok, msg = _docker_health()
    if ok:
        n = _docker_cleanup_stale()
        if n:
            console.print(f"已清理 {n} 个上次残留的容器", style="dim")
    console.print(f"Docker:{'✅ ' if ok else '⚠ 不可用 —— '}{msg}", style="dim" if ok else "yellow")
    # 后台拉起虚拟机(非阻断,失败仅提示,agent 照常启动)
    try:
        _vm_kickoff()  # 后台线程启动/配置虚拟机,不阻塞
        ready = _vm_state_get()["status"] == "ready"
        console.print(f"VM:{vm_status()}", style="dim" if ready else "yellow")
    except Exception as exc:  # noqa: BLE001
        console.print(f"VM:启动失败(不影响 agent)—— {exc}", style="yellow")
    console.print("Agent 已启动。", style="bold")
    console.print("输入多行:连续输入,最后一个空行提交(支持粘贴)。", style="dim")
    console.print("执行中 Ctrl+C=取消本轮;空闲时 Ctrl+C=退出;exit 退出。", style="dim")
    console.print("指令:" + "、".join(n for n, _, _ in all_commands())
                  + f"(敲 /help 看说明);上下文占用达 {AUTO_COMPACT_RATIO:.0%} 会自动压缩。",
                  style="dim")
    console.print(f"工作区:{ROOT}", style="dim")
    console.print(f"会话:{resume_note}", style="dim")
    if conflict:
        console.print(conflict, style="yellow")
    _replay_history(history)   # 把上次对话按原样重放一遍，接着聊
    console.print()

    while True:
        try:
            user_input = _read_multiline()  # 多行读取,空行提交
            if user_input is None:  # 空闲时 Ctrl+C = 退出
                console.print("再见。", style="dim")
                break
            text = user_input.rstrip()  # 去掉粘贴时多带的结尾空行,保留行内缩进
            if not text.strip():
                continue
            if text in {"exit", "quit"}:
                break
            # 终端指令(/compact、/reset、/tokens…)。注册在 agent/commands/ 里,
            # 系统提示词中那段说明也由同一份注册表生成,不用两头各维护一遍。
            cmd_result = dispatch_command(text, CommandContext(messages))
            if cmd_result is not None:
                if cmd_result:
                    console.print(cmd_result, style="dim")
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
            rewrite_session(messages)

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
