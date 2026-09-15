"""派活的工具:主 agent 用来把一件事交给子 agent,子 agent 用来把问题交回去。

两边是**同一个机制的两头**:主 agent 派下去的是任务,子 agent 交上来的是问题(要权限、
要问用户、拿不准)。所以子 agent 那边没有"调用主 agent"这种动作 —— 它只是**把自己停下**。
"""
from __future__ import annotations

from .registry import tool


@tool(
    description="把一件**具体的事**派给一个子 agent 去做。"
                "**默认派完就回来**(不等它)—— 你接着干别的、或者把这一轮结束掉,把终端"
                "还给用户;它干完了会主动通报给你。要同时铺开好几件事,连着调几次就行。"
                "适用:需要大量阅读或反复试错的活(代码审计、结构分析、质量检查、"
                "去混淆)、能各自独立并行的活、以及你清单里标着 **[子 agent 专用]** 的技能 —— "
                "那些你加载不了,只能派出去。"
                "不适用:一两步就能答完的问题、要跟用户来回商量的对话 —— 那些你自己做更快;"
                "派活的成本是**另起一次完整对话**,小事派出去反而更贵。"
                "**只有下一步非等它的结果不可时,才传 wait=true 干等** —— 那会把你这一轮"
                "卡住,期间派不了第二个活、也没法把终端还给用户(用户会以为程序死了)。"
                "task 要写清**要达到什么结果**,不要写步骤(它有自己那套技能,写步骤是替它想)。"
                "结果会带着它的结论、产出位置、以及**它没做成的部分**一起回来 —— 那部分"
                "是真的没做,不要当成做完了。",
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要它做什么、做到什么程度算完。写清目标,不用写步骤。",
            },
            "vm": {
                "type": "boolean",
                "description": "要不要把虚拟机开给它。**默认 false** —— VM 是你和所有子 agent "
                               "共用的**一台**,只用在该用的任务上(需要在里面跑东西、装包、"
                               "动态分析样本)。",
            },
            "wait": {
                "type": "boolean",
                "description": "false(默认)= 派完就撒手,它干完通报你 —— **一般都用这个**。"
                               "true = 在这儿干等它出结果:会把你这一轮卡住"
                               "(期间派不了别的活、终端也还不回给用户)。",
            },
            "write": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许它**写**哪些路径(相对工作区)。**不填就等于不许写任何文件**。"
                               "**末尾的 `/` 决定是目录还是文件,必须写清**:目录写"
                               "[\"reports/\"],单个文件写 [\"notes/plan.md\"]。"
                               "**不带斜杠一律当文件** —— write=[\"reports\"] 是建一个叫"
                               "reports 的**文件**,不是目录。方向是宁可少给:判成文件只是"
                               "写不进去,判成目录是悄悄多给一片权限。"
                               "写范围必须给窄:并排派好几个时,两个范围撞上会被直接拒掉 ——"
                               "同时改一处,改完不报错、只是结果对不上,事后查不出是谁改的。",
            },
            "read": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许它**读**哪些路径。不填 = 整个工作区都能读(读基本无害)。",
            },
            "allow_delete": {
                "type": "boolean",
                "description": "允许它删/移文件。**默认 false,而且和写权限是分开的** ——"
                               "写坏一个文件还能改回来,删掉就没了。要删也只能删在 write "
                               "范围里的东西。",
            },
            "batch": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "write": {"type": "array", "items": {"type": "string"}},
                        "read": {"type": "array", "items": {"type": "string"}},
                        "allow_delete": {"type": "boolean"},
                        "vm": {"type": "boolean"},
                    },
                },
                "description": "**一次派好几件互相独立的活时用这个**(比连着调好几次省往返)。"
                               "每项一个对象,字段和单件时一样(每件可以有自己的 write 范围)。"
                               "**派出去的顺序由工具管**:先放一件、等它第一次请求落盘,"
                               "再一起放其余的(几秒钟,一个进程只发生一次)—— 这样它们能共用"
                               "同一段缓存前缀,每条少付一截全价输入。"
                               "**别自己一件一件派**,那等于把省下的 token 又花回去。",
            },
        },
        "required": [],
    },
)
def dispatch_task(task: str = "", vm: bool = False, wait: bool = False,
                  write: list | None = None, read: list | None = None,
                  allow_delete: bool = False, batch: list | None = None) -> str:
    from .. import ctx, tasks
    from ..ctx import FS_ANY, FsGrant
    from .container import prepare_scopes

    def _fs(w=None, r=None, dele=None) -> FsGrant:
        return FsGrant(read=tuple(r) if r else (FS_ANY,),
                       write=tuple(w or ()), delete=bool(dele))

    def _prep(grant: FsGrant) -> None:
        # 范围不存在就先建出来(建早了才早发现)。但那些说明**只打到终端**:
        # 每向主 agent 说一句话,它整段上下文就要重发一遍模型 —— 边角信息不值得那个价。
        for note in prepare_scopes(grant.write) if grant.write else []:
            try:
                ctx.out().print(f"! {note}", style="dim", markup=False)
            except Exception:      # noqa: BLE001 - 提示打不出来不该拦住派活
                pass

    if batch:
        if task.strip():
            return "错误:task 和 batch 只能给一个(一件活用 task,多件用 batch)。"
        items = []
        for i, one in enumerate(batch, 1):
            if not isinstance(one, dict) or not str(one.get("task") or "").strip():
                return f"错误:batch 里第 {i} 项没有 task(每项都要写清要它做什么)。"
            grant = _fs(one.get("write"), one.get("read"), one.get("allow_delete"))
            _prep(grant)
            items.append({"prompt": str(one["task"]).strip(),
                          "vm": bool(one.get("vm")),
                          "fs": grant, "wait": False})
        results, note = tasks.dispatch_batch(items)
        ok = [r["task_id"] for r in results if r.get("task_id")]
        bad = [r.get("message", "?") for r in results if r.get("status") == "error"]
        out = f"已派 {len(ok)} 件:{'、'.join(ok)},都在后台跑,干完会通报你。"
        if bad:
            out += f"({len(bad)} 件没派出去:{';'.join(bad)})"
        if note:
            out += f" {note}"
        return out

    if not (task or "").strip():
        return "错误:任务描述是空的 —— 子 agent 不知道要干什么。"
    fs = _fs(write, read, allow_delete)
    _prep(fs)
    r = tasks.dispatch(task, vm=vm, wait=wait, fs=fs)
    return r.get("message") or (f"已派给 {r['task_id']},它在后台跑。"
                                f"用 task_status 看进展;干完我会告诉你。")


