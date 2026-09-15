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
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import ctx, prompts
from .ctx import FS_ANY, FsGrant
from .core import CONTAINER_WORKSPACE, scratch_scope
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
# 接管时最多补多少字符的历史(见 replay_text)。它只打到终端、不进模型上下文,
# 但仍然得有个上限:接管一个跑了 40 步的子 agent,不该往屏幕上倒几百行。
REPLAY_MAX_CHARS = int(os.environ.get("SUBAGENT_REPLAY_MAX", "6000"))
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

# 接管时"一段输出安静下来"的判定时长(见 attach_settle)
ATTACH_QUIET = float(os.environ.get("SUBAGENT_ATTACH_QUIET", "1.0"))
# 上一次往终端转发是什么时候 / 有没有还没收尾的输出(见 _Sink.write 与 attach_settle)
_ATTACH_LAST = 0.0
_ATTACH_SETTLE = False


def attach_settle() -> bool:
    """接管的那段输出**安静下来了**没有?是的话返回 True 并收尾(调用方把提示符补回去)。

    子 agent 说的话是**另一条线程**直接写 stdout 的,不像主 agent 的输出那样"打完就轮到
    提示符"。所以主线程等输入时得自己盯着:它不写了,就把提示符补回最后一行。

    **为什么要等安静**:子 agent 一次输出常常连着好几行、还夹着工具调用行。每行都补一次
    提示符,屏幕上会插满 "[t2] > ",根本没法读。等它不写了再补,人一眼就能分清
    "这是它说的""这是该我敲了"。

    一段只收尾一次(收完就把标记清掉);它再开口,会重新开一段。
    """
    global _ATTACH_SETTLE
    if _ATTACHED is None or not _ATTACH_SETTLE:
        return False
    if time.monotonic() - _ATTACH_LAST < ATTACH_QUIET:
        return False
    _ATTACH_SETTLE = False
    return True


def attached() -> str | None:
    return _ATTACHED


def attach(tid: str) -> str:
    """接管:它的输出实时打到终端,你敲的字进它的对话。

    **返回里只留一句"接管了哪个"** —— 怎么用、怎么退出,由 attach_hint() 单独放在
    补出来的那段历史**后面**(见 commands/subtasks.py):提示语要是排在历史前面,
    几十行输出会当场把它顶出屏幕,用户就又不知道该怎么退出来了(实测)。
    """
    global _ATTACHED
    t = get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。"
    with _ATTACH_LOCK:
        _ATTACHED = t.id
    return f"已接管 {t.id}({t.status})。"


