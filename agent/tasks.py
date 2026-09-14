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
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ctx, prompts
from .ctx import FS_ANY, FsGrant
from .core import MAX_STEPS
from . import session as _session

# 子 agent 的步数上限:比主 agent 小。它只干一件事,不该跑到 50 步 —— 真跑到那么多,
# 多半是跑偏了,早停早省。
SUB_MAX_STEPS = int(os.environ.get("SUBAGENT_MAX_STEPS", "40"))
# 同时能跑几个。每个都是独立的 API 流 + 独立的知识上下文,不是越多越好。
MAX_CONCURRENT = int(os.environ.get("SUBAGENT_MAX_CONCURRENT", "10"))
# 交回给主 agent 的报告上限(字符)。子 agent 的报告要是比它干的活还长,那这个设计就白搭了。
RESULT_MAX_CHARS = int(os.environ.get("SUBAGENT_RESULT_MAX", "4000"))
# 输出缓冲区保留多少字符(给 /subtasks 看和排错用,不进上下文)
BUFFER_MAX_CHARS = int(os.environ.get("SUBAGENT_BUFFER_MAX", "200000"))


class _Sink(io.TextIOBase):
    """子 agent 的输出落进这里,而不是终端。

    主终端是用户跟**主 agent** 对话的地方。子 agent 往那儿刷几百行,用户就看不见主 agent
    在说什么了 —— 这正是当初要解决的问题的一部分,不能从这个门再放回来。
    留一个环形缓冲:够事后查(和以后"切进去看"),又不会无限涨。
    """

    def __init__(self, limit: int = BUFFER_MAX_CHARS):
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0

    def write(self, s: str) -> int:      # noqa: D102 - TextIOBase 的接口
        if not s:
            return 0
        self.parts.append(s)
        self.size += len(s)
        while self.size > self.limit and len(self.parts) > 1:
            self.size -= len(self.parts.pop(0))
        return len(s)

    def text(self) -> str:
        return "".join(self.parts)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _buffer_console():
    """给子 agent 的输出出口:写进缓冲区,不碰终端。"""
    from rich.console import Console
    return Console(file=_Sink(), width=110, highlight=False, soft_wrap=True)


@dataclass
class Task:
    id: str
    prompt: str
    status: str = "queued"       # queued / running / waiting_input / done / failed / interrupted
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


def _save(t: Task) -> None:
    """落盘:meta 一份、消息一行行追加。

    消息**每次追加都立刻写** —— 崩在中间时,已经跑过的那些步不会丢,恢复时能接着看。
    这和会话本身用的是同一套理由(见 session.py 头部):崩溃/强杀是常态,不是意外。
    """
    try:
        d = task_dir(t.id)
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
        d = task_dir(t.id)
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
    t = Task(id=_new_id(), prompt=prompt, vm=bool(vm), awaited=bool(wait), fs=fs)
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
    th.start()


def _run(t: Task) -> None:
    """子 agent 线程:建自己的上下文、跑自己的循环、把结果落盘。

    **上下文必须在线程内部 use()** —— 新线程不继承父线程的上下文(ContextVar 的规矩),
    忘了这一步,它的图片、搜索配额、用量会落到那个兜底上下文上,等于全部丢失。
    """
    c = ctx.AgentCtx(role="sub", task_id=t.id, label=t.id, vm_grant=t.vm,
                     fs=t.fs, console=_buffer_console())
    t.ctx = c                                     # 保留引用,供 /subtasks 查状态
    outcome = "done"
    try:
        with ctx.use(c):
            t.result = loop_run(t, c)
        if c.suspend:
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
        try:
            ctx.out().print(f"{t.id} [{t.status}] {_cost_line(t)}",
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
    """等它跑完(或者停下来等回话)。"""
    ok = t.notifier.wait(timeout)
    if not ok:
        t.awaited = False      # 不接着等了 —— 它跑完时按"没人等"处理,发通报
        return {"status": "timeout", "task_id": t.id,
                "message": f"{t.id} 还没回来。用 task_status('{t.id}') 看进展。"}
    return report(t)


def _notify(t: Task) -> None:
    """跑完了,给主 agent 留一条通报 —— 它下一轮开头会收到。"""
    with _LOCK:
        _NOTIFY.append(t.id)


def pending_notifications() -> list[str]:
    """取走"跑完了但主 agent 还没看到"的通报(取走即清空)。"""
    with _LOCK:
        ids, _NOTIFY[:] = list(_NOTIFY), []
    out = []
    for tid in ids:
        t = _TASKS.get(tid)
        if t is not None and t.status == "done":
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
    where = task_dir(t.id) / "messages.jsonl"
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
    if t.status == "failed":
        return {"status": "failed", "task_id": t.id, "message": (
            f"[子 agent {t.id} 失败] {t.error}\n\n"
            f"它的对话落在 {task_dir(t.id) / 'messages.jsonl'},要查原因就去读。"
            f"**别把失败当成「干完了但没结果」** —— 那部分活没做,要重派或者自己接。"
        )}
    if t.status == "interrupted":
        return {"status": "interrupted", "task_id": t.id, "message": (
            f"[子 agent {t.id} 上次被中断了(进程退出)] 它停在:\n{t.ask or '(未知步骤)'}\n\n"
            f"那个**正在做的那一步结果未知**(可能已经产生了副作用),但之前完成的步骤是好的。"
            f"你决定:resume_task('{t.id}') 让它从断点接着干(中断那步会重做),"
            f"还是自己接手、或者放弃。"
        )}
    return {"status": "done", "task_id": t.id, "message": (
        f"[子 agent {t.id} 完成]\n\n{_clip(t.result, t)}"
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
    if t.status not in ("waiting_input", "interrupted"):
        return {"status": "error",
                "message": f"{tid} 现在的状态是 {t.status},不在等回话 —— 没什么可接着干的。"}
    t.awaited = bool(wait)
    t.resumed.append(answer or "(主 agent 没有给具体答复,按你的判断继续;拿不准就再挂起问一次)")
    t.ask = ""
    t.notifier = threading.Event()
    _start(t)
    return _wait_for(t, timeout) if wait else {"status": "running", "task_id": t.id}


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

def listing() -> str:
    """给人/给主 agent 看的任务清单。"""
    if not _TASKS:
        return "(还没有派过任何子 agent)"
    lines = []
    for tid in sorted(_TASKS, key=lambda k: int(k[1:] or 0)):
        t = _TASKS[tid]
        line = f"- {t.brief()}"
        if t.status == "waiting_input":
            line += f"\n    在等: {t.ask}"
        lines.append(line)
    return "\n".join(lines)


def get(tid: str) -> Task | None:
    return _TASKS.get(tid) or _load(tid)


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
