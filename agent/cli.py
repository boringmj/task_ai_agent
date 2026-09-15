from __future__ import annotations

import atexit
import queue
import sys
import threading
from datetime import datetime

from rich.markdown import Markdown

from . import prompts
from .core import (
    AUTO_COMPACT_RATIO,
    ROOT,
    SCRATCH_MAX_AGE_DAYS,
    SESSION_RESUME_CHARS,
    SESSION_RESUME_MESSAGES,
    TRASH_MAX_AGE_DAYS,
    console,
    purge_scratch,
    scratch_scope,
)
from .ctx import FsGrant
from .commands import Context as CommandContext
from .commands import all_commands, dispatch as dispatch_command
from .commands import system_message as commands_system_message
from . import skills
from . import ctx
from . import session
from . import tasks
from .llm import usage_line
from . import loop
from .loop import load_system_prompt, run
from .session import (
    claim_owner,
    current_session_id,
    load_session,
    release_owner,
    session_note as session_note_text,
    touch_session,
)
from .tools.container import docker_cleanup_stale, docker_health
from .tools.memory import memory_text
from .tools.trash import purge_trash
from .tools.vm import (VM_AUTOSTART, vm_autostart_note, vm_kickoff, vm_state_get,
                       session_reset_hint, vm_status)


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
            # 程序自己插进对话的那些(压缩摘要、子 agent 通报)**不是用户打的字**。
            # 它们以 user 身份进对话是必要的(否则模型不当回事),但重放时按 `你 >`
            # 打出来就成了"用户说过这句话" —— 实测屏幕上会冒出一行
            # 「你 > (以上对话已压缩…」。所以按前缀分开渲染(见 session.SYSTEM_TAG)。
            if session.is_system_message(m):
                body = text[len(session.SYSTEM_TAG):].strip()
                console.print("系统 > ", style="bold magenta", end="")
                console.print(body, style="dim", markup=False)
            else:
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


def _render_reply(reply: str) -> None:
    """把模型这一轮的答复渲染出来(用户发起的轮次和被子 agent 叫醒的轮次共用)。"""
    console.print("AI >", style="bold green")
    # Markdown 要拿到完整文本才能正确解析,所以是等模型说完再一次性渲染
    console.print(Markdown(reply) if reply.strip() else "(模型没有返回内容)")
    line = usage_line()
    if line:
        console.print(line, style="dim")
    console.print()


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
    # 临时区同理:它本来是一次性的,但**不立刻删** —— 出事了要回头看当时产出的中间
    # 文件,那正是排错时最想要的东西。所以和回收站一样按天清。
    try:
        note = purge_scratch(SCRATCH_MAX_AGE_DAYS)
        if note:
            console.print(f"临时区:{note}", style="dim")
    except Exception as exc:  # noqa: BLE001
        console.print(f"临时区清理失败(不影响使用):{exc}", style="dim")


_EXIT_WORDS = {"exit", "quit"}      # 裸敲也等价于 /exit,两处用法见下
# 等输入时的轮询间隔:每这么久醒一次,看有没有子 agent 的事要处理。
# 别调太小(白烧 CPU),也别太大(叫醒不及时)。
_IDLE_POLL = 0.4


def _has_pending_work() -> bool:
    """有没有"不用等用户、现在就能处理"的事(后台子 agent 出结果了)。"""
    try:
        return bool(tasks.needs_attention())
    except Exception:  # noqa: BLE001 - 判断失败不该拦住读输入
        return False


class _Idle:
    """哨兵:没读到输入,但该先回去看看子 agent 有没有事。"""

    def __repr__(self) -> str:      # pragma: no cover - 只为调试好看
        return "_IDLE"


_IDLE = _Idle()
_INPUT: "queue.Queue" = queue.Queue()