def attach_hint() -> str:
    """接管状态下"你现在敲的字去哪儿、怎么出去"。没有接管时返回空串。"""
    tid = _ATTACHED
    if not tid:
        return ""
    return (f"! 你敲的字进 {tid} 的对话,它说的话带 [{tid}] 前缀打在这儿。\n"
            f"! 退出接管:/subtasks off(带斜杠的指令仍归主终端处理,不会发给它)。")


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
    转出去。

    **这里只做转发,不留历史。** 原来它还有一个环形缓冲,用来在接管时补上"它到刚才为止
    的输出" —— 那是个错的选择:缓冲里只有"打到终端上的东西",**你给它的任务、工具返回的
    结果、它交的结论都不在里面**(实测接管过去只补出一行"1 次请求 / 输出 32 tokens");
    更要命的是它挂在**活的上下文**上,进程重启后读回来的任务根本没有它 —— 也就是"回来
    之后再接管,历史是空的"。要重建历史得用**对话**(它逐条落盘,谁来了都拿得到),
    见 replay_text。
    """

    def __init__(self, tid: str = ""):
        self.tid = tid
        self._bol = True          # 上一个片段是不是停在行首(决定要不要加前缀)

    def write(self, s: str) -> int:      # noqa: D102 - TextIOBase 的接口
        global _ATTACH_LAST, _ATTACH_SETTLE
        if not s:
            return 0
        if self.tid and _ATTACHED == self.tid and not _SHUTTING_DOWN:
            # 直接用 sys.stdout,不走 rich:这些片段本来就是 rich 排版好的,
            # 再过一遍渲染只会把格式弄乱。打不出来也不该影响子 agent 干活。
            try:
                import sys
                now = time.monotonic()
                if now - _ATTACH_LAST >= ATTACH_QUIET:
                    # **一段新的输出开始:先把提示符那一行让出来。**
                    # 主线程打完 "[t2] > " 就在等输入了,而我们这条线程直接写的话会糊在
                    # 它后面("[t2] > 需要我做什么?")、并且那一行提示符就此作废 ——
                    # 实测用户得**再敲一次回车**才看见提示符回来(因为他提交空行之后
                    # 主循环才又打了一次)。先换行,让这块自成一段。
                    sys.stdout.write("\n")
                    self._bol = True     # 上面把它断成新的一行了,该重新加前缀
                _ATTACH_LAST = now
                _ATTACH_SETTLE = True    # 有输出还没收尾 —— 提示符等着补回去
                text = f"[{self.tid}] {s}" if self._bol else s
                self._bol = text.endswith("\n")
                sys.stdout.write(text)
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
        return len(s)


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
    # **结果交到主 agent 手上了吗。** 决定它还要不要再被通报一次 —— 主 agent 干等
    # 拿到的(wait=true)不该回头再收一遍自己的通报。只活在内存里:重启之后重新通报
    # 一次是对的(那会儿主 agent 确实不知道)。
    delivered: bool = False
    # 被叫停时算哪种结局。默认 closed(不要了);换会话时改成 interrupted ——
    # 那是**暂停**不是丢弃(切回去还能接着做),两者在报告里说的话完全不同。
    cancel_status: str = "closed"

    def meta(self) -> dict:
        """落盘的元信息(不含 messages —— 那个单独一行行追加)。"""
        return {"id": self.id, "prompt": self.prompt, "status": self.status,
                # **会话要落盘。** 不记的话,重启后读回来的任务 session 是空的,它的落盘
                # (见 _dir)和临时区就跟着"此刻谁在前台"走了 —— 而 Task.session 那段注释
                # 说的正是"文件放哪不该取决于此刻谁在前台"。它当时没落盘,所以只对
                # "派活那一刻"成立;多会话共用工作区时,重启后就可能指到别的会话去。
                "session": self.session,
                "vm": self.vm, "created": self.created, "updated": self.updated,
                "fs": {"read": list(self.fs.read), "write": list(self.fs.write),
                       "delete": self.fs.delete, "scratch": list(self.fs.scratch)},
                "result": self.result, "error": self.error, "ask": self.ask,
                "usage": self.usage}

    def brief(self) -> str:
        u = self.usage or {}
        cost = f",输出 {u.get('completion', 0):,} tokens" if u else ""
        return f"{self.id} [{self.status}]{cost} —— {self.prompt[:70]}"


_LOCK = threading.RLock()
_TASKS: dict[str, Task] = {}


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

    **平时用不到**(消息是产生一条落一条的,见 append_message)。留着是给"历史被改写过"
    的情况:它自己的自动压缩把前半段换成了摘要,那时追加会把压缩前后混在一个文件里。
    """
    try:
        d = _dir(t)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "messages.jsonl").open("w", encoding="utf-8") as f:
            for m in t.messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def append_message(tid: str, msg: dict) -> None:
    """**产生一条就落一条。**

    子 agent 跑起来动辄几十步、几分钟。只在跑完整体写一次的话,中途崩溃/被杀就什么都
    没留下 —— 而"中途被打断"是常态不是意外(和会话那边同一个理由,见 session.py 头部)。
    """
    try:
        d = task_dir(tid, _TASKS[tid].session if tid in _TASKS else None)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            f.flush()
    except (OSError, KeyError, TypeError, ValueError):
        pass


