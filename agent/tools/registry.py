"""工具注册表:自动发现本包下的工具模块,收集工具函数与它们的 schema。

每个工具在**自己所在的模块**里,用 `@tool(description=..., parameters=...)` 声明自己的
描述与参数(也就是模型看到的提示词),不再集中到本文件里维护。
本模块在导入时自动扫描 agent/tools/ 下的所有模块并收集 —— 新增一个工具只要在对应域模块
里写函数 + 装饰器,不必回来改这里。

约定:模块名以下划线开头视为私有,不参与扫描;工具名默认取函数名,也可以在装饰器里用 name= 覆盖。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from .. import ctx

_TOOL_FUNCS: dict[str, object] = {}   # 工具名 -> 实现函数
_TOOL_AGENTS: dict[str, tuple] = {}   # 工具名 -> 谁能调用(默认授权)
_TOOLS: list[dict] = []               # 全部工具的 JSON schema(发给模型看)


def tool(description: str, parameters: dict | None = None, name: str | None = None,
         agents: tuple = ("main",)):
    """把一个函数登记为模型可调用的工具,并附上它的 JSON schema。

    description 写清楚"什么时候用它",模型选工具主要靠这段文字;parameters 是
    JSON Schema 形式的参数说明(缺省表示无参数工具)。

    `agents` 声明**默认谁能调用**,只认 `"main"` / `"sub"`。**默认只有主 agent** ——
    新加的工具必须显式写上 `agents=("main", "sub")` 子 agent 才拿得到。默认给宽的话,
    以后谁加了个能干重活(或危险活)的工具,子 agent 会一声不响地就有了;默认给窄,
    最坏的结果是子 agent 用不了、报错信息告诉它去找主 agent —— 这个方向的错好收拾。

    **注意:这不影响发给模型的工具表。** 那张表是全局固定的(见 TOOLS 的注释),
    这里的声明只在**调用时**判 —— 授权是动态的,工具表不能是。
    """
    def decorate(fn):
        tool_name = name or fn.__name__
        if tool_name in _TOOL_FUNCS:
            raise RuntimeError(f"工具名重复:{tool_name}(来自 {fn.__module__})")
        bad = [a for a in agents if a not in ("main", "sub")]
        if bad:
            raise RuntimeError(f"工具 {tool_name} 的 agents 里有不认识的角色 {'、'.join(bad)}")
        _TOOL_FUNCS[tool_name] = fn
        _TOOL_AGENTS[tool_name] = tuple(agents)
        _TOOLS.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": parameters or {"type": "object", "properties": {}},
            },
        })
        return fn                      # 装饰器不改变函数本身,原有直接调用照常可用
    return decorate


def allowed(name: str, role: str) -> bool:
    """这个角色能不能调这个工具(**声明层面**的默认授权)。

    比这更细的授权不在这儿 —— 那些跟着**单次任务**走(这个子 agent 被给了写权限吗?
    允许碰 VM 吗?),由下面的 _TASK_GRANTS 判。
    """
    agents = _TOOL_AGENTS.get(name)
    return bool(agents) and role in agents


# 跟**单次任务**走的授权:按工具名前缀归类,值是一个"当前 agent 配不配用"的判断。
# 放在这儿而不是散进各工具模块,是因为这类判断必须**在调用前统一生效** —— 写进某个
# 工具函数内部的话,漏一个就是一条后门,而且谁也不会注意到。
_TASK_GRANTS = {
    # **主 agent 永远有** —— VM 是它自己管的东西(cli 启动时就在用 vm_status)。
    # 这条闸门拦的是子 agent:它们没有自己的机器,要用得由主 agent 在派活时开。
    "vm_": (lambda c: c.role == "main" or c.vm_grant,
            "这次的活没给你 VM 使用权。VM 是主 agent 和所有子 agent **共用的一台** —— "
            "要用就得由主 agent 明确开给你(派活时指定),不能自己拿。"
            "如果这个任务确实需要 VM,把它写进报告交由主 agent 决定。"),
}


def _grant_denied(name: str, c) -> str:
    """按任务走的授权检查。返回拒绝理由,没被拒就返回空串。"""
    for prefix, (ok, why) in _TASK_GRANTS.items():
        if name.startswith(prefix) and not ok(c):
            return why
    return ""


def _discover() -> None:
    """导入本包下所有工具模块,触发它们的 @tool 注册。"""
    pkg = __name__.rsplit(".", 1)[0]            # "agent.tools"
    here = Path(__file__).parent
    for path in sorted(here.glob("*.py")):
        if path.stem.startswith("_") or path.stem == Path(__file__).stem:
            continue
        importlib.import_module(f"{pkg}.{path.stem}")
    _check_no_private_tools()


def _check_no_private_tools() -> None:
    """注册表体检:工具名不该以下划线开头。

    抓的是**装饰器错位** —— 往 `@tool` 和它的 `def` 之间插辅助函数时,装饰器会落到辅助
    函数头上,原工具反过来变成裸函数。症状很隐蔽:某个天天在用的工具莫名从清单里消失,
    多出一个没人认识的私有名,模型还会照着那个错名字去调。

    这个坑踩过三次(vm_status、read_file、delete_file 各一次),每次都是靠人肉核对工具表
    才发现。项目里所有工具都是公开名,私有名只可能是辅助函数 —— 所以一条断言就能兜住。
    """
    bad = sorted(n for n in _TOOL_FUNCS if n.startswith("_"))
    if bad:
        raise RuntimeError(
            f"这些工具名以下划线开头:{bad} —— 几乎可以肯定是 @tool 装饰器落错了位置"
            f"(插辅助函数时被夹在了 @tool 和原 def 之间,把装饰器抢走了)。"
        )


_discover()

# 对外的只读视图(名字保持不变,loop/llm 无需改动)
TOOL_FUNCS = _TOOL_FUNCS
TOOL_AGENTS = _TOOL_AGENTS
# **全部**工具的 schema,不做任何裁剪 —— 主 agent 和每个子 agent 拿到的都是这一份。
#
# 为什么不让每个 agent 各拿一份自己的:
#   · 工具表是请求前缀的一部分,DeepSeek 的缓存按前缀命中。所有人用同一份,
#     第一个子 agent 把这个块焐热,后面 9 个直接命中(实测过:带上 tools 命中
#     6912 tokens,不带则 0)。按角色裁表 = 每人一份冷前缀,那是真花钱的地方。
#   · 反过来,**"谁能用什么"必须在调用时判** —— 授权是动态的(子 agent 能临时
#     向主 agent 申请权限),而工具表一旦在中途变了,整段对话的缓存全部失效。
# 一句话:**工具表不能是动态的,授权必须是动态的。**
TOOLS = _TOOLS


def dispatch(name: str, arguments: str) -> str:
    """执行工具。出错不崩溃,把错误信息回传给模型,它通常能自己改参数重试。"""
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"错误:不存在名为 {name} 的工具"

    # 授权在**调用时**判,而不是靠"没给它看就调不到" —— 模型完全可能凭记忆报出一个
    # 它没被授予的工具名。只裁 schema 而不在这儿拦,前面所有授权都只是装饰。
    me = ctx.current()
    if not allowed(name, me.role):
        who = "主 agent" if me.role == "main" else "子 agent"
        return (f"错误:你是{who},没有 `{name}` 这个工具。"
                f"换个你能用的工具,或者把这件事报告给主 agent 由它决定 —— 别硬凑。")
    denied = _grant_denied(name, me)
    if denied:
        return f"错误:{denied}"

    try:
        return str(func(**json.loads(arguments)))
    except Exception as exc:  # noqa: BLE001 - 任何异常都应该反馈给模型
        return f"错误:{type(exc).__name__}: {exc}"