def _start_input_reader() -> None:
    """把 stdin 的读取挪进一条后台线程。

    **为什么非得这样**:主 agent 空闲时要能被子 agent 叫醒。而在这之前,主线程是**阻塞
    在 `sys.stdin.readline()` 里的** —— 卡在系统调用上,子 agent 干完了也叫不动它,只能
    干等用户下次敲键盘。把读取挪到线程里,主线程就能"等输入,但每隔一会儿醒一次,
    看看有没有子 agent 的事要处理"。

    读取线程只负责把整行塞进队列,**不打印任何东西**,也不做任何解析 —— 提示符、
    多行规则、Ctrl+C 全留在主线程(不然两条线程都会碰终端)。
    """
    def run() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001 - 读崩了就当作 EOF
                line = ""
            _INPUT.put(None if line == "" else line)
            if line == "":
                return

    threading.Thread(target=run, daemon=True, name="stdin-reader").start()


def _take_line(timeout: float):
    """等一行输入,超时返回 _IDLE(表示"可以先去看看别的事")。"""
    try:
        return _INPUT.get(timeout=timeout)
    except queue.Empty:
        return _IDLE


def _read_multiline(prompt: str = "你 > "):
    """读取一段输入,空行提交 —— 支持粘贴多行并保留换行。

    console.input() 只读单行,没法粘贴多行代码/文本。改为逐行读取,
    用户输入完(或粘贴完)按一个空行结束。空行只作提交信号,不会进消息。

    例外:**第一行本身就是终端指令**(以 / 开头)或裸的 exit/quit 时,回车即执行,
    不必再敲空行 —— 指令天生是单行的,逼人多按一次回车只会烦人。

    返回:输入文本 / `None`(该退出)/ `_IDLE`(先别等了,回去处理子 agent 的事)。
    """
    lines: list[str] = []
    try:
        # markup=False:**提示符里会有方括号**(接管时是 "[t2] > ")。开着 markup 的话
        # rich 会把 `[t2]` 当样式标记、**静默吃掉** —— 实测用户看到的提示符是一个光秃秃的
        # " > ",完全看不出自己正在接管某个子 agent,于是也就不知道该怎么退出来。
        console.print(prompt, style="bold cyan", end="", markup=False)
        while True:
            line = _take_line(_IDLE_POLL)
            if line is _IDLE:
                # 等输入的时候定期醒一次,有子 agent 的事就让它插进来。
                #
                # **判断的是"有没有已经按过回车的完整行",不是"用户是不是正在打字"。**
                # 读取线程用的是 readline(),字符在回车之前缓冲在 C 层 —— 这里根本看不见。
                # 所以真正的保护是:用户已经提交过一行(多行输入的续行)时不让位,免得把
                # 话截断。他**正在敲、还没回车**的那几个字盖不住,那条通报的输出会插进
                # 他的输入行里 —— 这是这套做法的固有粗糙处,要根治得自己实现行编辑
                # (逐字符读 + 退格/方向键/粘贴),为这点收益不值得。
                if not lines and _has_pending_work():
                    return _IDLE
                # **接管时:子 agent 说完一段,提示符得自己回来。**
                # 它是另一条线程直接写 stdout 的 —— 不像主 agent 的输出那样"打完就轮到
                # 提示符"。不补的话用户看到的是:它输出完了,而 `[t2] > ` 不见了,得**再敲
                # 一次回车**才回来(实测)。续行(`+ `)进行中不补,那会儿他正打字。
                if not lines and tasks.attach_settle():
                    console.print(prompt, style="bold cyan", end="", markup=False)
                continue
            if line is None:        # EOF(Ctrl+D / Ctrl+Z)
                sys.stdout.write("\n")
                return None
            if line in {"\n", "\r\n"}:  # 空行 = 提交
                break
            lines.append(line.rstrip("\r\n"))
            if len(lines) == 1 and (lines[0].lstrip().startswith("/")
                                    or lines[0].strip().lower() in _EXIT_WORDS):
                break               # 指令:回车即走,连续行提示符都不打
            # 续行提示符用 ASCII 的 "+",别用省略号 —— 那个符号在终端里基本等于
            # "加载中/思考中",看到它会以为程序正忙、在那儿干等一个不会来的回复。
            console.print("+ ", style="dim", end="", markup=False)
    except KeyboardInterrupt:
        # 空闲时 Ctrl+C = 退出信号(返回 None)。
        # 必须包住整个函数(含提示打印),否则中断落在 console.print 里的
        # os.get_terminal_size() 时(像这次的栈)会从这个函数逃逸,直接崩掉进程。
        # 恢复打印用裸 write(不走 Rich),避免 get_terminal_size 又被中断引发二次异常。
        sys.stdout.write("\n")
        return None
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
    # 先把还在跑的子 agent 叫停并等一小会儿 —— 它们是 daemon 线程,主线程一退
    # 解释器就开始关停,那时还有线程碰 stdout 会直接 Fatal error(见 tasks.shutdown)
    try:
        tasks.shutdown()
    except Exception:  # noqa: BLE001 - 收尾失败不该阻止退出
        pass

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
    with ctx.use(ctx.AgentCtx(role="main", fs=_main_grant())):
        _session_loop()