@tool(
    description="看子 agent 的进展:不传 task_id 列全部,传了就只看那一个。"
                "派了不等的活(wait=false)之后用它确认干完没有;"
                "或者想知道某个子 agent 卡在哪、在等什么。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号,如 t1。不传 = 列全部。"},
        },
    },
)
def task_status(task_id: str = "") -> str:
    from .. import tasks
    tid = (task_id or "").strip()
    if not tid:
        return tasks.listing()
    t = tasks.get(tid)
    if t is None:
        return f"没有 {tid} 这个任务。当前:{tasks.listing()}"
    return tasks.report(t)["message"]


@tool(
    description="**验收并收尾**一个子 agent。它干完/停下来之后,由你做这个判断 —— "
                "它是不会自己消失的,得你明确处置。"
                "verdict=accept:验收通过,关闭它(对话和产出仍留在盘上,用户之后有疑问"
                "翻得到);**收尾之前先看一眼它的产出**:报告里说的「产出」路径去确认存在、"
                "抽查内容,别只信它的摘要 —— 摘要是概括,概括可能漏。"
                "verdict=rework:打回重做,note 里写清哪里不行、要它怎么改(空着打回等于"
                "让它把同一件事再做一遍)。"
                "verdict=stop:不要了,停下并关闭。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号,如 t1"},
            "verdict": {"type": "string", "enum": ["accept", "rework", "stop"],
                        "description": "accept=验收通过并关闭;rework=打回重做;stop=不要了。"},
            "note": {"type": "string",
                     "description": "verdict=rework 时**必填**:哪里不行、要它怎么改。"
                                    "其他情况可以不填。"},
        },
        "required": ["task_id", "verdict"],
    },
)
def finish_task(task_id: str, verdict: str = "accept", note: str = "") -> str:
    from .. import tasks
    return tasks.finish((task_id or "").strip(), verdict, note)


