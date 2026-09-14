"""子 agent:把一件活派出去,干完把结论交回来。

**为什么要有它**:所有活都压在主 agent 的上下文里,是这套东西最不划算的地方 ——
一次安全审计要读几十个文件、跑几个脚本,那些过程**用完就该扔**,可它们会一直躺在
主 agent 的记忆里,越滚越大,直到把上下文撑爆或者把它的判断淹掉。

子 agent 是**会话里独立、可恢复的单元**,不是一次嵌套调用:

    · 有**自己的上下文**(见 ctx.py)—— 它读的所有东西都进它自己那份,不进主 agent 的
    · 有**自己的存储**(sessions/<会话>/tasks/<id>/)—— 完整对话落盘,可追溯、可恢复
    · 有**自己的账号** —— 花了多少 token 单独记,不混进主 agent 的账

**它交回来的东西是有限且结构化的**(结论 / 产出 / 没做成的 / 存疑),不是把上下文倒
回来。倒回来就白做了 —— 省上下文这件事会在交回的那一刻全部还回去。

**挂起**:子 agent 要权限、或者要问用户时,用 `suspend` 把自己挂起来,把问题交给主 agent。
它不"调用"主 agent,而是**把自己停下**。这样同步异步都成立 —— 差别只在主 agent 什么时候
看到那个问题。
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ctx, prompts
from .ctx import FS_ANY, FsGrant
from .core import MAX_STEPS
from . import session as _session

# 子 agent 的步数上限。比主 agent 小一些,但它要跑完一件**完整**的活 —— 实测一次真仓库的
# 安全审计(扫 + 分诊 + 写报告)40 步不够,刚好卡在写报告那一步,等于 95% 的活白做。
# 现在的口径是:撞上限不算完成(见 ctx.truncated),报给主 agent 时明说没干完、可以
# resume 接着做。60 是个折中 —— 还是能被 SUBAGENT_MAX_STEPS 覆盖。
SUB_MAX_STEPS = int(os.environ.get("SUBAGENT_MAX_STEPS", "60"))
# 同时能跑几个。每个都是独立的 API 流 + 独立的知识上下文,不是越多越好。
MAX_CONCURRENT = int(os.environ.get("SUBAGENT_MAX_CONCURRENT", "10"))
# 交回给主 agent 的报告上限(字符)。子 agent 的报告要是比它干的活还长,那这个设计就白搭了。
RESULT_MAX_CHARS = int(os.environ.get("SUBAGENT_RESULT_MAX", "4000"))
# 输出缓冲区保留多少字符(给 /subtasks 看和排错用,不进上下文)
BUFFER_MAX_CHARS = int(os.environ.get("SUBAGENT_BUFFER_MAX", "200000"))
# 等子 agent 时的轮询间隔。**必须带超时** —— 见 _wait_for 里那段说明(不可中断的
# 阻塞锁会让 Ctrl+C 失效,用户以为程序死了)。
WAIT_POLL = 0.25
# 等的时候每隔这么久出一声,让人知道它还活着
WAIT_HEARTBEAT = float(os.environ.get("SUBAGENT_HEARTBEAT", "10"))
# 单次等待的硬上限:再久也先不等了,让它转后台,免得主 agent 被一个卡住的子 agent
# 无限期拖住。跑完照样会通报。
WAIT_MAX = float(os.environ.get("SUBAGENT_WAIT_MAX", "1800"))


# 用户当前"接管"着哪个子 agent(见 attach)。**只有一个** —— 一次看一个才看得清。
_ATTACHED: str | None = None
_ATTACH_LOCK = threading.RLock()
# 进程正在退出。**置上之后一律不再往 stdout 转发** —— 见 shutdown 的说明。
_SHUTTING_DOWN = False


def attached() -> str | None:
    return _ATTACHED


def attach(tid: str) -> str:
    """接管:它的输出实时打到终端,你敲的字进它的对话。"""
    global _ATTACHED
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。"
    with _ATTACH_LOCK:
        _ATTACHED = tid
    return (f"已接管 {tid}({t.status})。它说的话会带 [{tid}] 前缀打到这儿;"
            f"你敲的会进它的对话。回去用 /subtasks off。")


def detach() -> str:
    global _ATTACHED
    with _ATTACH_LOCK:
        was, _ATTACHED = _ATTACHED, None
    return f"已从 {was} 退出来。" if was else "本来就没接管谁。"


class _Sink(io.TextIOBase):
    """子 agent 的输出落进这里,而不是终端。

    主终端是用户跟**主 agent** 对话的地方。子 agent 往那儿刷几百行,用户就看不见主 agent
    在说什么了 —— 这正是当初要解决的问题的一部分,不能从这个门再放回来。

    但**接管之后**(`/subtasks <id> enter`)例外:那时用户明确说了"我要看这个",就实时
    转出去。留一个环形缓冲,一是给接管用(接管的瞬间先补上已经说过的),二是事后可查。
    """

    def __init__(self, tid: str = "", limit: int = BUFFER_MAX_CHARS):
        self.tid = tid
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self._bol = True          # 上一个片段是不是停在行首(决定要不要加前缀)

    def write(self, s: str) -> int:      # noqa: D102 - TextIOBase 的接口
        if not s:
            return 0
        self.parts.append(s)
        self.size += len(s)
        while self.size > self.limit and len(self.parts) > 1:
            self.size -= len(self.parts.pop(0))
        if self.tid and _ATTACHED == self.tid and not _SHUTTING_DOWN:
            # 直接用 sys.stdout,不走 rich:这些片段本来就是 rich 排版好的,
            # 再过一遍渲染只会把格式弄乱。打不出来也不该影响子 agent 干活。
            try:
                import sys
                text = f"[{self.tid}] {s}" if self._bol else s
                self._bol = text.endswith("\n")
                sys.stdout.write(text)
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
        return len(s)

    def text(self) -> str:
        return "".join(self.parts)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _buffer_console(tid: str):
    """给子 agent 的输出出口:写进缓冲区(被接管时才转到终端)。"""
    from rich.console import Console
    return Console(file=_Sink(tid), width=110, highlight=False, soft_wrap=True)


@dataclass
class Task:
    id: str
    prompt: str
    # **它属于哪个会话,派活时就记下来。** 不记的话,`/switch` 之后它的落盘会跟着
    # "当前会话"走 —— 实测:对话进了新会话的目录,原会话只剩一份过期的 meta。
    # 文件放哪不该取决于"此刻谁在前台"。
    session: str = ""
    # queued → running → waiting_input → done / failed / truncated / interrupted → closed
    status: str = "queued"
    vm: bool = False             # 主 agent 有没有把 VM 开给它
    fs: FsGrant = field(default_factory=FsGrant)   # 它能读写哪儿
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    result: str = ""             # 最终报告(交回主 agent 的那段)
    error: str = ""
    ask: str = ""                # 挂起时:它要什么
    usage: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    notifier: object = None      # threading.Event —— 谁在等就谁等
    resumed: list = field(default_factory=list)   # 待注入的回话
    # 有没有人在等这个结果。**派出去的时候就定下来**(而不是等完了再看),这样和
    # 子 agent 线程之间没有竞态:在等 → 结果直接由 dispatch/resume 返回,**不再发通报**,
    # 否则主 agent 会同一份报告收两遍(一遍是返回值,一遍是下一轮的通报)。
    awaited: bool = False
    # 它的运行时上下文(活的才有;从磁盘读回来的没有)。留着是为了能看它的输出缓冲、
    # 用量和"在等什么"。
    ctx: object = None
    thread: object = None        # 它那条线程(退出时要收拢,见 shutdown)

    def meta(self) -> dict:
        """落盘的元信息(不含 messages —— 那个单独一行行追加)。"""
        return {"id": self.id, "prompt": self.prompt, "status": self.status,
                "vm": self.vm, "created": self.created, "updated": self.updated,
                "fs": {"read": list(self.fs.read), "write": list(self.fs.write),
                       "delete": self.fs.delete},
                "result": self.result, "error": self.error, "ask": self.ask,
                "usage": self.usage}

    def brief(self) -> str:
        u = self.usage or {}
        cost = f",输出 {u.get('completion', 0):,} tokens" if u else ""
        return f"{self.id} [{self.status}]{cost} —— {self.prompt[:70]}"


_LOCK = threading.RLock()
_TASKS: dict[str, Task] = {}
_NOTIFY: list[str] = []          # 跑完的子 agent 的通报,等主 agent 下一轮开头收


# ========================= 存储 =========================

def _sid() -> str:
    return _session.current_session_id()


def tasks_root(sid: str | None = None) -> Path:
    return _session.session_dir(sid or _sid()) / "tasks"


def task_dir(tid: str, sid: str | None = None) -> Path:
    return tasks_root(sid) / tid


def _dir(t: Task) -> Path:
    """这个任务的文件该放哪 —— 认它**自己的**会话,不认"当前会话"。"""
    return task_dir(t.id, t.session or _sid())


def _save(t: Task) -> None:
    """落盘:meta 一份、消息一行行追加。

    消息**每次追加都立刻写** —— 崩在中间时,已经跑过的那些步不会丢,恢复时能接着看。
    这和会话本身用的是同一套理由(见 session.py 头部):崩溃/强杀是常态,不是意外。
    """
    try:
        d = _dir(t)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            json.dumps(t.meta(), ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _save_messages(t: Task) -> None:
    """整个对话重写一遍。

    用"重写"而不是"追加":子 agent 的对话是**它自己的**,不需要像主会话那样为崩溃做
    逐行增量保护 —— 真正怕丢的是主 agent 那边。重写顺带解决了压缩后要改历史的问题。
    """
    try:
        d = _dir(t)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "messages.jsonl").open("w", encoding="utf-8") as f:
            for m in t.messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


# ========================= 派活 =========================

def _new_id() -> str:
    with _LOCK:
        n = 1
        while f"t{n}" in _TASKS:
            n += 1
        return f"t{n}"


def _running_count() -> int:
    return sum(1 for t in _TASKS.values() if t.status == "running")


def _overlap(a: tuple, b: tuple) -> bool:
    """两片写范围有没有交集(任一方盖住另一方,就算撞上)。

    **宁可误拦也不要放过**:误拦的代价是"换个范围重派",放过的代价是两个 agent 同时改
    同一批文件,而且改完谁也不知道(不报错,只是结果对不上)。范围语义见
    `FsGrant._within` —— 带 `/` 是目录,不带是文件。
    """
    return any(FsGrant.covers(x, y) or FsGrant.covers(y, x) for x in a for y in b)


def conflict_for(rel: str, exclude: str = "") -> str:
    """`rel` 这块现在归哪个子 agent 管?(没人管就返回空串)

    给 `safe_path` 用:主 agent 要写一块正在被某个子 agent 写的地方时拦下来。
    这是授权划范围的另一半 —— 只约束子 agent 之间的话,主 agent 照样能把它们脚下的
    地板掀了,而且掀完谁也不知道。
    """
    with _LOCK:
        for t in _TASKS.values():
            if t.status != "running" or t.id == exclude:
                continue
            if _within(rel, t.fs.write):
                return t.id
    return ""


def _within(rel: str, scopes: tuple) -> bool:
    return FsGrant._within(rel, scopes)


def _build_messages(t: Task) -> list[dict]:
    """子 agent 的对话开头:它自己的系统提示词(含任务描述和它的技能清单)。"""
    from . import skills
    system = prompts.load(
        "subagent",
        task=t.prompt,
        skills=skills.prompt_section(role="sub"),
    )
    return [{"role": "system", "content": system}]


def dispatch(prompt: str, vm: bool = False, wait: bool = True,
             timeout: float | None = None, fs: FsGrant | None = None) -> dict:
    """派一件活给子 agent。返回一个结果字典(不是字符串 —— 调用方要按状态分支)。"""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"status": "error", "message": "任务描述是空的 —— 子 agent 不知道要干什么。"}
    with _LOCK:
        if _running_count() >= MAX_CONCURRENT:
            busy = "、".join(t.id for t in _TASKS.values() if t.status == "running")
            return {"status": "error",
                    "message": f"同时在跑的子 agent 已经到上限({MAX_CONCURRENT} 个:{busy})。"
                               f"先等它们回来,或者用 task_status 看看进展。"}

    if fs is None:
        # 默认:能读整个工作区,**不能写**。写必须由主 agent 显式划范围 ——
        # 不划就等于让几个并排的子 agent 随便改同一批文件。
        fs = FsGrant(read=(FS_ANY,), write=(), delete=False)
    if fs.write:
        for other in list(_TASKS.values()):
            if other.status == "running" and _overlap(fs.write, other.fs.write):
                return {"status": "error", "message": (
                    f"写范围和工作中的 {other.id} 撞上了(它管 {'、'.join(other.fs.write)},"
                    f"你要 {'、'.join(fs.write)})。两个 agent 同时改一处,撞了不报错、"
                    f"只是结果对不上,事后查不出是谁改的。换个不重叠的范围,或者等它回来。")}
    t = Task(id=_new_id(), prompt=prompt, vm=bool(vm), awaited=bool(wait), fs=fs,
             session=_sid())
    t.notifier = threading.Event()
    with _LOCK:
        _TASKS[t.id] = t
    t.messages = _build_messages(t)
    _save(t)
    _start(t)

    if not wait:
        return {"status": "running", "task_id": t.id}
    return _wait_for(t, timeout)


def _start(t: Task) -> None:
    t.status = "running"
    t.updated = _now()
    _save(t)
    th = threading.Thread(target=_run, args=(t,), name=f"subagent-{t.id}", daemon=True)
    t.thread = th
    th.start()


def _run(t: Task) -> None:
    """子 agent 线程:建自己的上下文、跑自己的循环、把结果落盘。

    **上下文必须在线程内部 use()** —— 新线程不继承父线程的上下文(ContextVar 的规矩),
    忘了这一步,它的图片、搜索配额、用量会落到那个兜底上下文上,等于全部丢失。
    """
    c = ctx.AgentCtx(role="sub", task_id=t.id, label=t.id, vm_grant=t.vm,
                     fs=t.fs, console=_buffer_console(t.id))
    t.ctx = c                                     # 保留引用,供 /subtasks 查状态
    outcome = "done"
    try:
        with ctx.use(c):
            t.result = loop_run(t, c)
        if c.truncated:
            # **不算完成。** 它是被步数上限砍断的 —— 已经做完的部分都在对话和产出里,
            # 但"交回来的报告"根本没写成。报给主 agent 时必须是这个口径,否则它会
            # 拿一份半截的东西去验收。
            outcome = "truncated"
        elif c.cancelled:
            # 叫停(用户 /subtasks kill,或主 agent finish_task stop)。
            # **直接落到 closed**:它没有"结果"可验收,再要一次确认只是多一步。
            outcome = "closed"
        elif c.suspend:
            outcome, t.ask = "waiting_input", str(c.suspend.get("ask") or "")
    except Exception as exc:  # noqa: BLE001 - 子 agent 崩溃不能带走主 agent
        outcome = "failed"
        t.error = f"{type(exc).__name__}: {exc}"
        t.messages.append({"role": "system", "content": "（子 agent 内部出错:\n"
                            + traceback.format_exc()[-2000:] + "）"})
    finally:
        # **状态、落盘、通报按这个顺序走,而且都在最后一起做。** 先设状态再发通报的话,
        # 外面轮询到 status == "done" 的那一刻通报还没入队(pending_notifications 会
        # 返回空)—— 结果就是"后台干完了但主 agent 永远收不到"。发通报放在设状态之后、
        # 唤醒等待者之前,这两个都发生在同一段不可被打断的收尾里。
        t.status = outcome
        t.usage = dict(c.total_usage)
        t.updated = _now()
        _save_messages(t)
        _save(t)
        if outcome == "done" and not t.awaited:
            _notify(t)
        # 花了多少,给**人**看,不给主 agent —— 报告里那句"12 次请求 / 8400 tokens"是
        # 纯账目,主 agent 拿它做不了任何决定,却要为它把整段上下文重发一遍。
        #
        # **必须用 c.console,不能用 ctx.out()**:这段 finally 在 `with ctx.use(c)` **外面**,
        # 那时 ctx.current() 已经不是这个子 agent 了 —— ctx.out() 会解析到**真正的终端**,
        # 于是:① 它的话漏到主终端上(哪怕没被接管);② 一个 daemon 线程在解释器关闭时
        # 写 stdout,会直接触发 "Fatal Python error: could not acquire lock for
        # <_io.BufferedWriter name='<stdout>'>"。两条都踩过。
        try:
            c.console.print(f"{t.id} [{t.status}] {_cost_line(t)}",
                            style="dim", markup=False)
        except Exception:  # noqa: BLE001
            pass
        t.notifier.set()


def loop_run(t: Task, c: ctx.AgentCtx) -> str:
    """跑一轮(把 import 放这儿,避开 registry 自动发现时的循环导入)。"""
    from . import loop
    # 恢复时:把上一轮存下的回话(主 agent 的答复)当成新输入接上去
    payload = t.resumed.pop() if t.resumed else t.prompt
    return loop.run(payload, t.messages, max_steps=SUB_MAX_STEPS)


def _wait_for(t: Task, timeout: float | None) -> dict:
    """等它跑完(或者停下来等回话)。

    **绝对不能写成 `notifier.wait()` 无超时那种等法。** 那在 Windows 上是一把**不可中断**
    的阻塞锁:主线程卡在 C 里,Ctrl+C 送不进去,用户看到的就是"整个程序死了、连打断都
    做不到"。所以这里改成**带超时的轮询**:每 0.25 秒回到一次 Python 字节码,信号才有机会
    被处理 —— 这是 Ctrl+C 能生效的前提。

    等的时候还要**出声**:子 agent 的输出默认一行都不打(那是对的),但"什么都不打"和
    "卡死了"在用户眼里长得一模一样。每 10 秒打一句"还在跑、第几步",人就知道该等还是该停。
    """
    limit = timeout if timeout is not None else WAIT_MAX
    started = time.monotonic()
    deadline = started + limit
    next_beat = started + WAIT_HEARTBEAT
    first = True
    try:
        while True:
            if t.notifier.wait(WAIT_POLL):
                return report(t)
            now = time.monotonic()
            if now >= deadline:
                t.awaited = False       # 不等了 —— 它跑完时按"没人等"处理,发通报
                return {"status": "timeout", "task_id": t.id, "message": (
                    f"{t.id} 跑了 {_dur(now - started)} 还没回来,先不等了 —— "
                    f"它在后台继续,干完会通知你。要看它在忙什么:task_status('{t.id}')。")}
            if now >= next_beat:
                next_beat = now + WAIT_HEARTBEAT
                hint = ("(Ctrl+C 可以不再等它 —— 它会在后台继续跑,用 /subtasks 看)"
                        if first else "")
                first = False
                _beat(t, now - started, hint)
    except BaseException:
        # 被 Ctrl+C 打断(或别的什么掀了):**不再算"有人等"** —— 它跑完照常通报,
        # 否则用户放弃等待之后,那份结果就再也没人告诉他了。
        t.awaited = False
        raise


def _dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s} 秒" if s < 60 else f"{s // 60} 分 {s % 60} 秒"


def _beat(t: Task, elapsed: float, hint: str = "") -> None:
    """等的时候定期出一声。只打终端,不进任何上下文。

    步数取"它说了几句话" —— 第一步没走完时是 0,那会儿只报时间就够了(写"第 0 步"
    看着像出错)。
    """
    steps = sum(1 for m in t.messages if m.get("role") == "assistant")
    where = f",第 {steps} 步" if steps else ""
    try:
        ctx.out().print(f"  … {t.id} 还在跑({_dur(elapsed)}{where}) {hint}",
                        style="dim", markup=False)
    except Exception:  # noqa: BLE001 - 打不出来不该影响等待
        pass


def _notify(t: Task) -> None:
    """跑完了,给主 agent 留一条通报 —— 它下一轮开头会收到。"""
    with _LOCK:
        _NOTIFY.append(t.id)


def pending_notifications() -> list[str]:
    """取走"跑完了但主 agent 还没看到"的通报(取走即清空)。

    只取**当前会话**的:切换会话之后,别的会话的子 agent 干完了,不该报给这边的主
    agent —— 那是另一段对话的事,这边既没有它的上下文、也无从处置。它的结果在它
    自己会话的盘上,回到那个会话就翻得到。
    """
    my = _sid()
    with _LOCK:
        ids, _NOTIFY[:] = list(_NOTIFY), []
    out = []
    for tid in ids:
        t = _TASKS.get(tid)
        if t is None:
            continue
        if t.session and t.session != my:
            # 不是这个会话的 —— **扔回队列**,等回到那个会话再交(不是丢弃)
            with _LOCK:
                _NOTIFY.append(tid)
            continue
        if t.status == "done":
            out.append(report(t)["message"])
    return out


# ========================= 交回结果 =========================

def _clip(text: str, t: Task, limit: int = RESULT_MAX_CHARS) -> str:
    """报告太长就截断 —— 但**说清截了**,并给出去哪儿读全部。

    截断本身没错(交回来的东西不能把刚省下的上下文又填回去),错的是**悄悄截**:
    那样主 agent 会以为它看到的就是全部,拿着半份报告去下结论。
    """
    if len(text) <= limit:
        return text
    where = _dir(t) / "messages.jsonl"
    return (text[:limit]
            + f"\n\n(报告过长,以上是前 {limit} 字,还有 {len(text) - limit} 字没显示。"
              f"全文和它干活的完整过程在 {where} —— 需要细节就去读,不要凭猜。)")


def report(t: Task) -> dict:
    """把任务状态整理成交给主 agent 的一段话。"""
    if t.status == "waiting_input":
        return {"status": "waiting_input", "task_id": t.id, "message": (
            f"[子 agent {t.id} 停下来等你决定]\n{t.ask}\n\n"
            f"它已经做完的部分在它的对话里,没丢。你答完用 "
            f"resume_task('{t.id}', answer='...') 让它接着干。"
        )}
    if t.status == "running":
        # **没有这一支的话会掉到下面那个"完成"分支上去** —— 问一个正在跑的活,回一句
        # "[子 agent t1 完成]"。实测就是这么把主 agent 绕晕的(它以为已经完事了)。
        asked = sum(1 for m in t.messages if m.get("role") == "assistant")
        return {"status": "running", "task_id": t.id, "message": (
            f"[子 agent {t.id} 正在跑,还没结果]已经走了约 {asked} 步。\n"
            f"要看它在忙什么:/subtasks {t.id} enter(接管过去实时看)。\n"
            f"不等它就等通报;要停它用 finish_task('{t.id}', 'stop')。"
        )}
    if t.status == "truncated":
        return {"status": "truncated", "task_id": t.id, "message": (
            f"[子 agent {t.id} **没干完** —— 撞上步数上限被砍断了]\n\n"
            f"它做的那些**都在**(对话和产出文件都在盘上),但**报告没写完** —— "
            f"别把上面那段话当结论。\n\n"
            f"**接着让它干**(推荐):`resume_task('{t.id}', answer='接着把没做完的做完')` "
            f"—— 它带着已有的成果继续,而且会**再拿到一份完整的步数预算**。"
            f"这样它的上下文(读了那么多文件才攒出来的)不浪费。\n"
            f"**别自己接手** —— 那等于把它读过的东西在你的上下文里再读一遍,"
            f"正好把这套设计省下来的东西又花回去(实测发生过)。"
        )}
    if t.status == "failed":
        return {"status": "failed", "task_id": t.id, "message": (
            f"[子 agent {t.id} 失败] {t.error}\n\n"
            f"它的对话落在 {_dir(t) / 'messages.jsonl'},要查原因就去读。"
            f"**别把失败当成「干完了但没结果」** —— 那部分活没做,要重派或者自己接。"
        )}
    if t.status == "interrupted":
        return {"status": "interrupted", "task_id": t.id, "message": (
            f"[子 agent {t.id} 上次被中断了(进程退出)] 它停在:\n{t.ask or '(未知步骤)'}\n\n"
            f"那个**正在做的那一步结果未知**(可能已经产生了副作用),但之前完成的步骤是好的。"
            f"你决定:resume_task('{t.id}') 让它从断点接着干(中断那步会重做),"
            f"还是自己接手、或者放弃。"
        )}
    if t.status == "done":
        return {"status": "done", "task_id": t.id, "message": (
            f"[子 agent {t.id} 完成]\n\n{_clip(t.result, t)}"
        )}
    # 剩下的(closed 之类)是已经收场的。**必须显式写出来** —— 原来这里是无条件兜底,
    # 于是新加的状态会**悄悄掉进"完成"那一支**:问一个正在跑的活,回一句"已完成"
    # (实测把主 agent 绕晕过)。状态多起来之后,"兜底"就是个陷阱。
    return {"status": t.status, "task_id": t.id, "message": (
        f"{t.id} 现在处于 {t.status},已经收场了。/subtasks all 能翻到它。"
    )}


def _cost_line(t: Task) -> str:
    u = t.usage or {}
    return (f"{u.get('requests', 0)} 次请求 / 输出 {u.get('completion', 0):,} tokens"
            f" / 缓存命中 {_rate(u)}")


def _rate(u: dict) -> str:
    p = u.get("prompt", 0)
    return f"{u.get('cache_hit', 0) / p * 100:.1f}%" if p else "—"


# ========================= 恢复 =========================

def resume(tid: str, answer: str = "", wait: bool = True,
           timeout: float | None = None) -> dict:
    """把主 agent 的答复交给一个挂起(或中断)的任务,让它接着干。"""
    t = _TASKS.get(tid) or _load(tid)
    if t is None:
        return {"status": "error", "message": f"没有 {tid} 这个任务。"}
    if t.status not in ("waiting_input", "interrupted", "truncated"):
        return {"status": "error",
                "message": f"{tid} 现在的状态是 {t.status},不在等回话 —— 没什么可接着干的。"}
    t.awaited = bool(wait)
    t.resumed.append(answer or "(主 agent 没有给具体答复,按你的判断继续;拿不准就再挂起问一次)")
    t.ask = ""
    t.notifier = threading.Event()
    _start(t)
    return _wait_for(t, timeout) if wait else {"status": "running", "task_id": t.id}


def tell(tid: str, text: str) -> str:
    """跟一个子 agent 说句话(接管状态下用户敲的)。

    **两种情况走的路完全不同,但都成立**:

    - 它**停着**(挂起/干完了/中断了)→ 当成 `resume` 的答复,让它带着这句话接着干。
    - 它**正在跑** → 直接把这句话追加进它的对话。它每一步都会重发整段历史,所以下一轮
      就看见了。不用中断它,也不用等 —— 这正是"每一步都重发历史"这个代价换来的好处。

    跑着的时候插话有个前提**:它下一步才看得到**,而且它可能正卡在一次很长的工具调用里。
    所以话要说得像"补充信息",别指望它立刻掉头。
    """
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。"
    text = (text or "").strip()
    if not text:
        return "(空话,没发)"
    if t.status == "running":
        t.messages.append({"role": "user", "content": text})
        return f"已插进 {tid} 的对话,它下一步会看到。"
    if t.status in ("waiting_input", "interrupted", "done", "closed", "truncated"):
        # 干完了还能接着聊:把话续在结论之后,等于"再让它做一件事"。
        t.resumed.append(text)
        t.ask = ""
        t.notifier = threading.Event()
        # 这次是**用户**直接交代的,主 agent 不在等;所以按"没人等"处理 ——
        # 它跑完会照常通报给主 agent(主 agent 是协调者,该知道下面又发生了什么)。
        t.awaited = False
        _start(t)
        return f"{tid} 接着跑了(在它之前那段的后面接着做)。" if t.messages else f"{tid} 跑起来了。"
    return f"{tid} 现在是 {t.status},没法接话。"


def finish(tid: str, verdict: str = "accept", note: str = "") -> str:
    """**验收并处置**一个子 agent —— 它干完之后,由主 agent 决定怎么收场。

    三种结局,对应主 agent 拿到报告之后真正会做的三种判断:

    - `accept` —— 验收通过。标记关闭、从活跃列表里移走(对话和产出**都还在盘上**,
      用户之后有疑问仍然翻得到),并释放它占的内存(那个输出缓冲区不小)。
    - `rework` —— 打回重做。把 note 当成新指令续给它,它带着意见接着干。
    - `stop` —— 不要了。叫停并关闭。

    **为什么关闭不能是自动的**:子 agent 说"干完了"不等于活干好了。中间隔着一层
    概括,而概括可能漏、可能偏。让它自己消失,等于默认它说的就是真的 —— 那正是
    这套设计一直在防的事(看着像结论的东西)。
    """
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。现有:{listing()}"
    verdict = (verdict or "accept").strip().lower()

    if verdict in ("accept", "ok", "done"):
        if t.status == "running":
            return (f"{tid} 还在跑,没法验收。等它停下来(它每步之间会检查中止信号,"
                    f"或者你等它跑完),再看 /subtasks {tid}。")
        t.status = "closed"
        t.updated = _now()
        t.ctx = None                  # 缓冲区不小,关了就别留着
        _save(t)
        return (f"已验收关闭 {tid}。它的对话和产出都还在"
                f"({_dir(t)}),用户之后有疑问翻得到。")

    if verdict in ("rework", "redo"):
        if not note.strip():
            return (f"打回 {tid} 得说清**哪里不行、要它怎么改** —— 空着打回,"
                    f"它只会把同一件事再做一遍。")
        if t.status == "running":
            return f"{tid} 还在跑,等它停下来再打回。"
        t.resumed.append(f"[主 agent 看过你的结果,打回重做]\n{note.strip()}")
        t.ask = ""
        t.notifier = threading.Event()
        t.awaited = False
        _start(t)
        return f"{tid} 已带着你的意见重做。"

    if verdict in ("stop", "kill", "cancel"):
        if t.status == "running" and t.ctx is not None:
            t.ctx.cancelled = True
            return (f"已叫停 {tid} —— 它在当前这一步做完之后停,然后会自动关闭"
                    f"(正在跑的那次工具调用不会被打断)。")
        t.status = "closed"
        t.updated = _now()
        t.ctx = None
        _save(t)
        return f"已关闭 {tid}。"

    return f"不认识的 verdict {verdict!r} —— 只认 accept / rework / stop。"


def kill(tid: str) -> str:
    """让一个子 agent 停下来。

    **只能商量,不能强杀** —— Python 没法从外面干掉一个线程。所以是给它设个旗子,
    它在**每一步之间**check 一次。它要是正卡在一次很长的工具调用里(比如容器里跑着
    60 秒的命令),那一下必须等完 —— 但不会再多走一步。

    要真正的强杀,得把子 agent 做成独立进程,那是另一件事,现在没做。
    """
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。"
    if t.status != "running":
        return f"{tid} 现在不是运行状态({t.status}),不用停。"
    if t.ctx is not None:
        t.ctx.cancelled = True
    return (f"已叫停 {tid} —— 它在**当前这一步**做完之后停,正在跑的工具调用不会被打断。"
            f"看它停没停用 /subtasks {tid}。")


def _load(tid: str) -> Task | None:
    """从磁盘读回来一个任务(进程重启后 _TASKS 是空的)。

    **必须把对话也读回来** —— 只读 meta 的话,resume 是从一个空历史开始的,它之前
    干的活全白做了,而它自己还不知道(它以为接着干,实际从零重来)。这是"看起来在
    恢复、其实在重做",比直接报错难发现得多。
    """
    try:
        meta = json.loads((task_dir(tid) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fields = {k: v for k, v in meta.items() if k in Task.__dataclass_fields__}
    fs = fields.pop("fs", None)
    t = Task(**fields)
    if isinstance(fs, dict):
        # 落盘的是纯字典,读回来要还原成 FsGrant —— 否则它会在 safe_path 里
        # 被当成对象用,属性全不存在,而报错会出现在很远的地方。
        t.fs = FsGrant(read=tuple(fs.get("read") or ()),
                       write=tuple(fs.get("write") or ()),
                       delete=bool(fs.get("delete")))
    t.messages = _repair(_load_messages(tid))
    return t


def _load_messages(tid: str) -> list[dict]:
    out: list[dict] = []
    try:
        text = (task_dir(tid) / "messages.jsonl").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                      # 最后一行可能写了一半(崩溃),跳过就好
    return out


def _repair(messages: list[dict]) -> list[dict]:
    """把**悬空的工具调用**补上一条结果。

    崩溃可能正好发生在"模型已经发出 tool_calls"和"结果写回对话"之间。这时历史里留着
    一个没有配对结果的 assistant 消息,直接发给 API 会 400 —— 恢复出来的任务**第一步就
    卡死**,而且看起来像是"它自己不干了"。

    补的那条要说清是**中断**,不能编一个结果:那一步到底有没有产生副作用(写了文件?
    发了请求?)是未知的,编一个"成功"会让人以为它做过了。
    """
    out: list[dict] = []
    for i, m in enumerate(messages):
        out.append(m)
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        answered = set()                  # 紧跟其后的那串 tool 结果
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            answered.add(messages[j].get("tool_call_id"))
            j += 1
        for c in m["tool_calls"]:
            cid = c.get("id") or ""
            if cid and cid not in answered:
                out.append({
                    "role": "tool", "tool_call_id": cid,
                    "content": "（中断:进程在这一步之后退出了,这个工具到底执行没执行、"
                               "结果是什么,都未知。如果这一步可能有副作用(写文件、发请求、"
                               "改状态),重做之前先确认实际发生了什么。）",
                })
    return out


def recover() -> list[str]:
    """启动时把上次没跑完的任务标成"中断",返回给主 agent 的说明。

    **不自动续跑。** 中断那一步的副作用是未知的(可能已经写了文件、发了请求),
    自动重跑会把它再做一遍。所以只把状态摆正、通报给主 agent,由它决定 ——
    "不卡住"的实质是状态明确 + 知情 + 能一键接着干,不是替它做决定。
    """
    out = []
    root = tasks_root()
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not (d / "meta.json").is_file():
            continue
        t = _load(d.name)
        if t is None:
            continue
        if t.status in ("queued", "running"):
            t.status = "interrupted"
            t.ask = t.ask or "（中断在:正在跑的过程中）"
            _save(t)
        _TASKS[t.id] = t
        if t.status == "interrupted":
            out.append(report(t)["message"])
    return out


# ========================= 查询 =========================

def listing(show_closed: bool = False) -> str:
    """给人/给主 agent 看的任务清单。

    **已关闭的默认不列**:它们已经验收完了,再列只会把真正需要看的挤下去。
    但数量告诉你 —— 免得以为它们消失了(对话和产出都还在盘上)。
    """
    if not _TASKS:
        return "(还没有派过任何子 agent)"
    lines = []
    closed = 0
    my = _sid()
    for tid in sorted(_TASKS, key=lambda k: int(k[1:] or 0)):
        t = _TASKS[tid]
        if t.session and t.session != my:
            continue          # 别的会话的,不在这儿列(回到那个会话再见)
        if t.status == "closed":
            closed += 1
            if not show_closed:
                continue
        line = f"- {t.brief()}"
        if t.status == "waiting_input":
            line += f"\n    在等: {t.ask}"
        lines.append(line)
    if closed and not show_closed:
        lines.append(f"(另有 {closed} 个已验收关闭的,用 /subtasks all 看)")
    return "\n".join(lines) if lines else "(没有活跃的子 agent)"


def shutdown(timeout: float = 3.0) -> str:
    """退出前把还在跑的子 agent 收拢。

    **为什么非收不可**:它们是 daemon 线程,主线程一退出,解释器就开始关停 —— 那时
    还有线程在往 stdout 写的话,Python 会直接抛
    `Fatal Python error: could not acquire lock for <_io.BufferedWriter ...>`,
    用户看到的是崩溃栈而不是正常退出。

    做法是先叫停、再等一小会儿。等不到也不要紧:下面把**转发关掉**(见 `_Sink`),
    剩下的线程就算还活着也碰不到 stdout 了 —— 那才是崩溃的根因,等不等得到只是体面问题。
    """
    global _SHUTTING_DOWN
    with _LOCK:
        live = [t for t in _TASKS.values() if t.status == "running"]
    for t in live:
        if t.ctx is not None:
            t.ctx.cancelled = True
    for t in live:
        th = getattr(t, "thread", None)
        if th is not None and th.is_alive():
            th.join(timeout / max(1, len(live)))
    _SHUTTING_DOWN = True
    return f"退出前收拢了 {len(live)} 个还在跑的子 agent。" if live else ""


def get(tid: str) -> Task | None:
    return _TASKS.get(tid) or _load(tid)


def buffer_text(tid: str) -> str:
    """它到刚才为止的输出(接管时先补一段,不然用户盯着空白不知道它在忙什么)。"""
    t = get(tid)
    if t is None or t.ctx is None:
        return ""
    sink = getattr(getattr(t.ctx, "console", None), "file", None)
    return sink.text() if sink is not None else ""


def show(tid: str) -> str:
    """一个子 agent 的详情(给人看的,不进模型上下文)。

    和 `report()` 的区别:那个是**给主 agent 的交接**,只有结论;这个是**给用户看的**,
    要说清它是什么状态、在等什么、干到哪了 —— 以及怎么接管它。
    """
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。现有:{listing()}"
    lines = [
        f"{t.id}  [{t.status}]  {_cost_line(t)}",
        f"任务: {t.prompt}",
        f"创建: {t.created}   更新: {t.updated}",
        f"权限: 读={'、'.join(t.fs.read) or '无'} 写={'、'.join(t.fs.write) or '无'}"
        f"{' 可删' if t.fs.delete else ''}{'  VM' if t.vm else ''}",
    ]
    if t.ask:
        lines.append(f"**在等**: {t.ask}")
    if t.status == "running":
        lines.append("(正在跑。要盯着它看、或者跟它说话:/subtasks %s enter)" % t.id)
    elif t.status == "waiting_input":
        lines.append("(它停着等人回话。接管过去直接答:/subtasks %s enter)" % t.id)
    elif t.status == "done" and t.result:
        lines.append("\n--- 它交回来的报告 ---\n" + t.result[:1500])
    if t.error:
        lines.append(f"\n出错: {t.error}")
    lines.append(f"\n对话在 {_dir(t) / 'messages.jsonl'}(共 {len(t.messages)} 条);"
                 f"看全过程:/subtasks {t.id} log")
    return "\n".join(lines)


def log(tid: str) -> str:
    return transcript(tid)


def transcript(tid: str) -> str:
    """它的完整对话(给用户看的,不进主 agent 上下文)。"""
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。"
    parts = [f"=== {t.id} [{t.status}] {t.created} ===\n任务: {t.prompt}\n"]
    for m in t.messages:
        role = m.get("role")
        if role == "user":
            parts.append(f"\n>>> 给它的输入\n{m.get('content')}")
        elif role == "assistant":
            if (m.get("content") or "").strip():
                parts.append(f"\n<<< 它说\n{m['content']}")
            for c in (m.get("tool_calls") or []):
                fn = c.get("function", {})
                parts.append(f"  • {fn.get('name')}({fn.get('arguments')})")
        elif role == "tool":
            parts.append(f"    → {(m.get('content') or '')[:500]}")
    return "\n".join(parts)