def _exit_or_detach() -> bool:
    """/exit 被敲下时,到底该退程序还是只退出接管。返回 True = 真的退出。

    **为什么要拦一道**:用户想"退出接管"时最容易试的就是 /exit —— 它原来会直接把
    agent 关掉,而用户以为自己只是从子 agent 里出来了(实测)。退出程序是有代价的动作
    (收尾会话、收拢子 agent),不该被一次误试触发。

    抽成函数是为了**能测**:这段判断原来埋在 `_session_loop` 中间,只有真跑起整个 REPL
    才碰得到,而 REPL 一旦跑起来就没法在测试里断言"它到底退没退"。
    """
    was = tasks.attached()
    if not was:
        return True
    console.print(tasks.detach(), markup=False)
    console.print(f"(刚才在接管 {was} —— /exit 先退到主终端。"
                  f"真要退出程序,再敲一次 /exit。)", style="dim")
    return False


def _main_grant() -> FsGrant:
    """主 agent 的授权:不限制 + 一块**跟着当前会话**走的临时区。

    路径每次现算,不缓存 —— 会话是会变的(`/switch`),而"存下来以后再用"在这个项目里
    踩过三次(见 session.current_session_id 的说明)。切换时由 `cmd_switch` 直接换掉
    上下文里那一份(它知道新会话是谁,而这一层不知道什么时候会切)。
    """
    return FsGrant.for_main(scratch_scope(current_session_id(), "main"))


def _system_messages() -> list[dict]:
    """开场那几条 system 消息(**主 agent 的全部"说明书"**)。

    抽成函数是为了**能测**:它们是"给谁看什么"的装配处,错一条(比如工具指南没带上)
    不会有任何报错,只会让主 agent 看着少了点什么却说不出来。子 agent 那边对应的是
    `tasks._build_messages`。

    **顺序按"多久变一次"排**:角色提示词和工具指南几乎不变,长期记忆和技能清单会变 ——
    越稳的越靠前,变了的那部分才不会把前面的前缀缓存一起打掉。
    """
    messages = [
        {"role": "system", "content": load_system_prompt()},
        # **工具指南单独成一条**(容器、VM、搜索、改文件的规矩、安全边界那一整套)。
        # 它和"你是谁"无关 —— 主 agent 和子 agent 用的是**同一份**,改一处两边都生效。
        # 拆出来之前只长在 system.md 里,**子 agent 看不见**:它们照样要跑容器、要改文件、
        # 要从网页里取东西,不知道那些坑就只能自己踩一遍(见 prompts/tools_guide.md)。
        {"role": "system", "content": prompts.load("tools_guide")},
    ]
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
    return messages