@tool(
    description="把一个**停下来等你回话**的子 agent 接着放下去跑。"
                "两种情况会用到:它中途挂起问你(申请权限、要问用户、拿不准要你定)、"
                "或者上次进程退出把它中断了。"
                "**已经收场(closed)的也能这么叫起来** —— 它的对话和读过的文件都还在,"
                "让它补一步/写报告/复核比重派一个新的从头做便宜得多。"
                "answer 里写清你的决定 —— 它会当成新信息接着往下做。"
                "**它申请的是权限时,光说「批准」没用** —— 权限在派它的时候就定死了,"
                "一句话改不了它能不能写文件、能不能用 VM。那种必须把权限**一并给出去**"
                "(write / read / allow_delete / vm):它要写 reports/ 就传 write=[\"reports/\"]。"
                "给的时候和派活一样划窄、一样是**增量**(在原有范围上加,不是替换)。"
                "**用户没同意就别替用户答应** —— 它申请的是它没有的东西,该问用户就问。"
                "中断的那种情况留意:它被中断时**正在做的那一步结果未知**,续跑会让那一步重做。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号,如 t1"},
            "answer": {"type": "string",
                       "description": "你对它那个问题的答复,或者让它继续的指示。"
                                      "它挂起时问什么,上面就照什么答。"},
            "write": {
                "type": "array", "items": {"type": "string"},
                "description": "**额外交给它的写权限**(相对工作区)。它申请写权限时**必须**"
                               "在这里给,光在 answer 里说「批准」它照样写不进去。"
                               "末尾的 `/` 决定是目录还是文件:目录写 [\"reports/\"],"
                               "单个文件写 [\"notes/plan.md\"];不带斜杠一律当文件。",
            },
            "read": {
                "type": "array", "items": {"type": "string"},
                "description": "**额外交给它的读权限**。它本来就能读整个工作区,"
                               "只有在你当初派活时收窄过读范围、而它现在要读那儿时才需要。",
            },
            "allow_delete": {
                "type": "boolean",
                "description": "把删除/移动也开给它。**默认不动**(不给就别传)——"
                               "删掉就没了,和写权限是分开的两件事。",
            },
            "vm": {
                "type": "boolean",
                "description": "把虚拟机开给它(它申请用 VM 时)。**默认不动**。"
                               "VM 是共用的一台,同时只该有一个子 agent 在里面跑东西,"
                               "撞上会被拒 —— 那时先把另一个停掉或者等它回来。",
            },
            "wait": {"type": "boolean",
                     "description": "false(默认)= 放它去跑,结果回头通报你;"
                                    "true = 在这儿等(会卡住你这一轮)。"},
        },
        "required": ["task_id"],
    },
)
def resume_task(task_id: str, answer: str = "", wait: bool = False,
                write: list | None = None, read: list | None = None,
                allow_delete: bool | None = None, vm: bool = False) -> str:
    from .. import tasks
    r = tasks.resume((task_id or "").strip(), answer=answer, wait=wait,
                     write=write, read=read, allow_delete=allow_delete, vm=vm)
    return r.get("message") or f"{task_id} 已经接着跑了,干完会告诉你。"


# ---------------- 下面这个只有子 agent 用 ----------------

@tool(
    agents=("sub",),
    description="**停下来,把主动权交出去。** 遇到下面这些情况必须用它,不要自己绕:"
                "①工具被拒(缺权限、缺资源);②需要用户拍板或提供信息;"
                "③拿不准取向,继续做下去可能要白干一大片。"
                "ask 里写清:你要什么、为什么需要、给了之后你打算干什么 —— 主 agent 拿着"
                "这句话去决定或者去问用户,写得不清楚就得来回好几趟。"
                "**不要用这个逃避困难**:能自己查、自己试的,先自己做。",
    parameters={
        "type": "object",
        "properties": {
            "ask": {"type": "string",
                    "description": "要什么、为什么、拿到之后你准备怎么做。一两句说清。"},
        },
        "required": ["ask"],
    },
)
def suspend(ask: str) -> str:
    from .. import ctx
    ctx.current().suspend = {"ask": (ask or "").strip() or "(它没说清要什么)"}
    return "已挂起,等主 agent 回话。"
