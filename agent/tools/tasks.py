"""派活的工具:主 agent 用来把一件事交给子 agent,子 agent 用来把问题交回去。

两边是**同一个机制的两头**:主 agent 派下去的是任务,子 agent 交上来的是问题(要权限、
要问用户、拿不准)。所以子 agent 那边没有"调用主 agent"这种动作 —— 它只是**把自己停下**。
"""
from __future__ import annotations

from .registry import tool


@tool(
    description="把一件**具体的事**派给一个子 agent 去做,拿回它的结论。"
                "适用:需要大量阅读或反复试错的活(代码审计、结构分析、质量检查、"
                "去混淆)、能各自独立并行的活、以及你清单里标着 **[子 agent 专用]** 的技能 —— "
                "那些你加载不了,只能派出去。"
                "不适用:一两步就能答完的问题、要跟用户来回商量的对话 —— 那些你自己做更快;"
                "派活的成本是**另起一次完整对话**,小事派出去反而更贵。"
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
                "description": "true(默认)= 在这儿等它干完再往下走,直接拿到结论;"
                               "false = 派完就撒手,你继续干别的,它干完会通知你。"
                               "要并排铺开好几件事时用 false,一次派完再等。",
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
        },
        "required": ["task"],
    },
)
def dispatch_task(task: str, vm: bool = False, wait: bool = True,
                  write: list | None = None, read: list | None = None,
                  allow_delete: bool = False) -> str:
    from .. import ctx, tasks
    from ..ctx import FS_ANY, FsGrant
    from .container import prepare_scopes
    fs = FsGrant(read=tuple(read) if read else (FS_ANY,),
                 write=tuple(write or ()),
                 delete=bool(allow_delete))
    # 范围不存在就先建出来(建早了才早发现)。但那些说明**只打到终端**:
    # 每向主 agent 说一句话,它整段上下文就要重发一遍模型 —— 边角信息不值得那个价。
    for note in prepare_scopes(fs.write) if fs.write else []:
        try:
            ctx.out().print(f"! {note}", style="dim", markup=False)
        except Exception:      # noqa: BLE001 - 提示打不出来不该拦住派活
            pass
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
    description="把一个**停下来等你回话**的子 agent 接着放下去跑。"
                "两种情况会用到:它中途挂起问你(申请权限、要问用户、拿不准要你定)、"
                "或者上次进程退出把它中断了。"
                "answer 里写清你的决定 —— 它会当成新信息接着往下做。"
                "中断的那种情况留意:它被中断时**正在做的那一步结果未知**,续跑会让那一步重做。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号,如 t1"},
            "answer": {"type": "string",
                       "description": "你对它那个问题的答复,或者让它继续的指示。"
                                      "它挂起时问什么,上面就照什么答。"},
            "wait": {"type": "boolean",
                     "description": "true(默认)= 等它这一轮跑完再回来;false = 撒手。"},
        },
        "required": ["task_id"],
    },
)
def resume_task(task_id: str, answer: str = "", wait: bool = True) -> str:
    from .. import tasks
    r = tasks.resume((task_id or "").strip(), answer=answer, wait=wait)
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