def _session_loop() -> None:
    _make_stdio_forgiving()
    # stdin 的读取挪到后台线程 —— 主线程才有机会「等输入,但定期醒来看子 agent」
    # (见 _start_input_reader 的说明)。必须在任何读输入之前起。
    _start_input_reader()
    messages: list[dict] = _system_messages()
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
    # 虚拟机**默认不跟着会话起来**(见 VM_AUTOSTART):多数会话用不到它,而它是一台
    # 完整机器。要用的时候 vm_start 就行,十几秒。
    try:
        if VM_AUTOSTART:
            vm_kickoff()  # 后台线程启动/配置虚拟机,不阻塞
            ready = vm_state_get()["status"] == "ready"
            console.print(f"VM:{vm_status()}", style="dim" if ready else "yellow")
        else:
            console.print(f"VM:{vm_autostart_note()}", style="dim")
    except Exception as exc:  # noqa: BLE001
        console.print(f"VM:启动失败(不影响 agent)—— {exc}", style="yellow")
    console.print("多行输入用空行提交;执行中 Ctrl+C 取消本轮,空闲时退出。", style="dim")
    console.print("指令:" + "、".join(n for n, *_ in all_commands())
                  + f"(/help 有说明);上下文到 {AUTO_COMPACT_RATIO:.0%} 自动压缩。",
                  style="dim")
    console.print(f"工作区:{ROOT}", style="dim")
    console.print(f"会话:{session_id} — {session_note};{resume_note}", style="dim")
    # 上次进程退出时没跑完的子 agent:标成「中断」。**不自动续跑** —— 中断那一步的
    # 副作用是未知的,由主 agent 决定接着干还是重派。
    #
    # 通报**不在这里塞进对话**:它由 loop 在每一步开头统一注入(见 _inject_notices)。
    # 两边都发就重复了 —— 而 "中断" 的任务同样属于 needs_attention,走那条路自然会被
    # 报上去。这里只打一行给**人**看(启动时就知道有这么回事,不用等敲第一句话)。
    interrupted = tasks.recover()
    for note in interrupted:
        console.print(f"! {note.splitlines()[0]}", style="yellow")
    # **把"已经通报过"这件事从对话历史里恢复回来。** delivered 只在内存里,重启后是空的 ——
    # 不重算的话,用户昨天已经看过、也早就处理过的那些报告会再喊一遍(用户的原话:
    # 已经消费过的消息不要反复吵闹)。历史就在手边,翻得到就不用再喊。
    loop.reconcile_announced(messages)
    _replay_history(history)   # 把上次对话按原样重放一遍，接着聊
    console.print()

    while True:
        try:
            # 接管着某个子 agent 时,提示符换成它 —— 一眼能看出"现在敲的话是说给谁听的"
            watched = tasks.attached()
            # **闲置时被子 agent 叫醒** —— 不用等用户敲键盘。
            # 在等输入的过程中,`_read_multiline` 每隔一会儿回来看一眼:有子 agent
            # 出结果了就返回 _IDLE,那儿我们直接把这一轮跑掉。用户什么都没说,
            # 主 agent 自己就把通报处理了。
            if _has_pending_work():
                # 提示符后面可能已经有用户打了一半的字 —— 先换行,让这一块自成一段,
                # 免得我们的输出和他的输入糊在同一行上。
                console.print()
                console.print("(后台子 agent 有结果,主 agent 主动处理)", style="dim")
                reply = run(None, messages)
                _render_reply(reply)
                continue

            user_input = _read_multiline(f"[{watched}] > " if watched else "你 > ")
            if user_input is _IDLE:
                continue                # 回去处理子 agent 的事
            if user_input is None:      # 空闲时 Ctrl+C / EOF = 退出
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
            # 后台子 agent 的通报**不在这里收** —— 由 loop 在每一步开头收(见
            # loop._inject_notices)。那两个位置看起来一样(都是"用户输入之前"),
            # 但 loop 那个多覆盖一种情况:**主 agent 还在干活时子 agent 干完了** ——
            # 那时它下一步就会看到,不用等用户再敲一次键盘。收在一个地方就够了。
            #
            # 终端指令(/compact、/reset、/tokens…)。注册在 agent/commands/ 里,
            # 系统提示词中那段说明也由同一份注册表生成,不用两头各维护一遍。
            cmd_ctx = CommandContext(messages)
            cmd_result = dispatch_command(text, cmd_ctx)
            if cmd_result is not None:
                # **还在接管里的 /exit:先退到主终端,别把整个程序关掉**(见 _exit_or_detach)
                if "exit" in cmd_ctx.events and not _exit_or_detach():
                    continue
                # 会话被重置 → 顺带说清虚拟机没跟着重置。为什么补在这层:只有装配层
                # 同时认识"会话"和"VM"两边;命令层只声明事实,不去 import VM 模块。
                if "session_reset" in cmd_ctx.events:
                    cmd_result += session_reset_hint()
                if cmd_result:
                    # 用终端默认亮度,别加 dim —— dim 是留给背景信息的(Docker/VM 状态、
                    # 工作区路径那些)。命令结果是用户主动敲的、正要读的内容,/help 一次
                    # 列八条指令,暗色下几乎看不清。
                    #
                    # markup=False:指令的返回文本里**到处是方括号**,而且是当普通文字用的
                    # (「t1 [running]」「/subtasks t1 enter」)。开着 markup 会把它们当样式
                    # 标记:实测 `[running]` 被**静默吃掉**(列表里那个状态就这么没了,不报错),
                    # `[...]` 里带斜杠的则直接抛 MarkupError。指令输出是我们自己拼的纯文本,
                    # 从来没用过富文本标记,所以关掉它只有收益。
                    console.print(cmd_result, markup=False)
                if "session_switched" in cmd_ctx.events:
                    # 切完会话,把新会话的历史按原样重放一遍 —— 只说一句"已切到 X",
                    # 用户看不到里面聊过什么。和启动时的重放共用同一套渲染,长得一样。
                    _replay_history(messages, source="刚切到的会话")
                    # 切过来的这段历史里已经通报过的,别再喊一遍(和启动时同一个理由:
                    # delivered 只活在内存里,而任务属于某个会话,换个会话就该重新对一遍)
                    loop.reconcile_announced(messages)
                if "exit" in cmd_ctx.events:      # /exit、/quit —— 收尾动作由这层做
                    break
                continue

            try:
                reply = run(text, messages)
            except KeyboardInterrupt:
                # 执行中 Ctrl+C:**不回滚,只打断。**
                # 已经说过的话、已经跑完的工具调用全都留着 —— 用户按 Ctrl+C 想停下的是
                # "还在跑的那个东西",不是"把刚才发生的事都抹掉"。
                #
                # 但半截历史里可能留着一个**没有配对结果的 tool_calls**(正好在工具执行
                # 途中被打断),那个直接发给 API 会 400 —— 这就是原来回滚的原因。
                # 对策从"删掉整轮"换成"**补一条说明**":已经发生的照原样留着,被打断的
                # 那一步补一句"结果未知"(不编一个成功 —— 它可能压根没执行)。
                n = session.repair_dangling(messages)
                if n:
                    session.append_messages(messages[len(messages) - n:], current_session_id())
                console.print("\n[已取消 —— 上面已经发生的都留着]", style="bold red")
                continue
            except Exception as exc:  # noqa: BLE001
                # 未预期的错误(渲染、网络、工具内部崩了……)绝不能掀翻整个会话。
                # 同样**不回滚**:理由和上面一样,把已经跑完的丢掉太可惜了,而且用户
                # 往往正是靠那些信息才看得懂出了什么事。修好历史合法性就够了。
                n = session.repair_dangling(messages)
                if n:
                    session.append_messages(messages[len(messages) - n:], current_session_id())
                console.print(f"\n[出错,本轮中止]{type(exc).__name__}: {exc}", style="bold red")
                continue

            # 消息是**产生一条落一条**的(见 loop._persist),所以这里不用再整体落盘 ——
            # 中途被打断时,已经发生过的那些已经在盘上了。**只有历史被改写过的情况**
            # (自动压缩)才需要整体重写,那个由 loop 自己在压缩之后做掉。

            _render_reply(reply)
        except KeyboardInterrupt:
            # 兜底:任何没被上面捕获的 Ctrl+C(例如渲染 Markdown 那一下),
            # 一律干净退出,而不是抛栈崩掉。
            console.print("\n再见。", style="dim")
            break


if __name__ == "__main__":
    main()