def rewrite_messages(tid: str, messages: list[dict]) -> None:
    """整体重写(只在它自己的历史被改写过时用,如自动压缩)。"""
    try:
        d = task_dir(tid, _TASKS[tid].session if tid in _TASKS else None)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "messages.jsonl").open("w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except (OSError, KeyError, TypeError, ValueError):
        pass


# ========================= 派活 =========================

def _norm(tid: str) -> str:
    """把敲进来的任务号归一:`2`、`T2`、`t2` 都当 `t2`。

    **为什么必须认纯数字**:列表里印的就是 `- t2 [done]`,人眼看进去的是那个 2 ——
    实测用户就是照着敲 `/subtasks 2 enter`,然后收到一句「没有 2 这个任务」。
    这种"我明明看着它写的"的失败最气人,而且用户不会去猜是不是要加个 t。
    """
    s = (tid or "").strip()
    if s.isdigit():
        return "t" + s
    if len(s) > 1 and s[0] in "tT" and s[1:].isdigit():
        return "t" + s[1:]
    return s


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


def _conflicting(scope: tuple, exclude: str = "") -> Task | None:
    """`scope` 这片写范围有没有和**正在跑**的别的子 agent 撞上?(返回那个任务)

    派活和批权限用的是同一份判断 —— 见 `_extend_grant`:批权限要是绕开这道检查,
    就等于给了一个后门,"划范围"这一整套就白做了(主 agent 一句话就能让两个 agent
    同时改同一批文件,而且改完不报错)。
    """
    if not scope:
        return None
    with _LOCK:
        snap = list(_TASKS.values())
    for other in snap:
        if other.id == exclude or other.status != "running":
            continue
        if _overlap(scope, other.fs.write):
            return other
    return None


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
    """子 agent 的对话开头:**一条共用的系统提示词 + 一条只有它自己的补充**。

    **第一条必须逐字相同 —— 它是所有子 agent 的共同前缀。**
    DeepSeek 的缓存按**前缀**命中,所以提示词里一旦混进每个任务都不一样的东西(任务描述、
    带任务号的临时区路径),从那一行起后面全部重新计费。实测过代价:任务的 `{task}` 原来
    在系统提示词第 8 行,而技能清单在第 83 行 —— **整份提示词 3257 token 里有 3110
    (95%,含技能清单 1823)永远共享不到**,每起一个子 agent 白付一遍全价。

    所以这里分成两条:

      · 第一条:规则、工具与权限、报告格式、技能清单 —— **所有子 agent 逐字相同**;
      · 第二条:它的临时区路径(带任务号,每个任务不同)。只有这一小条要全价。
      · 任务描述**不在这里** —— 它已经由 `loop.run` 作为第一条 user 消息发过去了
        (见 loop_run 的 payload),写进系统提示词本来就是在发两遍。

    改动这段时要记住这条不变量,它由 test_scratch.py 里的
    `test_two_subagents_get_the_same_system_prompt` 钉着。
    """
    from . import skills
    shared = prompts.load("subagent", skills=skills.prompt_section(role="sub"))
    own = prompts.load(
        "subagent_scratch",
        # 临时区**要告诉它**:光在授权里给一块地方,它不知道那是干什么用的、也不知道
        # 容器里对应哪个路径,照样会去别处找地方写(实测:倒进了 .pylibs)。
        scratch=t.fs.scratch[0] if t.fs.scratch else ".tmp/(未分配)",
        scratch_in_container=(CONTAINER_WORKSPACE + "/" + t.fs.scratch[0]
                              if t.fs.scratch else "(未分配)"),
    )
    return [{"role": "system", "content": shared},
            {"role": "system", "content": own}]


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
        other = _conflicting(fs.write)
        if other is not None:
            return {"status": "error", "message": (
                f"写范围和工作中的 {other.id} 撞上了(它管 {'、'.join(other.fs.write)},"
                f"你要 {'、'.join(fs.write)})。两个 agent 同时改一处,撞了不报错、"
                f"只是结果对不上,事后查不出是谁改的。换个不重叠的范围,或者等它回来。")}
    # **每块活自带一小块临时区**(永远可写、随便删,见 core.scratch_scope)。
    # 技能脚本要产出中间文件,而它的写范围常常只有"报告目录"那么窄 —— 没有指定地方,
    # 它就会往"唯一能写的地方"倒(实测:一份 28KB 的 raw.json 倒进了 .pylibs,那是
    # 容器里仅有的可写口子)。用 replace 而不是直接改 fs:调用方传进来的那份可能是共用的,
    # 不能顺手给它加上一块草稿纸。
    t = Task(id=_new_id(), prompt=prompt, vm=bool(vm), awaited=bool(wait), fs=fs,
             session=_sid())
    t.fs = replace(t.fs, scratch=(scratch_scope(t.session, t.id),))
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


def _say(c, t: Task) -> None:
    """把它的**成品**打到它自己的输出口上(被接管时实时看到,没接管时进缓冲区)。

    **为什么非打不可**:接管的全部意义是"跟它说话"。可它最后一轮说的话原来只是 return
    出去存进 t.result —— 用户跟它说一句,屏幕上只有一行账目("1 次请求 / 输出 41 tokens"),
    看不到它答了什么。那不叫接管,那叫往门缝里塞纸条。

    打进输出口之后就对了:接着看着的人立刻看到,没接着看的也在缓冲区里
    (`/subtasks <id> log` 或者下次接管时补出来的历史)。

    **不发回主 agent、也不进任何上下文** —— 它照样只把 report() 那份交回去。
    """
    text = (t.result or "").strip()
    if text:
        try:
            c.console.print(f"\n{t.id} >", style="bold cyan", markup=False)
            c.console.print(text, markup=False, highlight=False)
        except Exception:      # noqa: BLE001 - 打不出来不该影响它干活
            pass
    if t.ask:
        # 挂起问话也一样:它停下来等回话,而接管的用户就在旁边看着 —— 不告诉他
        # 他在等什么,他只会觉得它卡住了。
        try:
            c.console.print(f"{t.id} 在等回话:{t.ask}", style="yellow", markup=False)
        except Exception:      # noqa: BLE001
            pass


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
            # 叫停。**落到哪种结局取决于"为什么叫停"**(见 Task.cancel_status):
            #   · 用户 /subtasks kill、主 agent finish_task(stop) → closed(不要了)
            #   · /switch 换会话 → interrupted(**暂停**,切回去还能接着做)
            # 两者在报告里说的话完全不同,不能混成一种。
            outcome = t.cancel_status or "closed"
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
        # 消息是逐条落盘的(见 append_message),收尾不用再整体重写 ——
        # 只有它自己被压缩过的那种情况才需要,那个由 loop 负责调 rewrite_messages
        _save(t)
        # 花了多少,给**人**看,不给主 agent —— 报告里那句"12 次请求 / 8400 tokens"是
        # 纯账目,主 agent 拿它做不了任何决定,却要为它把整段上下文重发一遍。
        #
        # **必须用 c.console,不能用 ctx.out()**:这段 finally 在 `with ctx.use(c)` **外面**,
        # 那时 ctx.current() 已经不是这个子 agent 了 —— ctx.out() 会解析到**真正的终端**,
        # 于是:① 它的话漏到主终端上(哪怕没被接管);② 一个 daemon 线程在解释器关闭时
        # 写 stdout,会直接触发 "Fatal Python error: could not acquire lock for
        # <_io.BufferedWriter name='<stdout>'>"。两条都踩过。
        try:
            _say(c, t)               # 先把它说的话打出来,账目跟在后面当页脚
            c.console.print(f"{t.id} [{t.status}] {_cost_line(t)}",
                            style="dim", markup=False)
        except Exception:  # noqa: BLE001
            pass
        t.notifier.set()


def loop_run(t: Task, c: ctx.AgentCtx) -> str:
    """跑一轮(把 import 放这儿,避开 registry 自动发现时的循环导入)。

    任务描述是**作为第一条 user 消息**进去的(所以系统提示词里不再重复一遍,见
    _build_messages)。恢复时那条消息早就在历史里了,这里接上去的是主 agent 的答复。
    """
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
                t.delivered = True      # 交到主 agent 手上了,别再通报一遍
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


# 这些状态都需要主 agent 过一眼:干完了要验收、被砍断了要决定接着干还是算了、
# 停着等回话要答、失败/中断要处置。
_NEEDS_MAIN = ("done", "truncated", "failed", "waiting_input", "interrupted")


def needs_attention(include_delivered: bool = False) -> list["Task"]:
    """**还没交到主 agent 手上、而且需要它处理**的任务(限本会话)。

    `include_delivered=True` 时把已经通报过的也算进来 —— **只有"重算通报状态"那条路
    (loop.reconcile_announced)需要它**,别的地方都该用默认值。加这个口子是因为它原来
    漏掉了一整个函数:`reconcile_announced` 拿的正是这个集合,而它要干的事恰恰是把某个
    任务的 delivered **从 True 改回 False**(通报被压缩吃掉了,得重报)。可它连那些任务
    都看不见 —— 于是"压缩之后重报"这段逻辑**从来没生效过**:真被吃掉的时候,主 agent
    就再也不知道有个子 agent 干完了(实测对不上就是这样)。

    这是"通报"的正确模型:它**不是一个一次性的消息,而是一个未处理的子 agent**。

    为什么不能是消息:消息进了对话就可能被**压缩掉**。实测会发生的场景 —— 主 agent
    决定"先干完手上的再管它"(那是它该有的判断),两步之后上下文触发自动压缩,那条
    通报正好在要被摘要掉的那一段里。而"这件事还没被处理"**不该跟着消息一起消失**。

    所以每一轮都**重新问一遍**"还有谁等着主 agent",而不是发一次就忘。已经交过的
    (delivered)不重复打扰;`finish_task` 关掉的自然就不在这个集合里了。
    """
    my = _sid()
    with _LOCK:
        snap = list(_TASKS.values())
    return [t for t in snap
            if (not t.session or t.session == my)
            and t.status in _NEEDS_MAIN
            and (include_delivered or not t.delivered)]


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
            f"resume_task('{t.id}', answer='...') 让它接着干。\n\n"
            f"**它要是缺权限,光在 answer 里说「批准」没用** —— 权限在派它的时候就定死了,"
            f"得在 resume_task 里一并给出去(write=/read=/allow_delete=/vm=),"
            f"否则它下一步还是被同一道墙挡住。"
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

def _extend_grant(t: Task, write=None, read=None,
                  allow_delete=None, vm=None) -> str:
    """给一个已经派出去的任务**加**权限。返回拒绝理由,通过则返回空串。

    **为什么批复必须落到这份授权上**:子 agent 挂起说"给我 reports/ 的写权限"时,
    主 agent 回一句"批准"是**没有任何用处的** —— 权限在派活那一刻就定死了,那句话只是
    一段文字,它下一步照样被 safe_path 拒绝。实测(假模型驱动真引擎)就是这样:子 agent
    申请、主 agent 批准、它再写一次、**还是被拒**。那样整个"申请权限"就是个摆设:
    它会以为批过了、主 agent 会以为给过了,而活干不成。

    **只增不减**:这里加的范围是往已有范围上**并**,不是替换。答复是"再给你一块",
    不是"改成这一块" —— 后者会让一次批准悄悄收走它原有的权限,而双方都以为只是在放宽。

    **必须过重叠检查**:批准是主 agent 说的,但"两个 agent 同改一处、撞了不报错"这件事
    不因为谁批准就消失。走和派活同一道闸门(见 _conflicting),批权限才不是后门。
    """
    add_write = tuple(write or ())
    add_read = tuple(read or ())
    if add_write:
        merged = tuple(t.fs.write) + add_write
        other = _conflicting(merged, exclude=t.id)
        if other is not None:
            return (f"不能给它 {'、'.join(add_write)}:和工作中的 {other.id} 撞上了"
                    f"(它管 {'、'.join(other.fs.write)})。两个 agent 同时改一处,撞了"
                    f"不报错、只是结果对不上,事后查不出是谁改的。要么等它回来,"
                    f"要么给个不重叠的范围。")
    if vm:
        # VM 是主 agent 和所有子 agent 共用的**一台**。同一时刻只该有一个在用它
        # 跑东西(命令之间会互相踩),所以这里不排队、直接拒 —— 让主 agent 决定。
        holder = next((x.id for x in list(_TASKS.values())
                       if x.id != t.id and x.vm and x.status == "running"), "")
        if holder and not t.vm:
            return (f"不能把 VM 开给 {t.id}:{holder} 正在用它。VM 是共用的一台,"
                    f"两个子 agent 同时在里面跑命令会互相踩(而且看不出是谁踩的)。")

    if add_write:
        t.fs.write = tuple(t.fs.write) + add_write
    if add_read:
        t.fs.read = tuple(t.fs.read) + add_read
    if allow_delete is not None:
        t.fs.delete = bool(allow_delete)
    if vm:
        t.vm = True
    _save(t)
    return ""


def resume(tid: str, answer: str = "", wait: bool = True,
           timeout: float | None = None, write: list | None = None,
           read: list | None = None, allow_delete: bool | None = None,
           vm: bool = False) -> dict:
    """把主 agent 的答复交给一个挂起(或中断)的任务,让它接着干。

    `write` / `read` / `allow_delete` / `vm` 是**答复里附带的授权**:它挂起说"我没权限",
    这里就得真的把权限给它(见 _extend_grant)。不附带授权时,答复只是一段话 —— 那对
    "它不是缺权限、只是拿不准"这种挂起是对的,对"它缺权限"那种就没用。
    """
    t = get(tid)                      # 认 2 / T2 / t2(见 _norm)
    if t is None:
        return {"status": "error", "message": f"没有 {tid} 这个任务。"}
    if t.status not in ("waiting_input", "interrupted", "truncated"):
        return {"status": "error",
                "message": f"{tid} 现在的状态是 {t.status},不在等回话 —— 没什么可接着干的。"}
    why = _extend_grant(t, write=write, read=read,
                        allow_delete=allow_delete, vm=vm)
    if why:
        # **授权给不出去就不别让它跑。** 让它带着"批准了"的错觉继续干,它会在同一个
        # 地方再撞一次墙,而这一次没人知道为什么 —— 宁可现在把话退回去。
        return {"status": "error", "message": f"没能扩权,{t.id} 还停在原地等你:{why}"}
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
        c = t.ctx
        if c is not None:
            # **不能直接往 t.messages 里塞。** 它现在可能正卡在"assistant(带 tool_calls)
            # 已写、tool 结果还没写"的中间态(一次 vm_run 能有好几十秒),这时插一条
            # user 进去,下一次请求的历史就是非法的 —— 它只会表现为"莫名失败"。
            # 排进队列,由它在下一步开头自己取走(见 loop._take_pending)。
            c.pending_input.append(text)
            return f"已插进 {tid} 的对话,它下一步会看到。"
        # 极窄的窗口:刚 _start 完、线程还没建出自己的上下文。这时它还没跑第一步,
        # 历史末尾是干净的(不会切在 tool 配对中间),直接追加是安全的。
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


def _cancel_session(sid: str, status: str) -> list[Task]:
    """把一个会话里所有还在跑的**叫停**,返回被叫停的那几个。

    叫停是**商量式的**(和 kill 一样:线程没法从外面强杀,只能设个旗子让它每步之间自己
    停)—— 所以这里只是"通知到了",它们会在当前那一步做完之后才落到 `status`。

    **落到哪种结局由调用方给**:同样是叫停,「换会话」(暂停)和「重置会话」(作废)的
    意思完全不同,见下面两个函数。
    """
    with _LOCK:
        live = [t for t in _TASKS.values()
                if t.status == "running" and t.session == sid]
    for t in live:
        t.cancel_status = status
        if t.ctx is not None:
            t.ctx.cancelled = True
    return live


def stop_for_switch(sid: str) -> int:
    """换会话时把一个会话里所有还在跑的叫停,返回叫停了几个。

    标 interrupted 而不是 closed:这些活没做完、也不该被丢掉,只是**不该在一个你已经
    离开的会话里继续烧 token**。切回去时主 agent 会被问到,可以 resume。
    """
    return len(_cancel_session(sid, "interrupted"))


def clear_for_reset(sid: str | None = None) -> str:
    """会话重置时把子 agent 一并收掉,返回给用户看的一句说明(没有就返回空串)。

    **为什么必须收**:对话被丢掉了,挂在它下面的活还留着的话 —— 跑着的继续烧 token
    (花在一段已经不要了的对话上),干完的继续按「待办」通报进一段**全新的**对话。
    实测就是这个现象:`/new` 之后 `/subtasks` 里还是 `t1 [running]`、`t9 [done]`,
    而下一步请求立刻被那条通报打扰。

    **和 `/switch` 的区别**:那边是**暂停**(切回去还能接着做),这里是**作废** ——
    对话都不要了,没什么可回去的。所以标 closed 而不是 interrupted,否则刚清空的对话
    马上会收到一条"某个子 agent 被中断了,可以 resume"。标成 closed 之后,它也就不再
    出现在 `/subtasks` 的清单里、不再进 needs_attention。

    它的对话和产出**不删**(在 sessions/<会话>/tasks/<id>/ 下)—— 这一条和 /reset 的
    其余部分一致:只清对话,不动文件。
    """
    sid = sid or _sid()
    with _LOCK:
        # 没记会话的那些(老数据)算当前会话的,和 needs_attention 同一个口径
        mine = [t for t in _TASKS.values()
                if (not t.session or t.session == sid) and t.status != "closed"]
    stopping = [t for t in mine if t.status == "running"]
    for t in stopping:
        t.cancel_status = "closed"
        if t.ctx is not None:
            t.ctx.cancelled = True
    closing = [t for t in mine if t.status != "running"]
    for t in closing:
        t.status = "closed"
        t.updated = _now()
        t.ctx = None
        _save(t)

    if _ATTACHED and any(t.id == _ATTACHED for t in mine):
        detach()          # 正接管着其中一个 —— 它没了,接管也得退出来

    parts = []
    if stopping:
        parts.append(f"{len(stopping)} 个还在跑的已通知它们停"
                     f"(做完当前这一步就停:{'、'.join(t.id for t in stopping)})")
    if closing:
        parts.append(f"{len(closing)} 个已经收场的关掉了"
                     f"({'、'.join(t.id for t in closing)})")
    if not parts:
        return ""
    return "子 agent 一并收掉了:" + ";".join(parts) + "。它的对话和产出还在盘上,没有删。"


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
        # **临时区也要一起还原**:漏了的话,读回来的任务续跑时写自己的草稿纸会被拒,
        # 而它上一步明明刚往那儿写过(现象是"接着干就莫名没权限了")。
        t.fs = FsGrant(read=tuple(fs.get("read") or ()),
                       write=tuple(fs.get("write") or ()),
                       delete=bool(fs.get("delete")),
                       scratch=tuple(fs.get("scratch") or ()))
        if not t.fs.scratch:
            # 老数据(没有 scratch 字段)按规矩补一块。会话取它自己的,取不到就按当前会话 ——
            # 和 _dir() 同一个口径(那边也是 `t.session or _sid()`)。
            t.fs = replace(t.fs, scratch=(scratch_scope(t.session or _sid(), t.id),))
    t.messages = _load_messages(tid)
    # 悬空的工具调用补上说明 —— 不补的话,续跑的第一步就是 400
    _session.repair_dangling(t.messages)
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
    return _TASKS.get(_norm(tid)) or _load(_norm(tid))


def replay_text(tid: str, limit: int | None = None) -> str:
    """接管时补出来的那段"它到刚才为止干了什么" —— **从对话重建,不是终端输出缓冲。**

    三件事决定了必须走对话:

      · 终端缓冲里只有"打到终端上的东西"(它说的话、工具调用行、账目行),**你给它的
        任务、工具返回的结果、它交的结论都不在里面** —— 实测补出来是孤零零一行
        "t2 [done] 1 次请求 / 输出 32 tokens",跟没补一样。
      · 那个缓冲挂在**活的上下文**上。**进程重启后读回来的任务没有它**(ctx 是 None)——
        也就是用户说的"回来之后接管,历史是空的"。
      · 对话是**逐条落盘**的(见 append_message):活着的、从盘上读回来的,拿到的都是
        同一份完整记录,连"重做时被打断的那一步结果未知"这种补写都在里面。

    太长就只补最后一段 —— 一次接管不该往屏幕上倒几百行,但必须说清"还有多少、
    去哪儿看全的",不能让人以为这就是全部。
    """
    t = get(tid)
    if t is None:
        return ""
    # 上限在这里取(而不是当默认参数写死)—— 写死的话这个常量就改不动了,
    # 测试也没法用一个短上限去验"截断"这条分支
    limit = REPLAY_MAX_CHARS if limit is None else limit
    body = transcript(tid)
    if len(body) <= limit:
        return body
    cut = body[-limit:]
    nl = cut.find("\n")
    if nl >= 0:
        cut = cut[nl + 1:]               # 别从一行的中间切开
    return (f"(它的对话很长,这里只补最后一段。完整的那份:/subtasks {t.id} log)\n\n" + cut)


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
        (f"临时区: {'、'.join(t.fs.scratch)}(中间产物放这儿,不动交付目录)"
         if t.fs.scratch else "临时区: 无"),
    ]
    if t.ask:
        lines.append(f"在等: {t.ask}")
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
